"""App-level settings backed by the settings table."""

from app import db

DEFAULTS = {
    "timezone": "Europe/London",
}


def get_all() -> dict:
    rows = db.fetchall("SELECT key, value FROM settings")
    result = dict(DEFAULTS)
    result.update({r["key"]: r["value"] for r in rows})
    return result


def get(key: str) -> str:
    row = db.fetchone("SELECT value FROM settings WHERE key = %s", (key,))
    return row["value"] if row else DEFAULTS.get(key, "")


def set_value(key: str, value: str) -> None:
    db.execute(
        "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, value),
    )
