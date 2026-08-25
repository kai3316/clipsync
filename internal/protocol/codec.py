"""Binary frame encoder/decoder for clipboard sync protocol.

Frame format (TLV + header):
  [2 bytes] magic: 0x4353 ("CS" — ClipSync; binary frames use 0x4253 "BS")
  [1 byte]  version
  [4 bytes] payload length L
  [4 bytes] message_id length N
  [N bytes] message_id (UUID hex)
  [1 byte]  source_device length M
  [M bytes] source_device
  [L bytes] payload (JSON-serialized content metadata + format descriptors)

Clipboard payload JSON structure:
{
  "msg_type": "clipboard",
  "types": {
    "TEXT": "<base64>",
    "HTML": "<base64>",
    "IMAGE_PNG": "<base64>"
  },
  "timestamp": 1234567890.123
}

File transfer payload JSON structures:
  file_request:  {"msg_type": "file_request", "transfer_id": "...",
                  "file_name": "...", "file_size": N, "mime_type": "..."}
  file_chunk:    {"msg_type": "file_chunk", "transfer_id": "...",
                  "chunk_index": N, "total_chunks": N, "data": "<base64>"}
  file_ack:      {"msg_type": "file_ack", "transfer_id": "..."}
  file_reject:   {"msg_type": "file_reject", "transfer_id": "..."}
  file_complete: {"msg_type": "file_complete", "transfer_id": "...", "status": "..."}

If "msg_type" is absent from the payload, it defaults to "clipboard" for
backward compatibility.
"""

import base64
import json
import logging
import struct
import time
import uuid
import zlib
from io import BytesIO

logger = logging.getLogger(__name__)

from internal.clipboard.format import ClipboardContent, ContentType, SyncMessage

MAGIC = 0x4353  # "CS" for ClipSync (JSON frames)
BINARY_MAGIC = 0x4253  # "BS" for binary payload frames (file chunks)
VERSION = 2
HEADER_FMT = ">H B I"  # magic, version, payload_length
HEADER_SIZE = 7
# Upper bound for any single decompressed content payload.  Mirrors the
# transport frame cap (10 MB) and prevents a tiny compressed frame from
# expanding into a zip-bomb OOM during decode.
MAX_FRAME_SIZE = 10 * 1024 * 1024  # 10 MB

# Wire-protocol type labels.  These are fixed for cross-version
# compatibility and intentionally differ from the persistence labels
# (internal.clipboard.dedup.CONTENT_TYPE_LABELS, where IMAGE_PNG is "IMAGE"):
# do NOT align the two — the wire name is part of the on-the-wire contract.
_TYPE_NAME_MAP = {
    ContentType.TEXT: "TEXT",
    ContentType.HTML: "HTML",
    ContentType.RTF: "RTF",
    ContentType.IMAGE_PNG: "IMAGE_PNG",
    ContentType.IMAGE_EMF: "IMAGE_EMF",
}
_NAME_TYPE_MAP = {v: k for k, v in _TYPE_NAME_MAP.items()}


def has_syncable_types(content: ClipboardContent) -> bool:
    """Return True if any format in ``content`` can be encoded for sync."""
    return any(t in _TYPE_NAME_MAP for t in content.types)


# Valid message types for file transfer routing
FILE_TRANSFER_MSG_TYPES = frozenset({
    "file_request", "file_chunk", "file_ack", "file_reject", "file_complete",
    "file_chunk_ack", "file_pause", "file_resume",
    "speed_test_data", "speed_test_result",
})

# Pairing lifecycle messages sent over the sync transport.  These keep both
# devices' pairing state in sync: confirmation is a two-sided commitment, so
# each side tells the other when it confirms, rejects, or un-pairs.
PAIRING_MSG_TYPES = frozenset({
    "pairing_confirm", "pairing_reject", "pairing_unpair",
})

# Nearby-chat messages for consent-gated communication with UNPAIRED devices
# discovered on the LAN.  These are the only frames (besides pairing) that the
# transport gate lets through from unpaired peers; every other type still
# requires an established (paired) trust relationship.
CHAT_MSG_TYPES = frozenset({
    "chat_invite", "chat_accept", "chat_decline", "chat_close",
    "chat_text", "chat_ping", "chat_pong",
    "chat_file_offer", "chat_file_accept", "chat_file_reject",
    "chat_file_cancel", "chat_file_complete",
    # Typing indicator ({session_id, typing}).  Backward compatible: older
    # peers drop unknown msg_types at the unpaired gate (logged, connection
    # kept) or fall through to clipboard handling where the empty "types"
    # payload is discarded by SyncManager's content.is_empty() check.
    "chat_typing",
})

