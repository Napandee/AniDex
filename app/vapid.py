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

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid

from app import config


def get_or_create_keypair() -> tuple[str, str]:
    """Returns (public_key_b64, private_key_pem). Lazy-init: generated on first
    call and persisted, so a fresh instance never needs a separate setup step —
    the very first Settings page load or push send just works. A race between two
    concurrent first-ever calls is possible (both generate, last write wins) but
    harmless: it can only happen once, ever, on a brand-new instance, and either
    generated keypair is equally valid — nothing depends on winning that race.
    """
    private_pem = config.get_instance_value("vapid_private_key")
    public_b64 = config.get_instance_value("vapid_public_key")
    if private_pem and public_b64:
        return public_b64, private_pem

    v = Vapid()
    v.generate_keys()
    private_pem = v.private_pem().decode()
    raw_public = v.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    public_b64 = base64.urlsafe_b64encode(raw_public).rstrip(b"=").decode()

    config.set_instance_value("vapid_private_key", private_pem)
    config.set_instance_value("vapid_public_key", public_b64)
    return public_b64, private_pem
