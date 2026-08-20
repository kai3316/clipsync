"""SHA256 content hashing for clipboard deduplication."""

import hashlib

from internal.clipboard.format import ClipboardContent


def content_hash(content: ClipboardContent) -> str:
    """Compute SHA256 hash of clipboard content for dedup."""
    h = hashlib.sha256()
    # Hash all format types and their data for complete dedup
    for fmt in sorted(content.types.keys(), key=lambda f: f.name):
        h.update(fmt.name.encode())
        h.update(content.types[fmt])
    return h.hexdigest()
