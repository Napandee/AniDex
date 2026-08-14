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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
