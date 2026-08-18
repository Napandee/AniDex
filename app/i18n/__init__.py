"""Lightweight i18n: flat JSON locale files + a per-request translation callable.

No Babel/gettext — see issue #139 for why (this app has no frontend build step,
and Babel's extraction/compilation would need to become a new backend build step
in the deploy pipeline). English is the source-of-truth key set; every other
locale falls back to the English string, then the raw key, for anything missing
so a partial translation never breaks rendering.
"""

import json
from pathlib import Path

SUPPORTED_LOCALES = ["en", "es", "hi", "ja", "zh-CN"]
DEFAULT_LOCALE = "en"

# Each language's own name for itself, for the Settings language <select> — not
# translated per-locale, since a language name is most recognizable in its own script.
LOCALE_LABELS = {
    "en": "English",
    "es": "Español",
    "hi": "हिन्दी",
    "ja": "日本語",
    "zh-CN": "简体中文",
}

_LOCALE_DIR = Path(__file__).parent
_TRANSLATIONS: dict[str, dict[str, str]] = {
    locale: json.loads((_LOCALE_DIR / f"{locale}.json").read_text(encoding="utf-8"))
    for locale in SUPPORTED_LOCALES
}


def resolve_locale(accept_language: str | None, user_language: str | None) -> str:
    """The user's saved `language` setting wins when set and supported; otherwise
    fall back to the browser's Accept-Language header (for logged-out pages like
    auth_login.html, where there's no settings row to read yet), then English."""
    if user_language in SUPPORTED_LOCALES:
        return user_language
    if accept_language:
        for part in accept_language.split(","):
            code = part.split(";")[0].strip()
            if code in SUPPORTED_LOCALES:
                return code
            prefix = code.split("-")[0]
            for locale in SUPPORTED_LOCALES:
                if locale.split("-")[0] == prefix:
                    return locale
    return DEFAULT_LOCALE


def translator(locale: str):
    strings = _TRANSLATIONS.get(locale, {})
    fallback = _TRANSLATIONS[DEFAULT_LOCALE]

    def t(key: str, **kwargs) -> str:
        text = strings.get(key) or fallback.get(key) or key
        return text.format(**kwargs) if kwargs else text

    return t


def all_strings(locale: str) -> dict[str, str]:
    """The full key->string map for a locale, English-fallback already applied to
    any key that locale hasn't translated — for embedding as `window.I18N` so
    client-side JS (library.html/recommendations.html's runtime-built button and
    status text, see #147) never needs its own fallback chain, just a key lookup."""
    fallback = _TRANSLATIONS[DEFAULT_LOCALE]
    strings = _TRANSLATIONS.get(locale, {})
    return {**fallback, **strings}
