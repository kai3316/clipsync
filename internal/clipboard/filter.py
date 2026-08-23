"""Sensitive content filtering for clipboard data."""

import re

from internal.clipboard.format import ClipboardContent, ContentType

# ---------------------------------------------------------------------------
# Compiled regex patterns, grouped by sensitivity category.
# ---------------------------------------------------------------------------

_CREDIT_CARD_RE = re.compile(
    r'\b'
    r'(?:'
    # Visa: 13 or 16 digits, starts with 4
    r'4\d{3}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}'      # 16 digits
    r'|4\d{3}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{1}'      # 13 digits
    # MasterCard: 16 digits, starts with 51-55 or 2221-2720
    r'|5[1-5]\d{2}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}'
    r'|2(?:2[2-9]\d|[3-6]\d{2}|7[01]\d|720)'
    r'[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}'
    # Amex: 15 digits, starts with 34 or 37
    r'|3[47]\d{2}[-\s]?\d{6}[-\s]?\d{5}'
    # Discover: 16-19 digits, starts with 6011, 65, or 644-649
    r'|6(?:011|5\d{2})[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}'  # 16 digits
    r'|6(?:011|5\d{2})[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{1,3}'  # 17-19
    r')\b'
)

_SSN_RE = re.compile(r'\b\d{3}-\d{2}-\d{4}\b')

_API_KEY_RE = re.compile(
    r'(?:'
    # OpenAI / Stripe style: sk-...
    r'sk-[a-zA-Z0-9_\-]{16,}'
    # GitHub tokens
    r'|gh[pousr]_[a-zA-Z0-9]{16,}'
    r'|github_pat_[a-zA-Z0-9_]{16,}'
    # Slack tokens
    r'|xox[baprs]-[a-zA-Z0-9\-]{16,}'
    # AWS access key id
    r'|AKIA[0-9A-Z]{16}'
    # JWT / session tokens (three base64url segments)
    r'|eyJ[a-zA-Z0-9_\-]{8,}\.[a-zA-Z0-9_\-]{8,}\.[a-zA-Z0-9_\-]{8,}'
    # name=value / name:value secret assignments
    r'|(?:api[_-]?key|access[_-]?key|auth[_-]?token|client[_-]?secret|\bsecret|\btoken|\bkey)\s*[=:]\s*["\']?\s*[a-zA-Z0-9_\-\.]{16,}["\']?'
    # Authorization / Bearer headers
    r'|authorization\s*[=:]\s*(?:Bearer\s+)?[a-zA-Z0-9_\-\.]{16,}'
    r'|Bearer\s+[a-zA-Z0-9_\-\.]{16,}'
    # Generic key- prefix (e.g. key-...)
    r'|key-[a-zA-Z0-9_\-]{16,}'
    r')',
    re.IGNORECASE,
)

_PRIVATE_KEY_RE = re.compile(
    r'-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----'
    r'[\s\S]*?'
    r'-----END\s+(?:RSA\s+)?PRIVATE\s+KEY-----',
)

_PASSWORD_RE = re.compile(
    r'\b(?:password|passwd|pwd)\s*[=:]\s*["\']?\s*\S+["\']?',
    re.IGNORECASE,
)

