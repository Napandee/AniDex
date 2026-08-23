"""
Plex OAuth PIN connect flow (issue #153).

Plex has no server-side redirect+callback OAuth like Google/Discord (see
app/main.py's /settings/link/{provider}) — instead a client requests a PIN,
sends the user to plex.tv to approve it in a new tab, and polls until the PIN
is claimed. This is the exact flow every third-party Plex app (Overseerr,
Tautulli, etc.) uses for "Sign in with Plex", confirmed by reading
python-plexapi's MyPlexAccount/MyPlexPinLogin/MyPlexResource source directly —
see notes/2026-08-19-plex-sync-research.md for the full research trail this
module is built from.

Two tokens are involved, and they are not interchangeable:
  - the plex.tv ACCOUNT token this module's create_pin()/poll_pin() produce —
    proves who the Plex account is, not which server;
  - a SERVER-scoped token (what a real Plex Media Server actually accepts on
    every request) — list_server_resources() exchanges the account token for
    one per reachable server, so the user never has to go find and paste a raw
    server token by hand.

Deliberately hand-rolled with plain httpx calls rather than depending on
python-plexapi as a runtime dependency, matching this app's existing
preference for direct API clients over vendored third-party tools (see
CLAUDE.md's Architecture section on sync_crunchyroll.py/sync_netflix.py).

create_pin()/poll_pin() are CONFIRMED AGAINST THE REAL plex.tv API (2026-08-23,
unauthenticated — PIN creation/polling needs no Plex account login, only the
final "sign in and see a real authToken" step does): a live POST to
/api/v2/pins returned 201 with exactly the predicted {id, code, authToken:
null, ...} shape, and a live GET to /api/v2/pins/{id} returned 200 with the
same shape while still pending. list_server_resources()/pick_connection() and
the full end-to-end connect flow (a real account actually completing sign-in,
then a real server's watch history being fetched) are NOT yet live-verified —
needs Andreas to click through this app's own Connect button against a real
account, same class of remaining gap #17 (Prime Video) has for its own
live-only step.
"""

import httpx

# Not a secret — a stable, app-level identifier plex.tv associates this app's
# PIN-create/poll calls with. Generated once via uuid4(), hardcoded rather than
# per-request: the PIN-poll call must reuse the same identifier the PIN was
# created with, and there's no reason for it to vary between users or requests.
PLEX_CLIENT_IDENTIFIER = "f51b4395-529b-4537-9c8a-51b9b1c7fff4"
PLEX_PRODUCT = "AniDex"
PLEX_TV_ROOT = "https://plex.tv"

_HEADERS = {
    "Accept": "application/json",
    "X-Plex-Product": PLEX_PRODUCT,
    "X-Plex-Client-Identifier": PLEX_CLIENT_IDENTIFIER,
}


def create_pin() -> dict:
    """POST /api/v2/pins — starts a new connect attempt. Returns {"id", "code",
    "auth_url"}; auth_url is what the frontend opens in a new tab for the user
    to approve on plex.tv (their own password/2FA, never touches this app)."""
    resp = httpx.post(
        f"{PLEX_TV_ROOT}/api/v2/pins", params={"strong": "true"}, headers=_HEADERS, timeout=15
    )
    resp.raise_for_status()
    data = resp.json()
    auth_url = (
        "https://app.plex.tv/auth#?"
        f"clientID={PLEX_CLIENT_IDENTIFIER}&code={data['code']}"
        f"&context%5Bdevice%5D%5Bproduct%5D={PLEX_PRODUCT}"
    )
    return {"id": data["id"], "code": data["code"], "auth_url": auth_url}


def poll_pin(pin_id: int) -> str | None:
    """GET /api/v2/pins/{id} — the plex.tv account authToken once the user has
    completed sign-in, or None while still pending. Raises (via
    raise_for_status()) if the pin itself is invalid/expired."""
    resp = httpx.get(f"{PLEX_TV_ROOT}/api/v2/pins/{pin_id}", headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json().get("authToken") or None


def list_server_resources(account_token: str) -> list[dict]:
    """Servers this Plex account can see, each carrying its own server-scoped
    accessToken and a list of reachable connections — the exchange that means
    the user is never asked to paste a raw server token by hand."""
    resp = httpx.get(
        f"{PLEX_TV_ROOT}/api/v2/resources",
        params={"includeHttps": "1", "includeRelay": "1"},
        headers={**_HEADERS, "X-Plex-Token": account_token},
        timeout=15,
    )
    resp.raise_for_status()
    return [r for r in resp.json() if "server" in (r.get("provides") or "")]


def pick_connection(resource: dict, timeout: float = 3.0) -> str | None:
    """First reachable connection URI for `resource` — local connections tried
    before remote/relay ones. A quick unauthenticated GET /identity (every Plex
    server answers this without a token) is enough to confirm reachability
    without a full authenticated round trip for every candidate."""
    connections = sorted(resource.get("connections") or [], key=lambda c: not c.get("local"))
    for conn in connections:
        uri = conn.get("uri")
        if not uri:
            continue
        try:
            resp = httpx.get(f"{uri}/identity", timeout=timeout)
            if resp.status_code == 200:
                return uri
        except httpx.HTTPError:
            continue
    return None
