"""Content hashing and flavor merging for clipboard deduplication.

Two distinct mechanisms live under the "dedup" umbrella:

- ``content_hash()`` — exact-content hashing (every format byte counts).
  The sync manager uses it for loop prevention: a hash that was recently
  broadcast/received is suppressed so a clip never ping-pongs between
  peers.  See ``DEDUP_RING_TTL`` in ``internal/sync/manager.py``.

- :func:`merge_types` / the history-level text-body keys — same-text
  recognition for *history* records.  Multi-step clipboard writes and
  plain re-copies of an already-captured text must not stack duplicate
  entries, and a poorer variant (plain text) must never overwrite the
  richer one (HTML/RTF) already stored.
"""

import base64
import hashlib
import time

from internal.clipboard.format import ClipboardContent, ContentType


def content_hash(content: ClipboardContent) -> str:
    """Compute the canonical content hash for dedup.

    Thin wrapper over ``ClipboardContent.hash_key()`` so every caller
    shares a single, stable hash implementation (image and FILE/URL
    content included) instead of maintaining a parallel one.
    """
    return content.hash_key()


# Dedup hash algorithm for history text-body keys, wired from
# cfg.dedup_method ("sha256" default, or "simple" for a faster md5).
# Set at startup by the application; both history backends read it here.
DEDUP_ALGO = "sha256"

# Config value → hashlib algorithm name ("simple" is the fast path).
_DEDUP_ALGO_MAP = {"sha256": "sha256", "simple": "md5"}


def make_dedup_key(content: ClipboardContent) -> str:
    """Build a stable dedup key from the 'primary' content.

    Uses the text body (when available) rather than hashing all types,
    so multi-step clipboard writes (TEXT → HTML → RTF) that produce
    different ``hash_key()`` values are still recognised as the same
    user action.  Falls back to image-data hashes for image-only copies.
    Honours ``DEDUP_ALGO`` (sha256 / simple=md5) so both history backends
    coalesce identically for a given config.
    """
    _h = lambda data: hashlib.new(_DEDUP_ALGO_MAP.get(DEDUP_ALGO, "sha256"), data).hexdigest()
    if ContentType.TEXT in content.types:
        text = content.types[ContentType.TEXT].decode("utf-8", errors="replace")
        # Hash the full body so two long texts sharing a prefix are not
        # wrongly coalesced within the dedup window.
        return "text:" + _h(text.encode("utf-8", errors="replace"))
    if ContentType.IMAGE_PNG in content.types:
        return "png:" + _h(content.types[ContentType.IMAGE_PNG])
    if ContentType.IMAGE_EMF in content.types:
        return "emf:" + _h(content.types[ContentType.IMAGE_EMF])
    if ContentType.HTML in content.types:
        return "html:" + _h(content.types[ContentType.HTML])
    if ContentType.RTF in content.types:
        return "rtf:" + _h(content.types[ContentType.RTF])
    if ContentType.FILE in content.types:
        # FILE content is the newline-joined file paths — hash them so
        # file-only copies dedup instead of falling through to a unique key.
        return "file:" + _h(content.types[ContentType.FILE])
    if ContentType.URL in content.types:
        return "url:" + _h(content.types[ContentType.URL])
    return "other:" + str(time.time())


# Canonical persistence labels for content types.  Stored entries and the
# backup/restore schema serialize format keys under these labels, and both
# history backends decode through them, so this is the single source of
# truth for the persisted (non-wire) label set.  NOTE: the wire protocol
# (codec.py) labels image formats "IMAGE_PNG" — that one is protocol-fixed
# for cross-version compatibility and must NOT be aligned to "IMAGE".
CONTENT_TYPE_LABELS: dict[ContentType, str] = {
    ContentType.TEXT: "TEXT",
    ContentType.HTML: "HTML",
    ContentType.RTF: "RTF",
    ContentType.IMAGE_PNG: "IMAGE",
    ContentType.IMAGE_EMF: "IMAGE_EMF",
    ContentType.FILE: "FILE",
    ContentType.URL: "URL",
}
LABEL_TYPE_MAP: dict[str, ContentType] = {
    label: ct for ct, label in CONTENT_TYPE_LABELS.items()
}


def labels_to_types(labels: dict | None) -> dict[ContentType, bytes]:
    """Decode a persisted entry's base64 ``types`` map to ContentType->bytes.

    Corrupt or unknown labels are skipped rather than raising — a single
    bad entry must not break the merge path for every future capture.
    """
    out: dict[ContentType, bytes] = {}
    if not isinstance(labels, dict):
        return out
    for label, b64 in labels.items():
        ct = LABEL_TYPE_MAP.get(label)
        if ct is None:
            continue
        try:
            out[ct] = base64.b64decode(b64)
        except Exception:
            continue
    return out


def types_to_labels(types: dict[ContentType, bytes]) -> dict[str, str]:
    """Encode a ContentType->bytes map into the persisted label->base64 form."""
    inverse = {ct: label for label, ct in LABEL_TYPE_MAP.items()}
    return {
        inverse[ct]: base64.b64encode(data).decode("ascii")
        for ct, data in types.items()
        if ct in inverse
    }


def merge_types(existing: dict[ContentType, bytes],
                incoming: dict[ContentType, bytes]
                ) -> tuple[dict[ContentType, bytes], bool]:
    """Union two format maps of the SAME logical clip (same text body).

    Per format, the incoming capture wins when both sides carry it — those
    bytes reflect what is on the clipboard right now — while formats only
    the existing entry has survive untouched.  That is what keeps a rich
    flavor from being downgraded: re-copying the plain-text variant of a
    rich clip merges into the existing entry and its HTML/RTF survives,
    and a late-arriving HTML write upgrades an entry first captured as
    plain text instead of being dropped as a "duplicate".

    Returns ``(merged_types, changed)``.
    """
    merged = dict(existing)
    changed = False
    for ct, data in incoming.items():
        if merged.get(ct) != data:
            changed = True
        merged[ct] = data
    return merged, changed


def adds_new_flavors(existing_labels: dict | None,
                     incoming: dict[ContentType, bytes]) -> bool:
    """True if *incoming* carries any format the stored entry lacks.

    Used on the tight coalesce path: a repeat capture that adds nothing
    (same formats, same bytes) is a genuine duplicate and stays dropped;
    one that brings a new format (e.g. HTML written a beat later) must be
    merged so the flavor is not lost.
    """
    have = {LABEL_TYPE_MAP.get(l) for l in (existing_labels or {})}
    return any(ct not in have for ct in incoming)