# Frame types an UNPAIRED peer may send at the transport gate.  Chat file
# BYTES ride the generic ``file_chunk`` binary frame (not a ``chat_*`` type),
# so it must be admitted here too.  This is safe: the app router gives chat
# right-of-first-refusal on ``file_chunk`` (ChatManager.handle_binary_chunk),
# and FileTransferManager no-ops frames with unknown transfer_ids, so an
# unpaired peer still cannot initiate clipboard transfers — only chat carries
# file bytes from unpaired peers.
UNPAIRED_GATE_MSG_TYPES = PAIRING_MSG_TYPES | CHAT_MSG_TYPES | frozenset({"file_chunk"})

# Internet-relay messages.  ``relay_enroll`` ({relay_secret}) is sent to an
# already-paired peer over its encrypted LAN connection, so both sides can
# derive the shared public-broker topic + key (internal/transport/relay.py).
# ``relay_ack`` (Round 17) is the internet "delivered" receipt: {msg_id, ts},
# published back on the same encrypted relay channel when a clipboard frame
# lands in the receiver's history.  Both are paired-only by construction —
# deliberately NOT in UNPAIRED_GATE_MSG_TYPES (the transport gate drops them
# from unpaired LAN peers; the relay path only ever admits them from peers
# that hold the shared channel key).
RELAY_MSG_TYPES = frozenset({"relay_enroll", "relay_ack"})

# AI-config sync (Round 12): paired devices exchange *metadata* inventories of
# their user-declared AI tool config files (CLAUDE.md, memory notes, skills,
# .mcp.json, ...) via aiconfig_inv, then pull individual file contents with
# aiconfig_req / aiconfig_data.  These frames expose real file content, so they
# are paired-only by construction — deliberately NOT in UNPAIRED_GATE_MSG_TYPES
# (the transport gate already drops them from unpaired peers; the app-layer
# handler re-checks pairing as defense in depth).
AICONFIG_MSG_TYPES = frozenset({
    "aiconfig_inv", "aiconfig_req", "aiconfig_data",
})

# Internet pairing-code handshake (Round 14): ``netpair_hello`` rides the
# encrypted public-relay channel on a topic derived from a shared pairing code,
# so it is implicitly authenticated by that secret — there is no LAN trust to
# gate.  Deliberately NOT in UNPAIRED_GATE_MSG_TYPES: an unpaired LAN peer must
# never be able to inject a hello over the local channel.
NETPAIR_MSG_TYPES = frozenset({"netpair_hello"})

# Device connectivity probe (device card "test connection" button): a paired
# peer answers ``device_ping`` with ``device_pong`` echoing the same ping_id +
# ts so the initiator can measure per-channel round-trip latency.  Paired-only
# by construction — NOT in UNPAIRED_GATE_MSG_TYPES: an unpaired LAN peer must
# never be able to solicit a pong (or have its pings routed) over the local
# channel, and the relay path only admits them from channel-key holders.
DEVICE_PROBE_MSG_TYPES = frozenset({"device_ping", "device_pong"})


def encode_frame(payload_dict: dict, msg_id: str = "", source_device: str = "") -> bytes:
    """Encode a generic JSON payload dict into the binary frame format.

    This is the low-level encoder used by both clipboard sync and file transfers.
    Any dict can be passed as the payload; it will be JSON-serialized and wrapped
    in the standard ClipSync binary frame.
    """
    payload_bytes = json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")

    msg_id_bytes = (msg_id or uuid.uuid4().hex).encode("ascii")
    src_str = source_device[:255]
    while True:
        src_bytes = src_str.encode("utf-8")
        if len(src_bytes) <= 255:
            break
        src_str = src_str[:-1]  # trim one char to avoid mid-codepoint truncation

    buf = BytesIO()
    buf.write(struct.pack(HEADER_FMT, MAGIC, VERSION, len(payload_bytes)))
    buf.write(struct.pack(">I", len(msg_id_bytes)))
    buf.write(msg_id_bytes)
    buf.write(struct.pack(">B", len(src_bytes)))
    buf.write(src_bytes)
    buf.write(payload_bytes)

    return buf.getvalue()


