"""ClipSync translation endpoint — uses LibreTranslate public API.

No API key required. Falls back gracefully if the service is unavailable.
Uses only stdlib (urllib + json) — zero dependencies.
"""

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

LIBRETRANSLATE_URL = "https://libretranslate.com/translate"
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


def translate_text(
    text: str, target_lang: str = "en", source_lang: str = "auto"
) -> dict:
    """Translate text using the free LibreTranslate API.

    Args:
        text: The text to translate.
        target_lang: Target language code (default 'en').
        source_lang: Source language code (default 'auto').

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

    # Limit text length (LibreTranslate has practical limits)
    text = text.strip()[:5000]

    request_body = json.dumps(
        {
            "q": text,
            "source": source_lang,
            "target": target_lang,
            "format": "text",
        }
    ).encode("utf-8")

    try:
        req = urllib.request.Request(
            LIBRETRANSLATE_URL,
            data=request_body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        translated = result.get("translatedText", "")
        detected_lang = result.get("detectedLanguage", {})
        if isinstance(detected_lang, dict):
            src_lang = detected_lang.get("language", source_lang)
        else:
            src_lang = source_lang

        logger.info(
            "Translated %d chars %s → %s", len(text), src_lang, target_lang
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
