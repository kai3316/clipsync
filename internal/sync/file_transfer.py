"""Peer-to-peer file transfer for ClipSync.

Handles chunked file transfers between paired devices over the existing
TLS-encrypted transport. Messages are encoded with the standard binary
frame format and route through the same connections as clipboard sync.

Message types (stored in ``msg_type`` field of the JSON payload):
  file_request  -- sender announces a file the receiver may accept/reject
  file_chunk    -- a 64 KB base64-encoded slice of the file
  file_ack      -- receiver accepts a file_request
  file_reject   -- receiver declines a file_request
  file_complete -- receiver confirms successful (or failed) reception

Transfer flow (sender):
  1. User selects file -> send_file() called
  2. FILE_REQUEST sent to all connected peers via broadcast_fn
  3. Wait for FILE_ACK from at least one peer
  4. Read file in 64 KB chunks, send each as FILE_CHUNK
  5. After final chunk, wait for FILE_COMPLETE (with timeout)

Transfer flow (receiver):
  1. FILE_REQUEST arrives -> callback to UI for user decision
  2. If accepted -> FILE_ACK sent
  3. FILE_CHUNK arrives -> write to temp file, report progress
  4. Last chunk -> verify size, move to output directory
  5. FILE_COMPLETE sent with status (success / error_*)
"""

import base64
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from internal.protocol.codec import encode_binary_chunk, encode_frame

logger = logging.getLogger(__name__)


def _mask_file_name(file_name: str) -> str:
    """Return a privacy-safe file name: only the extension is preserved."""
    if not file_name or file_name == "?":
        return file_name
    ext = os.path.splitext(file_name)[1]
    return f"*{ext}" if ext else "*"


def _mask_path(path: str) -> str:
    """Return a privacy-safe path: only the parent directory name is shown."""
    parent = os.path.basename(os.path.dirname(path))
    return f"{parent}/***" if parent else "***"


# ---- Constants -----------------------------------------------------------

CHUNK_SIZE = 262144                    # 256 KB per chunk
TRANSFER_TIMEOUT = 120.0               # seconds -- overall transfer deadline
COMPLETION_WAIT_TIMEOUT = 60.0         # seconds -- wait for FILE_COMPLETE after last chunk
SPEED_TEST_CHUNKS = 20                 # number of chunks for speed test (~1.3 MB)
MAX_HISTORY = 50                       # max completed transfers to remember
MAX_FILE_SIZE = 2 * 1024**3            # 2 GiB -- maximum accepted file size

