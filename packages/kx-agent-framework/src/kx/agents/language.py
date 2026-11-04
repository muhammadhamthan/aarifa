"""Buyer languages. Arifaa Pack sells worldwide in English, Arabic, Hindi, Tamil, Chinese and Sinhala;
the AI must answer in the language the buyer writes in (T17), and voice picks its speech vendor by it.

Script detection is deterministic and cheap: the five non-Latin languages each have their own Unicode
block, so the buyer's script tells us the language without an LLM call. Latin-script text (English,
transliterated Hindi ...) falls back to the language stored on the contact.
"""

from __future__ import annotations

from typing import Final

DEFAULT_LANGUAGE: Final = "en"

LANGUAGE_NAMES: Final[dict[str, str]] = {
    "en": "English",
    "ar": "Arabic",
    "hi": "Hindi",
    "ta": "Tamil",
    "zh": "Chinese (Simplified)",
    "si": "Sinhala",
}

# (first code point, last code point, language) - checked in order
_SCRIPTS: Final = (
    (0x0600, 0x06FF, "ar"),
    (0x0750, 0x077F, "ar"),
    (0xFB50, 0xFDFF, "ar"),
    (0xFE70, 0xFEFF, "ar"),
    (0x0900, 0x097F, "hi"),
    (0x0B80, 0x0BFF, "ta"),
    (0x0D80, 0x0DFF, "si"),
    (0x4E00, 0x9FFF, "zh"),
    (0x3400, 0x4DBF, "zh"),
)
_MIN_SHARE: Final = 0.2  # a few foreign letters (a product name) do not switch the language


def normalise_language(tag: str | None, default: str = DEFAULT_LANGUAGE) -> str:
    """'ar-AE' / 'AR' / 'zh_Hans' -> 'ar' / 'ar' / 'zh'. Unknown or empty -> `default`."""
    if not tag:
        return default
    base = tag.replace("_", "-").split("-", 1)[0].strip().lower()
    return base or default


def detect_script_language(text: str) -> str | None:
    """The language implied by the dominant non-Latin script, or None for Latin / no letters."""
    counts: dict[str, int] = {}
    letters = 0
    for ch in text:
        if not ch.isalpha():
            continue
        letters += 1
        cp = ord(ch)
        if cp < 0x0600:
            continue
        for lo, hi, lang in _SCRIPTS:
            if lo <= cp <= hi:
                counts[lang] = counts.get(lang, 0) + 1
                break
    if not counts or not letters:
        return None
    lang, n = max(counts.items(), key=lambda kv: kv[1])
    return lang if n / letters >= _MIN_SHARE else None


def reply_language(text: str, contact_language: str | None) -> str:
    """Language to answer in: the buyer's script first, then the contact record, then English."""
    return detect_script_language(text) or normalise_language(contact_language)


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get(normalise_language(code), code)
