"""VAPID keypair for Web Push (issue #377).

Standard W3C Web Push, not a third-party push service (Firebase/OneSignal): this
instance generates its own keypair once and signs every push with the private half.
A subscribing browser's PushManager.subscribe() call already points its returned
endpoint at that browser vendor's own push infrastructure (Google's for Chrome,
Mozilla's for Firefox, ...) — sending directly to it needs no external account or
credentials of any kind, just a valid VAPID signature.

Instance-wide, not per-user (same shape as ANILIST_CLIENT_ID): this is the server's
own identity to every push service it talks to, not something meaningful to vary
per account.
"""

import base64

from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PrivateFormat,
    NoEncryption,
    PublicFormat,
)
from py_vapid import Vapid

from app import config


def get_or_create_keypair() -> tuple[str, str]:
    """Returns (public_key_b64, private_key_b64). Lazy-init: generated on first
    call and persisted, so a fresh instance never needs a separate setup step —
    the very first Settings page load or push send just works. A race between two
    concurrent first-ever calls is possible (both generate, last write wins) but
    harmless: it can only happen once, ever, on a brand-new instance, and either
    generated keypair is equally valid — nothing depends on winning that race.

    private_key_b64 is base64url-encoded DER, NOT PEM — confirmed live against
    py_vapid's own Vapid.from_string()/from_der() (what pywebpush.webpush() calls
    internally on every send): it base64url-decodes the string as-is and feeds
    the raw bytes straight to load_der_private_key(), so a PEM string (with
    "-----BEGIN PRIVATE KEY-----" armor and newlines) isn't valid input at all —
    an earlier version of this function stored private_pem() output here, which
    made every real push send fail with "ValueError: Could not deserialize key
    data" (caught silently by notify.py's WebPushChannel, so it never surfaced
    as a hard error — only found by actually sending a live push in a real
    browser, not by unit tests mocking pywebpush.webpush() itself).
    """
    private_b64 = config.get_instance_value("vapid_private_key")
    public_b64 = config.get_instance_value("vapid_public_key")
    if private_b64 and public_b64:
        return public_b64, private_b64

    v = Vapid()
    v.generate_keys()
    private_der = v.private_key.private_bytes(
        encoding=Encoding.DER,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    )
    private_b64 = base64.urlsafe_b64encode(private_der).rstrip(b"=").decode()
    raw_public = v.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    public_b64 = base64.urlsafe_b64encode(raw_public).rstrip(b"=").decode()

    config.set_instance_value("vapid_private_key", private_b64)
    config.set_instance_value("vapid_public_key", public_b64)
    return public_b64, private_b64
