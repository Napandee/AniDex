"""Cross-user visibility privacy controls — the shared prerequisite for any
feature that surfaces one user's library/activity to another (#22 "also
watching", #27 collaborative-filtering recommendations). Enforce hiding and
anonymization by calling these at query-construction time, not by filtering
in the template — a feature that reads library_entries across users and skips
this module is not privacy-safe no matter what the UI shows.

Both controls are about the OWNER of the data being surfaced, not the viewer:
"should THIS user's entry ever be shown to someone else," not "should the
viewer see it." Always pass the owning user's id/row, not the caller's.

Default state (resolved open question from #26): opt-in hiding. Nothing is
hidden by default, instance-wide or per-user — an admin or a user has to
explicitly add a tag/genre before anything gets withheld, so no one is
surprised by content silently disappearing. The instance-wide list and each
user's own list are just unioned together; there's no override/removal
semantics to reason about.
"""

import json

from app import config, db


def _instance_default_hidden_tags() -> list[str]:
    row = db.fetchone("SELECT value FROM instance_config WHERE key = 'default_hidden_tags'")
    if not row or not row["value"]:
        return []
    return json.loads(row["value"])


def get_hidden_tags(user_id: int) -> set[str]:
    """Union of the instance-wide default hidden tags and this user's own
    additions, lowercased for case-insensitive matching against genres/tags."""
    instance_default = _instance_default_hidden_tags()
    user_raw = config.get(user_id, "hidden_tags")
    user_tags = json.loads(user_raw) if user_raw else []
    return {t.strip().lower() for t in (instance_default + user_tags) if t.strip()}


def is_anonymized(user_id: int) -> bool:
    return config.get(user_id, "anonymize_activity") == "true"


def display_name(user: dict) -> str:
    """Name to show this user as in a cross-user visibility feature — their
    email/display name, or a fixed anonymized placeholder if they've opted in.
    Always call with the row of the user whose data is being displayed."""
    if is_anonymized(user["id"]):
        return "a user"
    return user.get("display_name") or user["email"]


def entry_hidden(genres: list[str] | None, personal_tags: list[str] | None, hidden_tags: set[str]) -> bool:
    """True if a library entry should never appear in a cross-user visibility
    feature, because one of its genres or personal tags is on the owning
    user's hidden-tags set (from get_hidden_tags(owner_user_id))."""
    if not hidden_tags:
        return False
    entry_tags = {t.strip().lower() for t in (genres or []) + (personal_tags or [])}
    return bool(entry_tags & hidden_tags)
