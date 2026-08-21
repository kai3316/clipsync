"""ClipSync translation endpoint — LibreTranslate-compatible proxy with a
free anonymous fallback.

Two modes:

* **Configured** — ``cfg.translate_url`` / ``cfg.translate_api_key`` point
  at a LibreTranslate-compatible instance (official, self-hosted, or a
  key-protected mirror).  The API key, when set, is sent as
  ``Authorization: Bearer <key>``.
* **Free fallback** — when neither is configured the public LibreTranslate
  instances now demand an API key, so we transparently fall back to MyMemory
  (https://api.mymemory.translated.net), which works anonymously.  MyMemory
  can't auto-detect the source language, so for ``source=auto`` we guess it
  from Unicode blocks.

Uses only stdlib (urllib + json) — zero dependencies.
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

LIBRETRANSLATE_URL = "https://libretranslate.com/translate"
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
REQUEST_TIMEOUT = 15  # seconds

SUPPORTED_LANGUAGES = {
    "auto": "Auto-detect",
    "en": "English",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "pt": "Portuguese",
    "ru": "Russian",
    "ar": "Arabic",
    "hi": "Hindi",
}

# LibreTranslate-style codes → MyMemory codes.  MyMemory accepts most ISO
# 639-1 codes as-is; only codes that differ need a mapping.
_MYMMEMORY_LANGMAP = {
    "zh": "zh-CN",
    "pt": "pt-PT",
}


def translate_text(
    text: str,
    target_lang: str = "en",
    source_lang: str = "auto",
    cfg=None,
) -> dict:
    """Translate text using the configured provider or a free fallback.

    Args:
        text: The text to translate.
        target_lang: Target language code (default 'en').
        source_lang: Source language code (default 'auto').
        cfg: Optional Config; ``translate_url`` / ``translate_api_key`` are
            read from it when present.

    Returns:
        dict with keys: ok, translated, source_lang, target_lang.
        On error: ok=False with an error message.
    """
    if not text or not text.strip():
        return {"ok": False, "error": "No text provided"}

    # Validate and sanitise language codes
    if source_lang not in SUPPORTED_LANGUAGES:
        source_lang = "auto"
    if target_lang not in SUPPORTED_LANGUAGES or target_lang == "auto":
        target_lang = "en"

    text = text.strip()

    url = (getattr(cfg, "translate_url", "") or "").strip() or LIBRETRANSLATE_URL
    api_key = (getattr(cfg, "translate_api_key", "") or "").strip()

    # Unconfigured → the free anonymous path.  The official public instance
    # now requires an API key, so "free" means the MyMemory fallback.
    if url == LIBRETRANSLATE_URL and not api_key:
        return _translate_mymemory(text, target_lang, source_lang)

    return _translate_libretranslate(url, api_key, text, target_lang, source_lang)


def _translate_libretranslate(
    url: str, api_key: str, text: str, target_lang: str, source_lang: str
) -> dict:
    """POST to a LibreTranslate-compatible endpoint."""
    text = text[:5000]
    request_body = json.dumps(
        {
            "q": text,
            "source": source_lang,
            "target": target_lang,
            "format": "text",
        }
    ).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if api_key:
        headers["Authorization"] = "Bearer " + api_key

    try:
        req = urllib.request.Request(url, data=request_body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        translated = result.get("translatedText", "")
        detected_lang = result.get("detectedLanguage", {})
        if isinstance(detected_lang, dict):
            src_lang = detected_lang.get("language", source_lang)
        else:
            src_lang = source_lang

        logger.info(
            "Translated %d chars %s → %s (LibreTranslate)", len(text), src_lang, target_lang
        )

        return {
            "ok": True,
            "translated": translated,
            "source_lang": src_lang,
            "target_lang": target_lang,
        }

    except urllib.error.HTTPError as e:
        logger.warning("LibreTranslate HTTP error: %s %s", e.code, e.reason)
        error_msg = "Translation service unavailable"
        try:
            error_body = e.read()
            err_data = json.loads(error_body.decode("utf-8", errors="replace"))
            error_msg = err_data.get("error", error_msg)
        except Exception:
            pass
        return {"ok": False, "error": error_msg}

    except urllib.error.URLError as e:
        logger.warning("LibreTranslate connection error: %s", e.reason)
        return {
            "ok": False,
            "error": (
                "Translation service is not reachable. "
                "Check your internet connection."
            ),
        }

    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning("LibreTranslate response parse error: %s", e)
        return {"ok": False, "error": "Invalid response from translation service."}

    except Exception as e:
        logger.error("Unexpected translation error: %s", e)
        return {"ok": False, "error": "Translation failed. Please try again later."}


def _translate_mymemory(text: str, target_lang: str, source_lang: str) -> dict:
    """Translate via the free MyMemory API (no key required).

    MyMemory's langpair needs an explicit source language, so ``auto`` is
    resolved by a Unicode-block heuristic before the request.
    """
    if source_lang == "auto":
        source_lang = _detect_source_lang(text)

    # MyMemory caps a single query at ~500 bytes; trim on a UTF-8 byte
    # boundary so CJK text (3 bytes/char) doesn't get rejected.
    text = text.encode("utf-8")[:450].decode("utf-8", errors="ignore").strip()

    src = _MYMMEMORY_LANGMAP.get(source_lang, source_lang)
    tgt = _MYMMEMORY_LANGMAP.get(target_lang, target_lang)

    url = (
        MYMEMORY_URL
        + "?"
        + urllib.parse.urlencode({"q": text, "langpair": f"{src}|{tgt}"})
    )

    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        translated = result.get("responseData", {}).get("translatedText", "") or ""
        # Success responses carry responseStatus == 200; error responses carry
        # it as null/missing even though the HTTP status is still 200.
        if result.get("responseStatus") != 200 or not translated:
            details = result.get("responseDetails") or ""
            logger.warning("MyMemory error: %s", details or translated[:200])
            return {
                "ok": False,
                "error": details or "Translation service unavailable",
            }

        logger.info(
            "Translated %d chars %s → %s (MyMemory)", len(text), source_lang, target_lang
        )
        return {
            "ok": True,
            "translated": translated,
            "source_lang": source_lang,
            "target_lang": target_lang,
        }

    except urllib.error.HTTPError as e:
        logger.warning("MyMemory HTTP error: %s %s", e.code, e.reason)
        return {"ok": False, "error": "Translation service unavailable"}

    except urllib.error.URLError as e:
        logger.warning("MyMemory connection error: %s", e.reason)
        return {
            "ok": False,
            "error": (
                "Translation service is not reachable. "
                "Check your internet connection."
            ),
        }

    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning("MyMemory response parse error: %s", e)
        return {"ok": False, "error": "Invalid response from translation service."}

    except Exception as e:
        logger.error("Unexpected translation error: %s", e)
        return {"ok": False, "error": "Translation failed. Please try again later."}


def _detect_source_lang(text: str) -> str:
    """Guess the source language from Unicode blocks (for auto-detect).

    Covers the languages we advertise; anything else defaults to English.
    Only the first matching block wins.
    """
    # CJK ideographs (simplified + traditional).  A string that also contains
    # kana is likely Japanese, so check kana first.
    for ch in text:
        if 0x3040 <= ord(ch) <= 0x30FF or 0x31F0 <= ord(ch) <= 0x31FF:
            return "ja"
    for ch in text:
        if 0x4E00 <= ord(ch) <= 0x9FFF or 0x3400 <= ord(ch) <= 0x4DBF:
            return "zh"
    for ch in text:
        if 0xAC00 <= ord(ch) <= 0xD7AF:
            return "ko"
    for ch in text:
        if 0x0400 <= ord(ch) <= 0x04FF:
            return "ru"
    for ch in text:
        if 0x0600 <= ord(ch) <= 0x06FF:
            return "ar"
    for ch in text:
        if 0x0900 <= ord(ch) <= 0x097F:
            return "hi"
    # European diacritics etc. are close enough to English for our purposes.
    return "en"
