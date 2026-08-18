"""Per-user settings backed by the settings table (PK: user_id, key)."""

from app import db

DEFAULTS = {
    "timezone": "Europe/London",
    "language": "en",
    "theme": "system",
}


def get_all(user_id: int) -> dict:
    rows = db.fetchall("SELECT key, value FROM settings WHERE user_id = %s", (user_id,))
    result = dict(DEFAULTS)
    result.update({r["key"]: r["value"] for r in rows})
    return result


def get(user_id: int, key: str) -> str:
    row = db.fetchone(
        "SELECT value FROM settings WHERE user_id = %s AND key = %s", (user_id, key)
    )
    return row["value"] if row else DEFAULTS.get(key, "")


def set_value(user_id: int, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO settings (user_id, key, value) VALUES (%s, %s, %s) "
        "ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value",
        (user_id, key, value),
    )
