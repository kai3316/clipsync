"""Multi-round retry capture for clipboard content.

Some applications (Office, Photoshop, etc.) write clipboard data in multiple
batches. A single immediate capture may miss formats that arrive later.
This module retries capture with increasing delays to catch late-arriving data.
"""

import logging
import time

from internal.clipboard.format import ContentType

logger = logging.getLogger(__name__)

# 7-round retry delays matching QuickClipboard's approach
RETRY_DELAYS_MS = [0, 40, 80, 140, 220, 360, 560]

# Plain text settles in a round or two.  Only rich formats (images, HTML,
# RTF) are written by apps in multiple batches and need the full retry
# window — reading (and re-hashing) them 7 times is wasteful for text.
_TEXT_ONLY_ROUNDS = 2

_RICH_FORMATS = (ContentType.IMAGE_PNG, ContentType.IMAGE_EMF,
                 ContentType.HTML, ContentType.RTF)


def _is_rich(content) -> bool:
    """True if the content carries formats that may arrive late."""
    return any(fmt in content.types for fmt in _RICH_FORMATS)


def capture_with_retry(reader, max_rounds: int = 7):
    """Capture clipboard content with multi-round retry.

    Args:
        reader: A ClipboardReader instance
        max_rounds: Number of retry rounds (1-7)

    Returns:
        ClipboardContent from the last successful capture

    Rounds are capped by content type: plain text stabilizes in a round
    or two (so we stop early instead of spawning 7 subprocess-heavy
    reads for every text copy), while images and rich formats get the
    full retry window because apps write those in multiple batches.
    """
    max_rounds = min(max_rounds, len(RETRY_DELAYS_MS))
    content = None
    prev_hash = None
    for i in range(max_rounds):
        delay_ms = RETRY_DELAYS_MS[i]
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)

        new_content = reader.read()
        if not new_content or not new_content.types:
            # Empty read — keep retrying for the current round budget.
            continue

        from internal.clipboard.dedup import content_hash
        new_hash = content_hash(new_content)

        if not _is_rich(new_content):
            # Text-only content: stop after a quick stability check.
            if content is not None and new_hash == prev_hash:
                logger.debug("Clipboard text stabilized after %d round(s)", i + 1)
                return content
            if i + 1 >= _TEXT_ONLY_ROUNDS:
                return new_content
            prev_hash = new_hash
            content = new_content
            continue

        # Rich content: keep retrying until it stabilizes.
        if new_hash == prev_hash and content is not None:
            logger.debug("Clipboard content stabilized after %d round(s)", i + 1)
            return content

        prev_hash = new_hash
        content = new_content

    if content:
        logger.debug("Clipboard capture completed after %d round(s)", min(max_rounds, len(RETRY_DELAYS_MS)))
    return content
