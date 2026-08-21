"""SHA256 content hashing for clipboard deduplication."""

from internal.clipboard.format import ClipboardContent


def content_hash(content: ClipboardContent) -> str:
    """Compute the canonical content hash for dedup.

    Thin wrapper over ``ClipboardContent.hash_key()`` so every caller
    shares a single, stable hash implementation (image and FILE/URL
    content included) instead of maintaining a parallel one.
    """
    return content.hash_key()
