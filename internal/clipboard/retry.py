"""Multi-round retry capture for clipboard content.

Some applications (Office, Photoshop, etc.) write clipboard data in multiple
batches. A single immediate capture may miss formats that arrive later.
This module retries capture with increasing delays to catch late-arriving data.
"""

import time
import logging

logger = logging.getLogger(__name__)

# 7-round retry delays matching QuickClipboard's approach
RETRY_DELAYS_MS = [0, 40, 80, 140, 220, 360, 560]


def capture_with_retry(reader, max_rounds: int = 7):
    """Capture clipboard content with multi-round retry.

    Args:
        reader: A ClipboardReader instance
        max_rounds: Number of retry rounds (1-7)

    Returns:
        ClipboardContent from the last successful capture
    """
    content = None
    prev_hash = None
    for i in range(min(max_rounds, len(RETRY_DELAYS_MS))):
        delay_ms = RETRY_DELAYS_MS[i]
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)

        new_content = reader.read()
        if not new_content or not new_content.types:
            continue

        # Check if content stabilized (same hash across two reads)
        from internal.clipboard.dedup import content_hash
        new_hash = content_hash(new_content)
        if new_hash == prev_hash and content is not None:
            logger.debug("Clipboard content stabilized after %d round(s)", i + 1)
            return content

        prev_hash = new_hash
        content = new_content

    if content:
        logger.debug("Clipboard capture completed after %d round(s)", min(max_rounds, len(RETRY_DELAYS_MS)))
    return content