_MIME_BY_EXT: dict[str, str] = {
    ".txt": "text/plain",
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".js": "application/javascript",
    ".json": "application/json",
    ".xml": "application/xml",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".mp4": "video/mp4",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".zip": "application/zip",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
    ".7z": "application/x-7z-compressed",
    ".py": "text/x-python",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _guess_mime_type(file_name: str) -> str:
    """Return a MIME type for *file_name* based on its extension."""
    ext = Path(file_name).suffix.lower()
    return _MIME_BY_EXT.get(ext, "application/octet-stream")


def _safe_remove(path: Path) -> None:
    """Remove a file, suppressing any OSError."""
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _sanitize_file_name(file_name: str) -> str:
    """Strip path separators, traversal components, Windows-reserved names,
    and trailing dots/spaces from a remote file name."""
    name = Path(file_name).name
    name = name.lstrip(".")
    if not name:
        name = "unnamed_file"
    name = name.rstrip(" .")
    if not name:
        name = "unnamed_file"
    base = name.split(".")[0].upper()
    if base in _WINDOWS_RESERVED_NAMES:
        name = f"_{name}"
    return name


# ---- FileTransferManager -------------------------------------------------

class FileTransferManager:
    """Manages peer-to-peer file transfers over the existing transport layer.

    Parameters
    ----------
    device_id:
        The local device identifier (used as the source in frame headers).
    output_dir:
        Directory where received files are saved.
        Defaults to ``~/Downloads/ClipSync``.
    """

    CHUNK_SIZE = CHUNK_SIZE

    def __init__(self, device_id: str, output_dir: Optional[str] = None,
                 transfer_timeout: float = TRANSFER_TIMEOUT):
        self._device_id = device_id

        if output_dir is None:
            output_dir = str(Path.home() / "Downloads" / "ClipSync")
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._transfer_timeout = transfer_timeout

        # transfer_id -> dict (active transfers)
        self._transfers: dict[str, dict[str, Any]] = {}
        # Completed transfers history: list of dicts (newest first)
        self._history: list[dict[str, Any]] = []
        # Speed test state
        self._speed_test: dict[str, Any] | None = None
        self._lock = threading.Lock()

        # ---- UI callbacks ----
        self._on_transfer_progress: Optional[Callable[[str, float], None]] = None
        self._on_transfer_complete: Optional[Callable[[str, bool], None]] = None
        self._on_file_received: Optional[Callable[[str, str, str], None]] = None
        self._on_transfer_request: Optional[Callable[[str, str, int, str, Callable], None]] = None

    # ------------------------------------------------------------------
    # Callback registration
    # ------------------------------------------------------------------

    def set_on_transfer_progress(self, callback: Callable[[str, float], None]) -> None:
        """*callback(transfer_id, fraction)* -- called as chunks arrive or are sent."""
        self._on_transfer_progress = callback

    def set_on_transfer_complete(self, callback: Callable[[str, bool], None]) -> None:
        """*callback(transfer_id, success)* -- called when a transfer finishes or fails."""
        self._on_transfer_complete = callback

    def set_on_file_received(self, callback: Callable[[str, str, str], None]) -> None:
        """*callback(transfer_id, saved_path, file_name)* -- called after a file is
        saved successfully to the output directory."""
        self._on_file_received = callback

    def set_on_transfer_request(
        self, callback: Callable[[str, str, int, str, Callable], None],
    ) -> None:
        """*callback(transfer_id, file_name, file_size, mime_type, send_fn)* --
        called when a remote peer wants to send a file.

        The callback should call :meth:`accept_transfer` or :meth:`reject_transfer`
        with *transfer_id* and *send_fn* to indicate the user's choice.
        """
        self._on_transfer_request = callback

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send_file(self, file_path: str, broadcast_fn: Callable[[bytes], None]) -> str:
        """Start sending *file_path* to all connected peers.

        Parameters
        ----------
        file_path:
            Absolute or relative path to the file to send.
        broadcast_fn:
            Callable that takes encoded ``bytes`` and sends them to all
            connected peers (typically ``TransportManager.broadcast``).

        Returns
        -------
        transfer_id:
            A unique hex string identifying this transfer.

        Raises
        ------
        FileNotFoundError:
            If *file_path* does not exist or is not a regular file.
        """
        file_path = Path(file_path).resolve()
        if not file_path.is_file():
            raise FileNotFoundError(f"File not found: {file_path}")

        transfer_id = uuid.uuid4().hex
        file_size = file_path.stat().st_size
        file_name = file_path.name
        mime_type = _guess_mime_type(file_name)
        total_chunks = max((file_size + self.CHUNK_SIZE - 1) // self.CHUNK_SIZE, 1)

        now = time.time()
        with self._lock:
            self._transfers[transfer_id] = {
                "transfer_id": transfer_id,
                "type": "outgoing",
                "file_path": str(file_path),
                "file_name": file_name,
                "file_size": file_size,
                "mime_type": mime_type,
                "total_chunks": total_chunks,
                "state": "awaiting_ack",
                "start_time": now,
                "_last_activity": now,
                "acked": False,
                "_last_progress": 0.0,
                "_bytes_sent": 0,
                "_send_fn": broadcast_fn,
            }

        self._send_as_frame(
            {
                "msg_type": "file_request",
                "transfer_id": transfer_id,
                "file_name": file_name,
                "file_size": file_size,
                "mime_type": mime_type,
            },
            broadcast_fn,
        )

        logger.info(
            "File transfer %s initiated: %s (%d bytes, %d chunks)",
            transfer_id[:8], _mask_file_name(file_name), file_size, total_chunks,
        )
        return transfer_id

    def get_transfer_send_fn(self, transfer_id: str) -> Callable[[bytes], None] | None:
        """Return the stored send function for an outgoing transfer."""
        with self._lock:
            transfer = self._transfers.get(transfer_id)
        if transfer is None:
            return None
        return transfer.get("_send_fn")

    def accept_transfer(self, transfer_id: str, send_fn: Callable[[bytes], None]) -> None:
        """Accept an incoming file transfer request.

        Call this from the ``on_transfer_request`` callback to indicate
        that the user wants to receive the file.
        """
        with self._lock:
            transfer = self._transfers.get(transfer_id)
            if transfer is None or transfer.get("type") != "incoming":
                logger.warning("Cannot accept unknown or outgoing transfer: %s", transfer_id[:8])
                return
            if transfer["state"] != "pending":
                logger.debug("Transfer %s already in state %s", transfer_id[:8], transfer["state"])
                return

            transfer["state"] = "receiving"
            temp_path = self._output_dir / f".{transfer_id}.part"
            try:
                transfer["temp_fh"] = open(str(temp_path), "wb")
            except OSError as exc:
                logger.error("Cannot create temp file for transfer %s: %s", transfer_id[:8], exc)
                self._transfers.pop(transfer_id, None)
                self._send_as_frame(
                    {"msg_type": "file_reject", "transfer_id": transfer_id},
                    send_fn,
                )
                return

        self._send_as_frame(
            {"msg_type": "file_ack", "transfer_id": transfer_id},
            send_fn,
        )
        logger.info(
            "Accepted file transfer: %s (%s)",
            transfer_id[:8], _mask_file_name(transfer.get("file_name", "?")),
        )

    def cancel_transfer(self, transfer_id: str, broadcast_fn: Callable[[bytes], None] | None = None) -> bool:
        """Cancel an active transfer (incoming or outgoing).

        Returns True if the transfer was found and cancelled, False otherwise.
        """
        with self._lock:
            transfer = self._transfers.get(transfer_id)
            if transfer is None:
                return False
            transfer["cancelled"] = True

        # Clean up temp file for incoming transfers
        if transfer.get("type") == "incoming":
            temp_fh = transfer.get("temp_fh")
            if temp_fh is not None:
                try:
                    temp_fh.close()
                except Exception:
                    pass
            temp_path = self._output_dir / f".{transfer_id}.part"
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

        # Notify peer -- but only if the transfer actually started.  An
        # outgoing transfer that was never acked already ended with the
        # file_request, so there is nothing to cancel on the peer's side.
        if broadcast_fn is not None:
            if not (transfer.get("type") == "outgoing" and not transfer.get("acked")):
                self._send_as_frame(
                    {"msg_type": "file_complete", "transfer_id": transfer_id, "status": "cancelled"},
                    broadcast_fn,
                )

        with self._lock:
            self._transfers.pop(transfer_id, None)

        self._add_to_history(transfer, False)
        logger.info("Transfer %s cancelled by user", transfer_id[:8])
        if self._on_transfer_complete is not None:
            self._on_transfer_complete(transfer_id, False)
        return True

    def reject_transfer(self, transfer_id: str, send_fn: Callable[[bytes], None]) -> None:
        """Reject an incoming file transfer request.

        Call this from the ``on_transfer_request`` callback to indicate
        that the user does not want to receive the file.
        """
        with self._lock:
            transfer = self._transfers.pop(transfer_id, None)

        if transfer and transfer.get("temp_fh") is not None:
            try:
                transfer["temp_fh"].close()
            except Exception:
                pass
            _safe_remove(self._output_dir / f".{transfer_id}.part")

        self._send_as_frame(
            {"msg_type": "file_reject", "transfer_id": transfer_id},
            send_fn,
        )
        logger.info("Rejected file transfer: %s", transfer_id[:8])

    def handle_message(
        self,
        msg_type: str,
        payload: dict[str, Any],
        send_fn: Callable[[bytes], None],
    ) -> None:
        """Route an incoming file-transfer message to the correct handler.

        Parameters
        ----------
        msg_type:
            One of ``"file_request"``, ``"file_chunk"``, ``"file_ack"``,
            ``"file_reject"``, or ``"file_complete"``.
        payload:
            The fully-decoded JSON payload (the ``_raw_payload`` attribute
            from the decoded ``SyncMessage``).
        send_fn:
            Callable to send a response (typically ``TransportManager.broadcast``).
        """
        handler_map: dict[str, Callable] = {
            "file_request": self._handle_file_request,
            "file_chunk": self._handle_file_chunk,
            "file_ack": self._handle_file_ack,
            "file_reject": self._handle_file_reject,
            "file_complete": self._handle_file_complete,
            "file_chunk_ack": self._handle_file_chunk_ack,
            "file_pause": self._handle_file_pause,
            "file_resume": self._handle_file_resume,
            "speed_test_data": self.handle_speed_test_data,
            "speed_test_result": self.handle_speed_test_result,
        }
        handler = handler_map.get(msg_type)
        if handler is None:
            logger.debug("Unknown file transfer message type: %s", msg_type)
            return
        handler(payload, send_fn)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _send_as_frame(payload_dict: dict[str, Any], send_fn: Optional[Callable[[bytes], None]]) -> None:
        """JSON-encode *payload_dict*, wrap it in a binary frame, and call *send_fn*.

        If *send_fn* is ``None`` the frame is dropped (e.g. a web-triggered
        pause/resume with no transport callback); state changes are applied by
        the caller regardless.
        """
        if send_fn is None:
            logger.debug("Dropping %s frame (no send_fn)", payload_dict.get("msg_type"))
            return
        data = encode_frame(payload_dict)
        send_fn(data)

    # ------------------------------------------------------------------
    # Message handlers (receiver side)
    # ------------------------------------------------------------------

    def _handle_file_request(self, payload: dict, send_fn: Callable[[bytes], None]) -> None:
        transfer_id = str(payload.get("transfer_id", ""))
        raw_name = payload.get("file_name")
        if not isinstance(raw_name, str) or not raw_name:
            raw_name = "unknown"
        file_name = _sanitize_file_name(raw_name)
        mime_type = payload.get("mime_type", "application/octet-stream")
        if not isinstance(mime_type, str):
            mime_type = "application/octet-stream"

        # Validate/coerce file_size -- a malformed value must not crash the
        # message handler or slip an absurd file into the pipeline.
        raw_size = payload.get("file_size", 0)
        if isinstance(raw_size, bool) or not isinstance(raw_size, int):
            logger.warning("Invalid file_size in request for transfer %s: %r", transfer_id[:8], raw_size)
            self._send_as_frame({"msg_type": "file_reject", "transfer_id": transfer_id}, send_fn)
            return
        file_size = raw_size
        if file_size < 0 or file_size > MAX_FILE_SIZE:
            logger.warning(
                "Rejecting transfer %s: file_size %r outside allowed range",
                transfer_id[:8], file_size,
            )
            self._send_as_frame({"msg_type": "file_reject", "transfer_id": transfer_id}, send_fn)
            return

        logger.info(
            "Incoming file transfer request: %s (%s, %d bytes)",
            _mask_file_name(file_name), transfer_id[:8], file_size,
        )

        total_chunks = max((file_size + self.CHUNK_SIZE - 1) // self.CHUNK_SIZE, 1) if file_size > 0 else 1

        now = time.time()
        with self._lock:
            self._transfers[transfer_id] = {
                "transfer_id": transfer_id,
                "type": "incoming",
                "file_name": file_name,
                "file_size": file_size,
                "mime_type": mime_type,
                "total_chunks": total_chunks,
                "received_chunks": 0,
                "received_bytes": 0,
                "temp_fh": None,
                "state": "pending",
                "start_time": now,
                "_last_activity": now,
                # Set of chunk indices still missing; drained as chunks arrive.
                "chunks": set(range(total_chunks)),
            }

        if self._on_transfer_request is not None:
            self._on_transfer_request(transfer_id, file_name, file_size, mime_type, send_fn)
        else:
            # No UI callback registered -- auto-accept for headless operation
            logger.info("Auto-accepting transfer %s (no UI callback registered)", transfer_id[:8])
            self.accept_transfer(transfer_id, send_fn)

    def _handle_file_chunk(self, payload: dict, send_fn: Callable[[bytes], None]) -> None:
        transfer_id = str(payload.get("transfer_id", ""))
        chunk_index = payload.get("chunk_index", 0)
        total_chunks = payload.get("total_chunks", 0)
        b64_data = payload.get("data", "")

        # Validate chunk_index -- a malformed value must not raise inside the
        # message handler or corrupt the receive window.
        if isinstance(chunk_index, bool) or not isinstance(chunk_index, int) or chunk_index < 0:
            logger.warning("Invalid chunk_index for transfer %s: %r", transfer_id[:8], chunk_index)
            return

        with self._lock:
            transfer = self._transfers.get(transfer_id)
            if transfer is None:
                logger.debug("Chunk for unknown transfer: %s", transfer_id[:8])
                return
            state = transfer.get("state")
            if state not in ("receiving", "awaiting_retransmit"):
                logger.debug(
                    "Chunk for transfer in state %s: %s",
                    state, transfer_id[:8],
                )
                return
            # Receiver-side pause: drop chunk; sender will retransmit on resume
            if transfer.get("paused"):
                return

        # Binary frame path (no base64 overhead) — preferred for new clients
        raw_data = payload.get("_raw_data")
        if raw_data is not None:
            chunk_data = raw_data
        else:
            # Legacy base64 path for backward compatibility
            try:
                chunk_data = base64.b64decode(b64_data)
            except Exception:
                logger.warning("Invalid base64 in chunk %d for transfer %s", chunk_index, transfer_id[:8])
                return

        with self._lock:
            # Re-acquire -- transfer may have been removed while we were decoding
            transfer = self._transfers.get(transfer_id)
            if transfer is None:
                return
            state = transfer.get("state")
            if state not in ("receiving", "awaiting_retransmit"):
                return

            total = transfer.get("total_chunks", total_chunks)
            if chunk_index >= total:
                logger.warning(
                    "Chunk index %d out of range for transfer %s (total %d)",
                    chunk_index, transfer_id[:8], total,
                )
                return

            # Stream each chunk to the temp file as it arrives, keeping only a
            # sparse set of still-missing indices in memory (no whole-file RAM).
            missing = transfer["chunks"]
            temp_fh = transfer.get("temp_fh")
            if chunk_index in missing:
                if temp_fh is not None and not temp_fh.closed:
                    try:
                        temp_fh.seek(chunk_index * self.CHUNK_SIZE)
                        temp_fh.write(chunk_data)
                        missing.discard(chunk_index)
                        transfer["received_bytes"] += len(chunk_data)
                    except (OSError, ValueError) as exc:
                        # Keep the chunk marked missing so it is re-requested.
                        logger.error(
                            "Failed writing chunk %d for transfer %s: %s",
                            chunk_index, transfer_id[:8], exc,
                        )
                else:
                    logger.warning(
                        "No open temp file for chunk %d of transfer %s",
                        chunk_index, transfer_id[:8],
                    )
                transfer["received_chunks"] = total - len(missing)

            transfer["_last_activity"] = time.time()
            progress = transfer["received_chunks"] / max(total, 1)
            # Trigger finalization once all but at most one chunk have arrived.
            # This fires both when the LAST chunk is still missing (in flight)
            # and when a MIDDLE chunk is missing (gap) -- the retransmit path
            # requests anything that did not arrive.
            is_last = transfer["received_chunks"] >= max(total - 1, 0)

        if self._on_transfer_progress is not None:
            self._on_transfer_progress(transfer_id, progress)

        if is_last:
            self._finalize_received_file(transfer_id, transfer, total, send_fn)

    def _finalize_received_file(
        self,
        transfer_id: str,
        transfer: dict,
        total_chunks: int,
        send_fn: Callable[[bytes], None],
    ) -> None:
        """Write all received chunks in order, verify size, move to output dir."""
        file_name = transfer.get("file_name", "unknown")
        temp_fh = transfer.get("temp_fh")

        if temp_fh is None:
            logger.error("No open temp file for transfer %s", transfer_id[:8])
            with self._lock:
                self._transfers.pop(transfer_id, None)
            self._send_as_frame(
                {"msg_type": "file_complete", "transfer_id": transfer_id, "status": "error_internal"},
                send_fn,
            )
            if self._on_transfer_complete:
                self._on_transfer_complete(transfer_id, False)
            return

        # Guard against concurrent finalization from _handle_file_chunk and
        # _retransmit_wait racing on the same transfer.
        with self._lock:
            fresh = self._transfers.get(transfer_id)
            if fresh is None:
                return
            if fresh.get("_finalizing"):
                return
            fresh["_finalizing"] = True

        temp_path = self._output_dir / f".{transfer_id}.part"

        try:
            # Chunks are streamed to the temp file as they arrive; only the
            # still-missing indices remain in transfer["chunks"].
            missing_chunks = sorted(transfer.get("chunks") or set())

            if missing_chunks:
                ack_round = transfer.get("_ack_rounds", 0)
                if ack_round >= 3:
                    logger.error(
                        "Missing %d chunks after %d ACK rounds for transfer %s -- giving up",
                        len(missing_chunks), ack_round, transfer_id[:8],
                    )
                    temp_fh.close()
                    transfer["temp_fh"] = None
                    _safe_remove(temp_path)
                    with self._lock:
                        self._transfers.pop(transfer_id, None)
                    self._send_as_frame(
                        {"msg_type": "file_complete", "transfer_id": transfer_id, "status": "error_missing_chunks"},
                        send_fn,
                    )
                    if self._on_transfer_complete:
                        self._on_transfer_complete(transfer_id, False)
                    return

                logger.info(
                    "Transfer %s: %d/%d chunks missing, requesting retransmit (round %d)",
                    transfer_id[:8], len(missing_chunks), total_chunks, ack_round + 1,
                )
                transfer["_ack_rounds"] = ack_round + 1
                transfer["state"] = "awaiting_retransmit"
                transfer["_send_fn"] = send_fn
                self._send_as_frame(
                    {
                        "msg_type": "file_chunk_ack",
                        "transfer_id": transfer_id,
                        "missing_chunks": missing_chunks,
                    },
                    send_fn,
                )
                # Spawn retry thread to re-finalize after waiting for retransmitted chunks
                threading.Thread(
                    target=self._retransmit_wait,
                    args=(transfer_id, total_chunks),
                    daemon=True,
                    name=f"retransmit-wait-{transfer_id[:8]}",
                ).start()
                # Clear the finalizing guard so the retry thread can re-finalize.
                with self._lock:
                    fresh = self._transfers.get(transfer_id)
                    if fresh is not None:
                        fresh["_finalizing"] = False
                return  # retry thread will re-call _finalize_received_file

            temp_fh.close()
            transfer["temp_fh"] = None

            # Verify final file size matches what was advertised
            actual_size = temp_path.stat().st_size
            expected_size = transfer["file_size"]
            if actual_size != expected_size:
                logger.error(
                    "Size mismatch for transfer %s: expected %d, got %d",
                    transfer_id[:8], expected_size, actual_size,
                )
                _safe_remove(temp_path)
                with self._lock:
                    self._transfers.pop(transfer_id, None)
                self._send_as_frame(
                    {
                        "msg_type": "file_complete",
                        "transfer_id": transfer_id,
                        "status": "error_size_mismatch",
                    },
                    send_fn,
                )
                if self._on_transfer_complete:
                    self._on_transfer_complete(transfer_id, False)
                return

            # Move to final destination, avoiding name collisions
            dest_path = self._output_dir / _sanitize_file_name(file_name)
            if dest_path.resolve().parent != self._output_dir.resolve():
                logger.error("Path traversal blocked for transfer %s: %s", transfer_id[:8], file_name)
                _safe_remove(temp_path)
                with self._lock:
                    self._transfers.pop(transfer_id, None)
                self._send_as_frame(
                    {"msg_type": "file_complete", "transfer_id": transfer_id, "status": "error_security"},
                    send_fn,
                )
                if self._on_transfer_complete:
                    self._on_transfer_complete(transfer_id, False)
                return
            if dest_path.exists():
                stem = dest_path.stem
                suffix = dest_path.suffix
                counter = 1
                while dest_path.exists():
                    dest_path = self._output_dir / f"{stem} ({counter}){suffix}"
                    counter += 1

            os.rename(str(temp_path), str(dest_path))

            with self._lock:
                self._transfers.pop(transfer_id, None)

            self._send_as_frame(
                {"msg_type": "file_complete", "transfer_id": transfer_id, "status": "success"},
                send_fn,
            )
            saved = str(dest_path)
            logger.info("File received successfully: %s -> %s", _mask_file_name(file_name), _mask_path(saved))
            self._add_to_history(transfer, True, saved_path=saved)

            if self._on_file_received is not None:
                self._on_file_received(transfer_id, saved, file_name)
            if self._on_transfer_complete is not None:
                self._on_transfer_complete(transfer_id, True)

        except OSError as exc:
            logger.error("I/O error finalizing transfer %s: %s", transfer_id[:8], exc)
            if temp_fh is not None and not temp_fh.closed:
                try:
                    temp_fh.close()
                except Exception:
                    pass
            _safe_remove(temp_path)
            with self._lock:
                self._transfers.pop(transfer_id, None)
            self._send_as_frame(
                {"msg_type": "file_complete", "transfer_id": transfer_id, "status": "error_disk"},
                send_fn,
            )
            if self._on_transfer_complete:
                self._on_transfer_complete(transfer_id, False)

    def _retransmit_wait(self, transfer_id: str, total_chunks: int) -> None:
        """Wait for retransmitted chunks, then re-trigger finalization.

        Called from a daemon thread spawned by :meth:`_finalize_received_file`
        when chunks are missing.  Waits up to 30 s for all chunks to arrive,
        then re-calls finalization (which will send another ``file_chunk_ack``
        if chunks are still missing, up to 3 rounds).
        """
        RETRANSMIT_TIMEOUT = 30.0

        deadline = time.time() + RETRANSMIT_TIMEOUT
        send_fn = None
        while time.time() < deadline:
            with self._lock:
                transfer = self._transfers.get(transfer_id)
                if transfer is None:
                    return
                if transfer.get("received_chunks", 0) >= total_chunks:
                    send_fn = transfer.get("_send_fn")
                    break
            time.sleep(0.5)
        else:
            # Timeout — check one final time
            with self._lock:
                transfer = self._transfers.get(transfer_id)
                if transfer is None:
                    return
                send_fn = transfer.get("_send_fn")

        if send_fn is not None:
            with self._lock:
                transfer = self._transfers.get(transfer_id)
                if transfer is None:
                    return
                total = transfer.get("total_chunks", total_chunks)
            self._finalize_received_file(transfer_id, transfer, total, send_fn)

    # ------------------------------------------------------------------
    # Message handlers (sender side)
    # ------------------------------------------------------------------

    def _handle_file_ack(self, payload: dict, send_fn: Callable[[bytes], None]) -> None:
        transfer_id = payload.get("transfer_id", "")

        with self._lock:
            transfer = self._transfers.get(transfer_id)
            if transfer is None or transfer.get("type") != "outgoing":
                return
            if transfer.get("acked"):
                return  # chunks already being sent
            transfer["acked"] = True

        logger.info(
            "File transfer %s acknowledged by peer -- starting chunk send", transfer_id[:8],
        )

        stored_send_fn = transfer.get("_send_fn", send_fn)

        thread = threading.Thread(
            target=self._send_chunks,
            args=(transfer_id, stored_send_fn),
            daemon=True,
            name=f"file-xfer-{transfer_id[:8]}",
        )
        thread.start()

    def _handle_file_reject(self, payload: dict, send_fn: Callable[[bytes], None]) -> None:
        transfer_id = payload.get("transfer_id", "")

        with self._lock:
            transfer = self._transfers.pop(transfer_id, None)

        if transfer is not None and transfer.get("type") == "outgoing":
            logger.info(
                "File transfer %s rejected by peer (%s)",
                transfer_id[:8], _mask_file_name(transfer.get("file_name", "?")),
            )
            if self._on_transfer_complete is not None:
                self._on_transfer_complete(transfer_id, False)

    def _handle_file_complete(self, payload: dict, send_fn: Callable[[bytes], None]) -> None:
        transfer_id = payload.get("transfer_id", "")
        status = payload.get("status", "unknown")

        with self._lock:
            transfer = self._transfers.pop(transfer_id, None)

        if transfer is not None and transfer.get("type") == "outgoing":
            success = status == "success"
            self._add_to_history(transfer, success)
            logger.info(
                "File transfer %s %s (%s) -- status=%s",
                transfer_id[:8],
                "completed" if success else "failed",
                _mask_file_name(transfer.get("file_name", "?")),
                status,
            )
            if self._on_transfer_complete is not None:
                self._on_transfer_complete(transfer_id, success)

    def _handle_file_chunk_ack(self, payload: dict, send_fn: Callable[[bytes], None]) -> None:
        """Sender: receiver reports missing chunks → retransmit them."""
        transfer_id = payload.get("transfer_id", "")
        missing = payload.get("missing_chunks", [])
        with self._lock:
            transfer = self._transfers.get(transfer_id)
        if transfer is None or transfer.get("type") != "outgoing":
            return
        if not missing:
            return  # all good, nothing to retransmit
        transfer["_retransmit_queue"] = missing
        logger.info(
            "Transfer %s: receiver requests %d missing chunks",
            transfer_id[:8], len(missing),
        )

    def _handle_file_pause(self, payload: dict, _send_fn=None) -> None:
        """Receiver requests pause."""
        transfer_id = str(payload.get("transfer_id", ""))
        with self._lock:
            transfer = self._transfers.get(transfer_id)
            if transfer:
                transfer["paused"] = True
                transfer["_last_activity"] = time.time()  # paused transfers stay alive
                logger.info("Transfer %s paused by receiver", transfer_id[:8])

    def _handle_file_resume(self, payload: dict, _send_fn=None) -> None:
        """Receiver requests resume."""
        transfer_id = str(payload.get("transfer_id", ""))
        with self._lock:
            transfer = self._transfers.get(transfer_id)
            if transfer:
                transfer["paused"] = False
                transfer["_last_activity"] = time.time()
                logger.info("Transfer %s resumed by receiver", transfer_id[:8])

    def pause_transfer(self, transfer_id: str, send_fn: Callable[[bytes], None]) -> bool:
        """Pause this transfer (works for both sender and receiver).

        - Sender side: pauses the local send loop immediately.
        - Receiver side: sends ``file_pause`` to tell the remote sender to pause.
        """
        with self._lock:
            transfer = self._transfers.get(transfer_id)
            if transfer is None:
                return False
            transfer["paused"] = True
            transfer["_last_activity"] = time.time()  # paused transfers stay alive
            is_outgoing = transfer.get("type") == "outgoing"
        # Tell the other side to stop sending (only meaningful for receiver→sender)
        self._send_as_frame(
            {"msg_type": "file_pause", "transfer_id": transfer_id}, send_fn,
        )
        logger.info("Transfer %s paused (outgoing=%s)", transfer_id[:8], is_outgoing)
        return True

    def resume_transfer(self, transfer_id: str, send_fn: Callable[[bytes], None]) -> bool:
        """Resume this transfer (works for both sender and receiver).

        - Sender side: resumes the local send loop.
        - Receiver side: sends ``file_resume`` to tell the remote sender to resume.
        """
        with self._lock:
            transfer = self._transfers.get(transfer_id)
            if transfer is None:
                return False
            transfer["paused"] = False
            transfer["_last_activity"] = time.time()
            is_outgoing = transfer.get("type") == "outgoing"
        self._send_as_frame(
            {"msg_type": "file_resume", "transfer_id": transfer_id}, send_fn,
        )
        logger.info("Transfer %s resumed (outgoing=%s)", transfer_id[:8], is_outgoing)
        return True

    # ------------------------------------------------------------------
    # Chunked send logic (runs in background thread)
    # ------------------------------------------------------------------

    def _send_chunks(self, transfer_id: str, broadcast_fn: Callable[[bytes], None]) -> None:
        """Read the file and send all chunks (called from a background thread)."""
        with self._lock:
            transfer = self._transfers.get(transfer_id)
            if transfer is None:
                return
            file_path = transfer["file_path"]
            total_chunks = transfer["total_chunks"]
            file_name = transfer.get("file_name", "?")

        logger.info(
            "Sending %d chunks for transfer %s (%s)",
            total_chunks, transfer_id[:8], _mask_file_name(file_name),
        )

        # Mark state so get_transfers() shows progress / speed / ETA
        with self._lock:
            transfer = self._transfers.get(transfer_id)
            if transfer:
                transfer["state"] = "sending"

        MAX_RETRANSMIT_ROUNDS = 3

        def _send_one_chunk(fh, chunk_index: int, total: int, *, seek: bool = True) -> None:
            """Read + encode + send a single chunk from the open file handle.

            Uses the compact binary frame format (no base64/JSON overhead).
            Only seeks when *seek* is True (retransmit path); the sequential
            first pass lets the file pointer advance naturally.
            """
            if seek:
                fh.seek(chunk_index * self.CHUNK_SIZE)
            chunk_data = fh.read(self.CHUNK_SIZE)
            frame = encode_binary_chunk(transfer_id, chunk_index, total, chunk_data)
            broadcast_fn(frame)

        try:
            with open(file_path, "rb") as fh:
                # ---- first pass: send all chunks in order (no seek needed) ----
                for chunk_index in range(total_chunks):
                    # Pause check — wait while paused (with cancellation check)
                    while True:
                        with self._lock:
                            transfer = self._transfers.get(transfer_id)
                            if transfer is None or transfer.get("cancelled"):
                                logger.info(
                                    "Transfer %s cancelled mid-send", transfer_id[:8],
                                )
                                if self._on_transfer_complete is not None:
                                    self._on_transfer_complete(transfer_id, False)
                                return
                            if not transfer.get("paused"):
                                break
                        time.sleep(0.5)

                    _send_one_chunk(fh, chunk_index, total_chunks, seek=False)

                    progress = (chunk_index + 1) / total_chunks
                    bytes_sent = (chunk_index + 1) * self.CHUNK_SIZE
                    with self._lock:
                        t = self._transfers.get(transfer_id)
                        if t:
                            t["_last_progress"] = progress
                            t["_bytes_sent"] = min(bytes_sent, t.get("file_size", bytes_sent))
                            t["_last_activity"] = time.time()
                    if self._on_transfer_progress is not None:
                        self._on_transfer_progress(transfer_id, progress)

                # ---- retransmit rounds: resend missing chunks reported by receiver ----
                for round_num in range(MAX_RETRANSMIT_ROUNDS):
                    time.sleep(1.0)  # brief wait for file_chunk_ack to arrive

                    with self._lock:
                        transfer = self._transfers.get(transfer_id)
                        if transfer is None or transfer.get("cancelled"):
                            return
                        missing = transfer.pop("_retransmit_queue", None)

                    if not missing:
                        break

                    logger.info(
                        "Transfer %s retransmit round %d: %d missing chunks",
                        transfer_id[:8], round_num + 1, len(missing),
                    )

                    for chunk_index in missing:
                        while True:
                            with self._lock:
                                transfer = self._transfers.get(transfer_id)
                                if transfer is None or transfer.get("cancelled"):
                                    return
                                if not transfer.get("paused"):
                                    break
                            time.sleep(0.5)

                        _send_one_chunk(fh, chunk_index, total_chunks)

                        with self._lock:
                            t = self._transfers.get(transfer_id)
                            if t:
                                t["_last_activity"] = time.time()

        except Exception as exc:
            logger.error(
                "Failed sending chunks for transfer %s (%s): %s",
                transfer_id[:8], file_name, exc,
            )
            with self._lock:
                self._transfers.pop(transfer_id, None)
            if self._on_transfer_complete is not None:
                self._on_transfer_complete(transfer_id, False)
            return

        logger.info(
            "All %d chunks sent for transfer %s -- waiting for FILE_COMPLETE",
            total_chunks, transfer_id[:8],
        )

        # Wait for FILE_COMPLETE from the receiver (with timeout)
        deadline = time.time() + COMPLETION_WAIT_TIMEOUT
        while time.time() < deadline:
            with self._lock:
                if transfer_id not in self._transfers:
                    return
            time.sleep(0.5)

        with self._lock:
            stale = self._transfers.pop(transfer_id, None)
        if stale is not None:
            logger.warning(
                "File transfer %s timed out waiting for FILE_COMPLETE", transfer_id[:8],
            )
            if self._on_transfer_complete is not None:
                self._on_transfer_complete(transfer_id, False)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_transfers(self) -> list[dict]:
        """Return a snapshot of active transfers for UI display.

        Each dict contains:
          ``transfer_id``, ``file_name``, ``file_size``, ``direction``,
          ``state``, ``progress`` (0.0–1.0), ``speed_bytes_per_sec``,
          ``eta_seconds``.
        """
        result: list[dict] = []
        now = time.time()
        with self._lock:
            for tid, t in self._transfers.items():
                direction = "up" if t.get("type") == "outgoing" else "down"
                state = t.get("state", "unknown")
                total = max(t.get("total_chunks", 1), 1)
                file_size = t.get("file_size", 0)
                if t.get("type") == "incoming":
                    progress = t.get("received_chunks", 0) / total
                    bytes_done = t.get("received_bytes", 0)
                elif state == "awaiting_ack":
                    progress = 0.0
                    bytes_done = 0
                else:
                    # outgoing: track last known chunk progress
                    progress = t.get("_last_progress", 0.0)
                    bytes_done = int(progress * file_size) if file_size else 0
                elapsed = now - t.get("start_time", now)
                speed = bytes_done / elapsed if elapsed > 0.5 and bytes_done > 0 else 0.0
                remaining = file_size - bytes_done
                eta = remaining / speed if speed > 0 and remaining > 0 else 0.0
                result.append({
                    "transfer_id": tid,
                    "file_name": t.get("file_name", "?"),
                    "file_size": file_size,
                    "direction": direction,
                    "state": state,
                    "progress": min(progress, 1.0),
                    "speed_bytes_per_sec": speed,
                    "eta_seconds": eta,
                    "paused": t.get("paused", False),
                })
        return result

    def get_history(self) -> list[dict]:
        """Return completed transfer history (newest first)."""
        with self._lock:
            return list(self._history)

    def clear_history(self) -> None:
        """Delete all transfer history entries."""
        with self._lock:
            self._history.clear()

    def delete_history_item(self, entry: dict) -> bool:
        """Remove a single history entry. Returns True if deleted."""
        with self._lock:
            try:
                self._history.remove(entry)
                return True
            except ValueError:
                return False

    def get_speed_test(self) -> dict | None:
        """Return current speed test state, if any."""
        with self._lock:
            return dict(self._speed_test) if self._speed_test else None

    def _add_to_history(self, transfer: dict, success: bool, saved_path: str = ""):
        """Record a completed transfer in the history list."""
        entry = {
            "transfer_id": transfer.get("transfer_id", uuid.uuid4().hex),
            "file_name": transfer.get("file_name", "?"),
            "file_size": transfer.get("file_size", 0),
            "direction": "up" if transfer.get("type") == "outgoing" else "down",
            "success": success,
            "state": transfer.get("state", "unknown"),
            "source_path": transfer.get("file_path", ""),
            "saved_path": saved_path,
            "timestamp": time.time(),
        }
        with self._lock:
            self._history.insert(0, entry)
            if len(self._history) > MAX_HISTORY:
                self._history = self._history[:MAX_HISTORY]

    # ------------------------------------------------------------------
    # Speed Test
    # ------------------------------------------------------------------

    def start_speed_test(self, broadcast_fn: Callable[[bytes], None]) -> str | None:
        """Start a speed test to measure network throughput between peers.

        Sends a burst of dummy data and measures the time until the peer
        echoes back a result. Returns a transfer_id for tracking, or None
        if no peer is connected.
        """
        test_id = uuid.uuid4().hex
        start_time = time.time()
        with self._lock:
            self._speed_test = {
                "test_id": test_id,
                "state": "sending",
                "start_time": start_time,
                "chunks_sent": 0,
                "total_chunks": SPEED_TEST_CHUNKS,
                "result_mbps": 0.0,
            }

        # Send test chunks in a background thread
        thread = threading.Thread(
            target=self._run_speed_test,
            args=(test_id, broadcast_fn),
            daemon=True,
            name=f"speed-test-{test_id[:8]}",
        )
        thread.start()
        return test_id

    def _run_speed_test(self, test_id: str, broadcast_fn: Callable[[bytes], None]):
        import secrets as _secrets
        dummy = base64.b64encode(_secrets.token_bytes(CHUNK_SIZE)).decode("ascii")
        total_bytes = SPEED_TEST_CHUNKS * CHUNK_SIZE
        start = time.time()

        for i in range(SPEED_TEST_CHUNKS):
            with self._lock:
                if self._speed_test is None or self._speed_test.get("test_id") != test_id:
                    return
            self._send_as_frame(
                {
                    "msg_type": "speed_test_data",
                    "test_id": test_id,
                    "chunk_index": i,
                    "total_chunks": SPEED_TEST_CHUNKS,
                    "data": dummy,
                },
                broadcast_fn,
            )
            with self._lock:
                if self._speed_test:
                    self._speed_test["chunks_sent"] = i + 1
            time.sleep(0.002)  # minimal delay between chunks

        elapsed = time.time() - start
        mbps = (total_bytes / (1024 * 1024)) / elapsed if elapsed > 0 else 0.0
        with self._lock:
            if self._speed_test:
                self._speed_test["state"] = "done"
                self._speed_test["result_mbps"] = round(mbps, 2)
        logger.info("Speed test %s complete: %.2f MB/s", test_id[:8], mbps)

    def handle_speed_test_data(self, payload: dict, send_fn: Callable[[bytes], None]):
        """Receiver side: echo back speed test data as result."""
        test_id = payload.get("test_id", "")
        chunk_index = payload.get("chunk_index", 0)
        total_chunks = payload.get("total_chunks", 0)
        # On the last chunk, send a result back
        if chunk_index >= total_chunks - 1:
            self._send_as_frame(
                {
                    "msg_type": "speed_test_result",
                    "test_id": test_id,
                },
                send_fn,
            )

    def handle_speed_test_result(self, payload: dict, _send_fn=None):
        """Sender side: peer acknowledged speed test."""
        test_id = payload.get("test_id", "")
        with self._lock:
            if self._speed_test and self._speed_test.get("test_id") == test_id:
                if self._speed_test["state"] == "sending":
                    # Mark as done; the sending thread will finalize
                    self._speed_test["state"] = "acknowledged"

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def cleanup_stale_transfers(self) -> None:
        """Remove transfers that have exceeded ``TRANSFER_TIMEOUT``.

        Uses ``_last_activity`` (updated on each chunk sent/received) so
        actively progressing transfers are not killed mid-flight.  Call
        this periodically (e.g. every 30 s) to prevent memory leaks from
        abandoned transfers. Partial temp files are deleted.
        """
        now = time.time()
        with self._lock:
            stale_ids = [
                tid for tid, t in self._transfers.items()
                if not t.get("paused")
                and now - t.get("_last_activity", t.get("start_time", 0)) > self._transfer_timeout
            ]

        for tid in stale_ids:
            with self._lock:
                transfer = self._transfers.pop(tid, None)
            if transfer is None:
                continue

            if transfer.get("temp_fh") is not None:
                try:
                    transfer["temp_fh"].close()
                except Exception:
                    pass
            _safe_remove(self._output_dir / f".{tid}.part")

            logger.info(
                "Cleaned up stale transfer %s (%s)",
                tid[:8], _mask_file_name(transfer.get("file_name", "?")),
            )
            if self._on_transfer_complete is not None:
                self._on_transfer_complete(tid, False)
