from django import template

register = template.Library()


@register.filter
def dict_pct(value, total):
    """Calculates percentage of value over total safely."""
    try:
        val = int(value or 0)
        tot = int(total or 0)
        if tot <= 0:
            return 100
        return int((val / tot) * 100)
    except (ValueError, TypeError):
        return 0


@register.filter
def format_compact_tokens(value):
    """Formats large token counts into human-readable compact numbers (e.g. 1.2M, 45K)."""
    try:
        val = int(value or 0)
        if val >= 1000000:
            return f"{val / 1000000:.1f}M"
        if val >= 1000:
            return f"{val / 1000:.1f}K"
        return str(val)
    except (ValueError, TypeError):
        return "0"


@register.filter
def replace_underscore(value):
    """Replaces underscores with spaces."""
    if not value:
        return ""
    return str(value).replace("_", " ")


# ── ISO 639-1/2 and common LLM-returned language string → full English name ──
_LANG_MAP: dict[str, str] = {
    # Arabic & Middle-Eastern
    "ar": "Arabic",
    "ara": "Arabic",
    "arabic": "Arabic",
    "he": "Hebrew",
    "heb": "Hebrew",
    "hebrew": "Hebrew",
    "fa": "Persian (Farsi)",
    "per": "Persian (Farsi)",
    "fas": "Persian (Farsi)",
    "farsi": "Persian (Farsi)",
    "persian": "Persian (Farsi)",
    "ur": "Urdu",
    "urd": "Urdu",
    "urdu": "Urdu",
    "ps": "Pashto",
    "pus": "Pashto",
    "pashto": "Pashto",
    "ku": "Kurdish",
    "kur": "Kurdish",
    "kurdish": "Kurdish",
    # Southeast Asian (important for Indonesia)
    "id": "Indonesian",
    "ind": "Indonesian",
    "indonesian": "Indonesian",
    "bahasa indonesia": "Indonesian",
    "bahasa": "Indonesian",
    "ms": "Malay",
    "msa": "Malay",
    "mal": "Malay",
    "malay": "Malay",
    "th": "Thai",
    "tha": "Thai",
    "thai": "Thai",
    "vi": "Vietnamese",
    "vie": "Vietnamese",
    "vietnamese": "Vietnamese",
    "tl": "Filipino",
    "fil": "Filipino",
    "tagalog": "Filipino",
    "filipino": "Filipino",
    "km": "Khmer",
    "khm": "Khmer",
    "khmer": "Khmer",
    "lo": "Lao",
    "lao": "Lao",
    "my": "Burmese",
    "bur": "Burmese",
    "burmese": "Burmese",
    # East Asian
    "zh": "Chinese",
    "zho": "Chinese",
    "chi": "Chinese",
    "chinese": "Chinese",
    "zh-hans": "Chinese (Simplified)",
    "zh-hant": "Chinese (Traditional)",
    "ja": "Japanese",
    "jpn": "Japanese",
    "japanese": "Japanese",
    "ko": "Korean",
    "kor": "Korean",
    "korean": "Korean",
    # South Asian
    "hi": "Hindi",
    "hin": "Hindi",
    "hindi": "Hindi",
    "bn": "Bengali",
    "ben": "Bengali",
    "bengali": "Bengali",
    "ta": "Tamil",
    "tam": "Tamil",
    "tamil": "Tamil",
    "te": "Telugu",
    "tel": "Telugu",
    "telugu": "Telugu",
    "mr": "Marathi",
    "mar": "Marathi",
    "marathi": "Marathi",
    "gu": "Gujarati",
    "guj": "Gujarati",
    "gujarati": "Gujarati",
    "pa": "Punjabi",
    "pan": "Punjabi",
    "punjabi": "Punjabi",
    "si": "Sinhala",
    "sin": "Sinhala",
    "sinhala": "Sinhala",
    "ne": "Nepali",
    "nep": "Nepali",
    "nepali": "Nepali",
    # European
    "en": "English",
    "eng": "English",
    "english": "English",
    "fr": "French",
    "fra": "French",
    "fre": "French",
    "french": "French",
    "de": "German",
    "deu": "German",
    "ger": "German",
    "german": "German",
    "es": "Spanish",
    "spa": "Spanish",
    "spanish": "Spanish",
    "it": "Italian",
    "ita": "Italian",
    "italian": "Italian",
    "pt": "Portuguese",
    "por": "Portuguese",
    "portuguese": "Portuguese",
    "nl": "Dutch",
    "nld": "Dutch",
    "dut": "Dutch",
    "dutch": "Dutch",
    "ru": "Russian",
    "rus": "Russian",
    "russian": "Russian",
    "pl": "Polish",
    "pol": "Polish",
    "polish": "Polish",
    "cs": "Czech",
    "cze": "Czech",
    "ces": "Czech",
    "czech": "Czech",
    "sv": "Swedish",
    "swe": "Swedish",
    "swedish": "Swedish",
    "da": "Danish",
    "dan": "Danish",
    "danish": "Danish",
    "fi": "Finnish",
    "fin": "Finnish",
    "finnish": "Finnish",
    "nb": "Norwegian",
    "nor": "Norwegian",
    "norwegian": "Norwegian",
    "ro": "Romanian",
    "ron": "Romanian",
    "rum": "Romanian",
    "romanian": "Romanian",
    "hu": "Hungarian",
    "hun": "Hungarian",
    "hungarian": "Hungarian",
    "el": "Greek",
    "gre": "Greek",
    "ell": "Greek",
    "greek": "Greek",
    "tr": "Turkish",
    "tur": "Turkish",
    "turkish": "Turkish",
    "uk": "Ukrainian",
    "ukr": "Ukrainian",
    "ukrainian": "Ukrainian",
    "bg": "Bulgarian",
    "bul": "Bulgarian",
    "bulgarian": "Bulgarian",
    "hr": "Croatian",
    "hrv": "Croatian",
    "croatian": "Croatian",
    "sk": "Slovak",
    "slk": "Slovak",
    "slo": "Slovak",
    "slovak": "Slovak",
    "sr": "Serbian",
    "srp": "Serbian",
    "serbian": "Serbian",
    "lt": "Lithuanian",
    "lit": "Lithuanian",
    "lithuanian": "Lithuanian",
    "lv": "Latvian",
    "lav": "Latvian",
    "latvian": "Latvian",
    "et": "Estonian",
    "est": "Estonian",
    "estonian": "Estonian",
    "ca": "Catalan",
    "cat": "Catalan",
    "catalan": "Catalan",
    # African
    "sw": "Swahili",
    "swa": "Swahili",
    "swahili": "Swahili",
    "am": "Amharic",
    "amh": "Amharic",
    "amharic": "Amharic",
    "so": "Somali",
    "som": "Somali",
    "somali": "Somali",
    "ha": "Hausa",
    "hau": "Hausa",
    "hausa": "Hausa",
    "yo": "Yoruba",
    "yor": "Yoruba",
    "yoruba": "Yoruba",
    # Other
    "hy": "Armenian",
    "arm": "Armenian",
    "armenian": "Armenian",
    "ka": "Georgian",
    "geo": "Georgian",
    "georgian": "Georgian",
    "az": "Azerbaijani",
    "aze": "Azerbaijani",
    "azerbaijani": "Azerbaijani",
    "kk": "Kazakh",
    "kaz": "Kazakh",
    "kazakh": "Kazakh",
    "uz": "Uzbek",
    "uzb": "Uzbek",
    "uzbek": "Uzbek",
    # Unknown / placeholder
    "unknown": "Unknown",
    "": "Unknown",
}


@register.filter
def normalize_language(value: str) -> str:
    """
    Converts ISO 639-1/2 language codes and raw LLM-returned strings into
    properly capitalised full English names.

    Examples:
      'ar'      → 'Arabic'
      'id'      → 'Indonesian'
      'bahasa'  → 'Indonesian'
      'english' → 'English'
      'Unknown' → 'Unknown'
      'xyz'     → 'Xyz'  (graceful title-case fallback)
    """
    if not value:
        return "Unknown"
    key = str(value).strip().lower()
    return _LANG_MAP.get(key, str(value).strip().title())


import os
import time

_CACHE_BUST_VAL = None


@register.filter
def cache_bust(static_url: str) -> str:
    """Appends a dynamic cache busting version based on the RELEASE_VERSION env variable or current server startup time."""
    global _CACHE_BUST_VAL
    if _CACHE_BUST_VAL is None:
        # Fallback to current time at startup to guarantee change on deployment restart
        _CACHE_BUST_VAL = os.getenv("RELEASE_VERSION") or str(int(time.time()))

    if not static_url:
        return ""

    if "?" in static_url:
        return f"{static_url}&v={_CACHE_BUST_VAL}"
    return f"{static_url}?v={_CACHE_BUST_VAL}"
