"""Tests for scripts/release/next_version.py (release version computation)."""
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "release" / "next_version.py"
sys.path.insert(0, str(SCRIPT.parent))

from next_version import next_version  # noqa: E402


def test_no_tags_gives_first_version():
    assert next_version([], "patch") == "1.0.0"


@pytest.mark.parametrize(
    "bump,expected", [("patch", "1.2.4"), ("minor", "1.3.0"), ("major", "2.0.0")]
)
def test_bumps_highest_tag(bump, expected):
    assert next_version(["v1.0.0", "v1.2.3", "v0.9.0"], bump) == expected


def test_ignores_non_semver_tags():
    tags = ["v1.2", "latest", "v1.2.3-rc.1", "v9", "v1.0.0"]
    assert next_version(tags, "patch") == "1.0.1"


def test_compares_numerically_not_lexically():
    assert next_version(["v1.9.0", "v1.10.0"], "patch") == "1.10.1"


def test_invalid_bump_rejected():
    with pytest.raises(ValueError):
        next_version([], "huge")


def test_cli_prints_next_version(capsys):
    import next_version as nv

    nv.main(["minor"], tags=["v1.0.0", "v1.1.0"])
    assert capsys.readouterr().out.strip() == "1.2.0"


def test_cli_rejects_bad_bump():
    import next_version as nv

    with pytest.raises(SystemExit) as exc:
        nv.main(["huge"], tags=[])
    assert exc.value.code != 0


def test_cli_refuses_existing_tag(monkeypatch, capsys):
    # Defensive guard: if the computed tag is somehow already among the repo's
    # tags (the scan and the tag list disagreeing), the CLI must exit non-zero
    # and print no version.
    import next_version as nv

    monkeypatch.setattr(nv, "next_version", lambda tags, bump, first="1.0.0": "1.0.1")
    with pytest.raises(SystemExit) as exc:
        nv.main(["patch"], tags=["v1.0.0", "v1.0.1"])
    assert exc.value.code != 0
    assert capsys.readouterr().out == ""