def encode_message(msg: SyncMessage, msg_type: str = "clipboard") -> bytes:
    """Encode a SyncMessage to wire format bytes.

    Args:
        msg: The SyncMessage containing clipboard content.
        msg_type: The message type discriminator (default "clipboard").
                  File transfers use types like "file_request", "file_chunk", etc.
    """
    payload: dict = {
        "msg_type": msg_type,
        "types": {},
        "timestamp": msg.content.timestamp,
    }

    if msg.content.image_fmt:
        payload["image_fmt"] = msg.content.image_fmt

    for content_type, data in msg.content.types.items():
        name = _TYPE_NAME_MAP.get(content_type)
        if name is None:
            logger.debug("Skipping unregistered content type: %s", content_type)
            continue
        # zlib compress non-PNG raster images to reduce wire size
        if content_type == ContentType.IMAGE_PNG and msg.content.image_fmt not in ("", "png"):
            data = zlib.compress(data, level=1)
        payload["types"][name] = base64.b64encode(data).decode("ascii")

    if not payload["types"] and msg.content.types:
        # Content carried types, but none of them are encodable (e.g. a
        # FILE/URL-only capture) — there is nothing to sync, so emit no frame.
        logger.debug("No syncable clipboard types — skipping encode")
        return b""

    return encode_frame(payload, msg.msg_id, msg.source_device)


def encode_binary_chunk(transfer_id: str, chunk_index: int,
                        total_chunks: int, raw_data: bytes) -> bytes:
    """Encode a file chunk as a compact binary frame (no base64/JSON overhead).

    Binary frame format::

      [2 bytes]  magic: 0x4253 ("BS")
      [32 bytes] transfer_id (ascii hex UUID)
      [4 bytes]  chunk_index (uint32, big-endian)
      [4 bytes]  total_chunks (uint32, big-endian)
      [4 bytes]  data_length (uint32, big-endian)
      [N bytes]  raw chunk data

    Total header: 46 bytes.
    """
    tid_bytes = transfer_id.encode("ascii")
    if len(tid_bytes) != 32:
        raise ValueError(f"transfer_id must be 32 hex chars, got {len(tid_bytes)}")
    header = struct.pack(
        ">H32sIII",
        BINARY_MAGIC,
        tid_bytes,
        chunk_index,
        total_chunks,
        len(raw_data),
    )
    return header + raw_data


def _decode_binary_frame(data: bytes):
    """Decode a binary frame into a SyncMessage, or return None."""
    BIN_HEADER_SIZE = 2 + 32 + 4 + 4 + 4  # 46 bytes
    if len(data) < BIN_HEADER_SIZE:
        return None
    magic, tid_bytes, chunk_index, total_chunks, data_len = struct.unpack_from(
        ">H32sIII", data, 0,
    )
    if magic != BINARY_MAGIC:
        return None
    if BIN_HEADER_SIZE + data_len > len(data):
        return None
    try:
        transfer_id = tid_bytes.decode("ascii")
    except (UnicodeDecodeError, ValueError):
        return None
    raw_data = data[BIN_HEADER_SIZE:BIN_HEADER_SIZE + data_len]

    content = ClipboardContent(timestamp=time.time())
    result = SyncMessage(content=content, msg_id=transfer_id, source_device="")
    result.msg_type = "file_chunk"
    result._raw_payload = {
        "msg_type": "file_chunk",
        "transfer_id": transfer_id,
        "chunk_index": chunk_index,
        "total_chunks": total_chunks,
        "_raw_data": raw_data,
    }
    return result


