"""
Release version baked into the image (VERSION build arg -> APP_VERSION env) and
surfaced by app_version(), the admin instance-health data and the Settings page.
Needs a reachable Postgres via DATABASE_URL for the last two tests (skipped without).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_app_version_reads_env(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-key")
    monkeypatch.setenv("APP_VERSION", " 1.2.3 ")
    import app.main as m

    assert m.app_version() == "1.2.3"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_app_version_defaults_to_dev(monkeypatch, value):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-key")
    if value is None:
        monkeypatch.delenv("APP_VERSION", raising=False)
    else:
        monkeypatch.setenv("APP_VERSION", value)
    import app.main as m

    assert m.app_version() == "dev"


def test_instance_health_includes_version(app_module, monkeypatch):
    monkeypatch.setenv("APP_VERSION", "1.2.3")
    assert app_module._instance_health()["version"] == "1.2.3"


def test_settings_page_shows_version(app_module, client, monkeypatch):
    monkeypatch.setenv("APP_VERSION", "1.2.3")
    resp = client.post(
        "/auth/register",
        data={"email": "ver@example.com", "password": "correct horse battery staple"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text

    html = client.get("/settings").text
    assert "v1.2.3" in html