# Bare email addresses (PII caught even without a key= prefix).
_EMAIL_RE = re.compile(
    r'\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b',
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


ALL_CATEGORIES = ["credit_card", "ssn", "api_key", "private_key", "password"]

CATEGORY_LABELS: dict[str, str] = {
    "credit_card": "Credit card numbers",
    "ssn": "Social Security numbers (XXX-XX-XXXX)",
    "api_key": "API keys & tokens (sk-*, Bearer, key-*)",
    "private_key": "Private key blocks (PEM)",
    "password": "Password-like patterns (password=...)",
}

# The literal placeholder that replaces redacted sensitive content in
# synced clipboard text.  Keep the string in sync with the sub pattern in
# ``ContentFilter.filter_content`` below.
FILTERED_MARKER = "[FILTERED]"


def _luhn_valid(number: str) -> bool:
    """Return True if *number* passes the Luhn checksum.

    A credit-card-shaped digit string is only a real card if it also passes
    Luhn — a 16-digit order/invoice number must not be redacted as a card.
    """
    digits = [int(c) for c in number if c.isdigit()]
    if len(digits) < 13:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def is_filtered_text(text: str) -> bool:
    """Return True if *text* contains the ``[FILTERED]`` marker.

    Lets the UI explain a redacted clip to the user ("some sensitive content
    was replaced") instead of showing a bare marker with no context.  Both the
    desktop and web views can call this before rendering a received clip.
    """
    return bool(text) and FILTERED_MARKER in text


class ContentFilter:
    """Detect and optionally strip sensitive content from clipboard data.

    The filter operates on text-based clipboard formats (TEXT and HTML).
    Binary formats (IMAGE_PNG, RTF) are left untouched.
    """

    def __init__(self, enabled_categories: list[str] | None = None) -> None:
        self._all_patterns: list[tuple[str, re.Pattern]] = [
            ("credit_card", _CREDIT_CARD_RE),
            ("ssn", _SSN_RE),
            ("api_key", _API_KEY_RE),
            # Bare emails are their own opt-in category, NOT part of the
            # default set: everyday text (signatures, pasted correspondence)
            # is full of addresses, and redacting them by default corrupted
            # far more legitimate clips on the receiving end than it
            # protected anything.  Enable "email" explicitly to opt in.
            ("email", _EMAIL_RE),
            ("private_key", _PRIVATE_KEY_RE),
            ("password", _PASSWORD_RE),
        ]
        # Redaction is ON by default: None (unconfigured) enables every
        # category except the opt-in "email" one, so fresh installs filter
        # genuinely sensitive content out of the box.  A non-empty list
        # enables just that subset; an EMPTY list means the user explicitly
        # disabled redaction (distinct from the None default).
        if enabled_categories is None:
            self._enabled = [c for c in dict.fromkeys(c for c, _ in self._all_patterns)
                             if c != "email"]
        else:
            self._enabled = list(enabled_categories)

    @property
    def enabled_categories(self) -> list[str]:
        return list(self._enabled)

    @enabled_categories.setter
    def enabled_categories(self, categories: list[str] | None) -> None:
        if categories is None:
            # Not configured → every category except opt-in "email" (default).
            self._enabled = [c for c in dict.fromkeys(c for c, _ in self._all_patterns)
                             if c != "email"]
        else:
            self._enabled = list(categories)

    @property
    def is_active(self) -> bool:
        return len(self._enabled) > 0

    def _active_patterns(self) -> list[tuple[str, re.Pattern]]:
        return [(c, p) for c, p in self._all_patterns if c in self._enabled]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _bytes_to_str(data: bytes) -> str:
        """Decode bytes to string, falling back to latin-1 on failure."""
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("latin-1")

    @staticmethod
    def _textual_types() -> tuple[ContentType, ...]:
        """Return the content types that carry text (filterable)."""
        return (ContentType.TEXT, ContentType.HTML)

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def is_sensitive(self, content: ClipboardContent) -> bool:
        """Check if clipboard content contains sensitive data.

        Only TEXT and HTML types are inspected.  Only enabled categories
        are checked.  Returns True as soon as any pattern matches.
        """
        if not self._enabled:
            return False
        for ct in self._textual_types():
            data = content.types.get(ct)
            if data is None:
                continue
            text = self._bytes_to_str(data)
            for _category, pattern in self._active_patterns():
                if _category == "credit_card":
                    if any(_luhn_valid(m.group(0)) for m in pattern.finditer(text)):
                        return True
                elif pattern.search(text):
                    return True
        return False

    def describe_sensitivity(self, content: ClipboardContent) -> list[str]:
        """Return a deduplicated list of matched sensitivity category names."""
        matched: list[str] = []
        for ct in self._textual_types():
            data = content.types.get(ct)
            if data is None:
                continue
            text = self._bytes_to_str(data)
            for category, pattern in self._active_patterns():
                if category in matched:
                    continue
                if category == "credit_card":
                    if any(_luhn_valid(m.group(0)) for m in pattern.finditer(text)):
                        matched.append(category)
                elif pattern.search(text):
                    matched.append(category)
        return matched

    # ------------------------------------------------------------------
    # Sanitisation
    # ------------------------------------------------------------------

    def filter_content(self, content: ClipboardContent) -> ClipboardContent:
        """Return a sanitized copy of *content*.

        Every match of every enabled pattern in TEXT and HTML content is
        replaced with ``[FILTERED]``.
        """
        filtered_types: dict[ContentType, bytes] = {}

        for ct in self._textual_types():
            data = content.types.get(ct)
            if data is None:
                continue
            text = self._bytes_to_str(data)
            for _category, pattern in self._active_patterns():
                if _category == "credit_card":
                    text = pattern.sub(
                        lambda m: FILTERED_MARKER if _luhn_valid(m.group(0)) else m.group(0),
                        text,
                    )
                else:
                    text = pattern.sub(FILTERED_MARKER, text)
            filtered_types[ct] = text.encode("utf-8")

        for ct, data in content.types.items():
            if ct not in self._textual_types():
                filtered_types[ct] = data

        return ClipboardContent(
            types=filtered_types,
            source_device=content.source_device,
            timestamp=content.timestamp,
            # Preserve the image format hint: without it the image bytes are
            # re-labelled as PNG (and zlib-compressed) on the wire, so a
            # BMP/TIFF copy gets corrupted on Linux/macOS receivers.
            image_fmt=content.image_fmt,
        )
