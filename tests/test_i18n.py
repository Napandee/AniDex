"""
Coverage for the i18n locale files added in #143/#147 — catches the exact failure
mode a manual one-off `python3 -c "..."` key-parity check (used while building #143)
doesn't: a locale file silently drifting out of key-parity with `en.json` on a later
PR that only remembers to update one or two of the five files.

No Postgres/app import needed — this only reads app/i18n/*.json directly.
"""

import json
import re
from pathlib import Path

import pytest

from app.i18n import SUPPORTED_LOCALES, DEFAULT_LOCALE

I18N_DIR = Path(__file__).resolve().parent.parent / "app" / "i18n"


def _load(locale: str) -> dict:
    return json.loads((I18N_DIR / f"{locale}.json").read_text(encoding="utf-8"))


def test_default_locale_is_supported():
    assert DEFAULT_LOCALE in SUPPORTED_LOCALES


@pytest.mark.parametrize("locale", SUPPORTED_LOCALES)
def test_locale_file_is_valid_json(locale):
    _load(locale)  # raises if malformed


@pytest.mark.parametrize("locale", [l for l in SUPPORTED_LOCALES if l != DEFAULT_LOCALE])
def test_locale_has_same_keys_as_default(locale):
    default_keys = set(_load(DEFAULT_LOCALE).keys())
    locale_keys = set(_load(locale).keys())
    missing = default_keys - locale_keys
    extra = locale_keys - default_keys
    assert not missing, f"{locale}.json is missing keys present in {DEFAULT_LOCALE}.json: {sorted(missing)}"
    assert not extra, f"{locale}.json has keys not present in {DEFAULT_LOCALE}.json: {sorted(extra)}"


@pytest.mark.parametrize("locale", SUPPORTED_LOCALES)
def test_locale_values_are_nonempty_strings(locale):
    for key, value in _load(locale).items():
        assert isinstance(value, str), f"{locale}.json[{key!r}] is not a string"
        assert value != "", f"{locale}.json[{key!r}] is an empty string"


def test_every_t_call_in_templates_resolves_to_a_real_key():
    """Static `t('key')` calls only — dynamic ones like `t('status_' + s.lower())`
    are checked separately below, since this can't evaluate the concatenation."""
    default_keys = set(_load(DEFAULT_LOCALE).keys())
    templates_dir = Path(__file__).resolve().parent.parent / "app" / "templates"
    missing = []
    for path in templates_dir.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"\{\{\s*t\('([a-zA-Z0-9_]+)'\s*[,)]", text):
            key = match.group(1)
            if key not in default_keys:
                missing.append(f"{path.name}: t('{key}')")
    assert not missing, "templates reference i18n keys that don't exist:\n" + "\n".join(missing)


def test_dynamic_status_keys_exist_for_every_anilist_status():
    # Mirrors the `t('status_' + s.lower())` pattern used in library.html, queue.html,
    # search.html, and notes.html for AniList's six list statuses plus the "ALL" filter.
    default_keys = set(_load(DEFAULT_LOCALE).keys())
    for status in ["watching", "completed", "dropped", "planning", "paused", "repeating", "all"]:
        assert f"status_{status}" in default_keys
