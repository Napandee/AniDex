import os
import sys
from pathlib import Path

# scripts/*.py read several env vars at import time (module-level os.environ[...]
# lookups) since they're written to run as standalone subprocess entry points, not
# as an importable package. Set harmless dummy values here — before pytest imports
# any test module — so importing them for testing never needs real credentials or
# a reachable database. Tests that need specific values override via monkeypatch.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("USER_ID", "1")
os.environ.setdefault("ANILIST_TOKEN", "test-token")
os.environ.setdefault("ANILIST_USERNAME", "test-user")
# app.main reads ANILIST_MOCK once at import time (module-level constant) — set
# here, before any test module gets a chance to trigger that first import, so
# tests that exercise the rating/status/progress write paths (issue #208's MCP
# write tools and their underlying routes) never attempt a real AniList call
# just because some other test file happened to import app.main first without
# this set. A previous local attempt at this same thing in
# tests/test_pat_and_mcp_server.py's live_app fixture used the wrong env var
# name (MOCK_ANILIST instead of ANILIST_MOCK) and silently never took effect —
# fixed there too, but this conftest-level default is the one that's actually
# guaranteed to run first.
os.environ.setdefault("ANILIST_MOCK", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