def decode_message(data: bytes) -> SyncMessage | None:
    """Decode wire format bytes to a SyncMessage, or None if invalid.

    The returned SyncMessage will have a ``msg_type`` attribute set:
      - "clipboard" for legacy/new clipboard sync messages.
      - One of the ``FILE_TRANSFER_MSG_TYPES`` for file transfers.
      - Falls back to "clipboard" if the ``msg_type`` field is missing
        from the JSON payload (backward compatibility).

    The raw decoded payload dict is stored as ``_raw_payload`` on the
    returned object so that file transfer handlers can access the full
    message body without a second deserialization.
    """
    if len(data) < HEADER_SIZE:
        return None

    # Route binary frames to the dedicated decoder
    if len(data) >= 2:
        first_two = struct.unpack_from(">H", data, 0)[0]
        if first_two == BINARY_MAGIC:
            return _decode_binary_frame(data)

    magic, version, payload_len = struct.unpack_from(HEADER_FMT, data, 0)
    if magic != MAGIC:
        logger.debug("Frame magic mismatch: expected 0x%04x, got 0x%04x", MAGIC, magic)
        return None
    if version < 1 or version > VERSION:
        logger.debug("Frame version out of range: got %d, accepted [1, %d]", version, VERSION)
        return None

    offset = HEADER_SIZE

    if offset + 4 > len(data):
        return None
    msg_id_len = struct.unpack_from(">I", data, offset)[0]
    offset += 4

    if offset + msg_id_len > len(data):
        return None
    try:
        msg_id = data[offset:offset + msg_id_len].decode("ascii")
    except (UnicodeDecodeError, ValueError):
        return None
    offset += msg_id_len

    if offset + 1 > len(data):
        return None
    src_len = struct.unpack_from(">B", data, offset)[0]
    offset += 1

    if offset + src_len > len(data):
        return None
    try:
        source_device = data[offset:offset + src_len].decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return None
    offset += src_len

    if offset + payload_len > len(data):
        return None
    payload_bytes = data[offset:offset + payload_len]

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError):
        return None

    # A syntactically valid JSON payload that isn't an object (list, string,
    # number, null) would blow up the .get() calls below — treat it as a bad
    # frame instead of letting it tear down the whole receive loop.
    if not isinstance(payload, dict):
        logger.debug("Frame payload is JSON but not an object (%s)", type(payload).__name__)
        return None

    # --- Extract message type and image format (backward-compatible) ---
    msg_type = payload.get("msg_type", "clipboard")
    # A non-string discriminator (e.g. {"msg_type": {}}) is unhashable: every
    # router does ``msg_type in <frozenset>``, which raises TypeError — and in
    # the transport recv loop's catch-all that tears down the whole
    # connection.  Drop the malformed frame instead of crashing the link.
    if not isinstance(msg_type, str):
        logger.debug(
            "Frame 'msg_type' is %s, expected a string -- dropped",
            type(msg_type).__name__,
        )
        return None
    image_fmt = payload.get("image_fmt", "")
    if not isinstance(image_fmt, str):
        image_fmt = ""
    # Timestamp must be a finite number in a sane range -- anything else
    # (string, null, list, NaN/Infinity literal) would poison downstream
    # age/sort arithmetic.
    raw_ts = payload.get("timestamp", 0.0)
    if isinstance(raw_ts, bool) or not isinstance(raw_ts, (int, float)) \
            or not (-1e15 < raw_ts < 1e15):
        raw_ts = 0.0

    content = ClipboardContent(
        timestamp=float(raw_ts),
        image_fmt=image_fmt,
    )

    # A malformed "types" (list, string, number) has no .items() — treat it
    # as "no types" instead of raising into the receive loop.
    raw_types = payload.get("types", {})
    if not isinstance(raw_types, dict):
        logger.debug("Frame 'types' is %s, expected an object", type(raw_types).__name__)
        raw_types = {}
    for name, b64_data in raw_types.items():
        content_type = _NAME_TYPE_MAP.get(name)
        if content_type:
            try:
                decoded = base64.b64decode(b64_data)
                # zlib decompress non-PNG raster images.  Bound the output so a
                # tiny compressed frame cannot expand into a zip-bomb OOM.
                if content_type == ContentType.IMAGE_PNG and image_fmt not in ("", "png"):
                    try:
                        decompressor = zlib.decompressobj()
                        decoded = decompressor.decompress(decoded, MAX_FRAME_SIZE)
                        if decompressor.unconsumed_tail or not decompressor.eof:
                            # Decompressed output would exceed the sane cap —
                            # drop this content type.
                            logger.debug(
                                "Image payload exceeds decompression cap for %s", name,
                            )
                            continue
                    except zlib.error:
                        pass  # legacy uncompressed data
                    except MemoryError:
                        logger.debug("Memory error decompressing content type %s", name)
                        continue
                content.types[content_type] = decoded
            except Exception:
                logger.debug("Invalid base64 for content type %s", name)
                continue

    result = SyncMessage(
        content=content,
        msg_id=msg_id,
        source_device=source_device,
    )

    # Attach metadata so callers can route file-transfer messages without
    # re-parsing the raw bytes.
    result.msg_type = msg_type
    result._raw_payload = payload

    return result
