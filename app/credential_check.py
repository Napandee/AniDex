"""
Live "Test connection" checks for Settings → Credentials (issue #188).

Reuses the exact login/auth code the sync scripts themselves run, rather than
reinventing credential validation from scratch:
  - AniList: scripts/anilist_sync_common.py's gql() helper, against the same
    MediaListCollection(userName) query sync_anilist.py's own library pull
    resolves — just chunk=1/perChunk=1, so validating a credential costs one
    cheap call instead of a full library pull.
  - Crunchyroll: scripts/sync_crunchyroll.py's CrunchyrollHistory._login() —
    the exact etp_rt-cookie login flow the real sync runs before it can fetch
    anything.
  - Netflix: scripts/sync_netflix.py's NetflixHistory._fetch_page(1) — the
    exact cookie+profile-guid-authenticated Falcor call the real sync uses,
    just capped to one page instead of a full history walk.

scripts/*.py are written as standalone subprocess entry points (see
run_full_sync.py), each reading DATABASE_URL/USER_ID/ANILIST_TOKEN/
ANILIST_USERNAME at *import* time since they normally run as their own
process with those set in its env. The always-running app process never sets
USER_ID/ANILIST_TOKEN/ANILIST_USERNAME itself (those exist only per
subprocess invocation, scoped to whichever user's sync is running) — so
importing those modules directly here would crash unless something fills
them in first. tests/conftest.py already works around the exact same problem
for pytest with harmless os.environ.setdefault(...) values; this module does
the same for the app. setdefault() never overwrites a real value if one
happens to already be set (e.g. DATABASE_URL, which the app process always
has), and none of the functions below ever rely on the module-level
ANILIST_TOKEN/ANILIST_USERNAME/USER_ID constants those scripts define — every
credential checked here is passed through explicitly, per-request, per-user.
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql://unused:unused@localhost/unused")
os.environ.setdefault("USER_ID", "0")
os.environ.setdefault("ANILIST_TOKEN", "")
os.environ.setdefault("ANILIST_USERNAME", "")

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from anilist_sync_common import gql  # noqa: E402
from sync_crunchyroll import CrunchyrollHistory  # noqa: E402
from sync_netflix import NetflixHistory  # noqa: E402
from sync_primevideo import PrimeVideoHistory  # noqa: E402

# Cheap enough to run on every "Test connection" click: perChunk=1 still
# requires AniList to resolve the username and return *a* page, so a bad
# username or an auth failure surfaces the same way the real sync would hit
# it, without pulling the user's whole list just to prove a token works.
_ANILIST_CHECK_QUERY = """
query ($userName: String) {
  MediaListCollection(userName: $userName, type: ANIME, chunk: 1, perChunk: 1) {
    hasNextChunk
  }
}
"""


def check_anilist(username: str, token: str) -> tuple[bool, str]:
    username = (username or "").strip()
    token = (token or "").strip()
    if not username:
        return False, "No AniList username is set."
    try:
        gql(_ANILIST_CHECK_QUERY, {"userName": username}, token=token or None)
        return True, f'Found AniList user "{username}".'
    except Exception as e:
        return False, str(e)


def check_crunchyroll(etp_rt: str) -> tuple[bool, str]:
    etp_rt = (etp_rt or "").strip()
    if not etp_rt:
        return False, "No Crunchyroll etp_rt cookie is set."
    client = CrunchyrollHistory(etp_rt)
    try:
        client._login()
        return True, "Crunchyroll session is valid."
    except Exception as e:
        return False, str(e)


def check_netflix(cookie_header: str, profile_guid: str) -> tuple[bool, str]:
    cookie_header = (cookie_header or "").strip()
    profile_guid = (profile_guid or "").strip()
    if not cookie_header or not profile_guid:
        return False, "Both the cookie header and profile guid are needed."
    client = NetflixHistory(cookie_header, profile_guid)
    try:
        client._fetch_page(1)
        return True, "Netflix session and profile guid are valid."
    except Exception as e:
        return False, str(e)


def check_primevideo(cookie_header: str) -> tuple[bool, str]:
    cookie_header = (cookie_header or "").strip()
    if not cookie_header:
        return False, "No Prime Video cookie header is set."
    client = PrimeVideoHistory(cookie_header)
    try:
        client._fetch_page(None)
        return True, "Prime Video session is valid."
    except Exception as e:
        return False, str(e)
