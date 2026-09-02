"""Local email+password auth routes — login, 2FA, register, password reset, logout.

Issue #463, slice 1 of an incremental split of app/main.py into feature-based
APIRouter modules. Deliberately scoped to the *local*-auth routes only — the
OAuth login/link/callback routes stay in app/main.py for a future slice, since
they lean on more async/external-client machinery (_ensure_oauth_registered,
the authlib OAuth client) that's cleaner to extract separately.

Shared helpers (session/rate-limit state, DB-backed user lookups, templates)
still live in app.main. This module imports app.main as a module reference
rather than named imports, and only reads attributes off it inside the route
handlers (i.e. at request time, not at import time) — that's what lets
app.main import this router without caring about app.main's own top-to-bottom
definition order: by the time any handler here actually runs, app.main has
long since finished executing and every attribute exists.
"""
from datetime import datetime, timezone

import bcrypt
import pyotp
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import config, db, sessions
from app import main as app_main

router = APIRouter()


def _valid_reset_token(token: str):
    """Returns the password_resets row if the token is usable, else None.

    Looks up by SHA256(token) (issue #358) — the raw token is never stored, same
    hash-and-lookup pattern as sessions.hash_token(), reusing that exact function
    rather than a second hashing convention."""
    row = db.fetchone(
        "SELECT * FROM password_resets WHERE token_hash = %s AND used_at IS NULL AND expires_at > now()",
        (sessions.hash_token(token),),
    )
    return row


@router.get("/auth/login", response_class=HTMLResponse)
def auth_login_page(request: Request, error: str = ""):
    if app_main._no_users_exist():
        return RedirectResponse(url="/auth/register", status_code=303)
    return app_main.templates.TemplateResponse(
        request,
        "auth_login.html",
        {
            "error": error,
            "oauth_google_configured": app_main.oauth_configured("google"),
            "oauth_discord_configured": app_main.oauth_configured("discord"),
        },
    )


@router.post("/auth/login")
def auth_login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    if app_main._ip_login_rate_limited(app_main._client_ip(request)):
        return app_main.templates.TemplateResponse(
            request,
            "auth_login.html",
            {
                "error": "Too many login attempts from this network. Try again later.",
                "oauth_google_configured": app_main.oauth_configured("google"),
                "oauth_discord_configured": app_main.oauth_configured("discord"),
            },
            status_code=429,
        )
    email = email.strip().lower()
    # Match by email regardless of auth_provider — an OAuth-originated account can
    # have a local password set from Settings too (see #11), and auth_provider is
    # purely historical ("how this account was originally created"), not a gate on
    # which login methods currently work. password_hash being unset (OAuth-only,
    # never added a password) still correctly fails the check below.
    user = db.fetchone("SELECT * FROM users WHERE email = %s", (email,))

    if user and user["locked_until"] and user["locked_until"] > datetime.now(timezone.utc):
        minutes_left = max(1, int((user["locked_until"] - datetime.now(timezone.utc)).total_seconds() // 60) + 1)
        return RedirectResponse(
            url=f"/auth/login?error=Too+many+failed+attempts.+Try+again+in+{minutes_left}+minutes",
            status_code=303,
        )

    valid = user and user["password_hash"] and bcrypt.checkpw(
        password.encode("utf-8"), user["password_hash"].encode("utf-8")
    )
    if not valid:
        if user:
            attempts = user["failed_login_attempts"] + 1
            if attempts >= app_main._LOGIN_MAX_ATTEMPTS:
                db.execute(
                    "UPDATE users SET failed_login_attempts = %s, locked_until = now() + (%s * interval '1 minute') WHERE id = %s",
                    (attempts, app_main._LOGIN_LOCKOUT_MINUTES, user["id"]),
                )
            else:
                db.execute(
                    "UPDATE users SET failed_login_attempts = %s WHERE id = %s",
                    (attempts, user["id"]),
                )
        return RedirectResponse(
            url="/auth/login?error=Invalid+email+or+password", status_code=303
        )

    if not user["is_active"]:
        return RedirectResponse(
            url="/auth/login?error=This+account+has+been+deactivated", status_code=303
        )

    if user["totp_enabled"]:
        # Issue #83 — hold off on last_login_at/the real session until the second
        # factor also succeeds. The password itself is proven correct at this point,
        # so failed_login_attempts/locked_until (the PASSWORD guess budget) reset
        # right away, same as the no-2FA path below — that's a separate concern from
        # totp_failed_attempts/totp_locked_until (the CODE guess budget), which
        # auth_login_2fa_submit owns exclusively from here on. If that TOTP counter
        # is already locked, say so now instead of bouncing the user into a 2FA
        # prompt they can't actually get past.
        if user["totp_locked_until"] and user["totp_locked_until"] > datetime.now(timezone.utc):
            minutes_left = max(1, int((user["totp_locked_until"] - datetime.now(timezone.utc)).total_seconds() // 60) + 1)
            return RedirectResponse(
                url=f"/auth/login?error=Too+many+failed+attempts.+Try+again+in+{minutes_left}+minutes",
                status_code=303,
            )
        db.execute(
            "UPDATE users SET failed_login_attempts = 0, locked_until = NULL WHERE id = %s",
            (user["id"],),
        )
        request.session[app_main._PENDING_2FA_SESSION_KEY] = user["id"]
        return RedirectResponse(url="/auth/login/2fa", status_code=303)

    db.execute(
        "UPDATE users SET last_login_at = now(), failed_login_attempts = 0, locked_until = NULL WHERE id = %s",
        (user["id"],),
    )
    app_main._set_authenticated_session(request, user["id"])
    return RedirectResponse(url="/", status_code=303)


@router.get("/auth/login/2fa", response_class=HTMLResponse)
def auth_login_2fa_page(request: Request, error: str = ""):
    """Second-factor prompt (issue #83) — only reachable via the pending_2fa_user_id
    session key auth_login_submit sets after a correct password on an account with
    TOTP enabled. Never starts a real session itself; that only happens on a
    verified code/recovery code below, via _set_authenticated_session (issue #82's
    session-token model under the hood, see that helper's docstring)."""
    if not request.session.get(app_main._PENDING_2FA_SESSION_KEY):
        return RedirectResponse(url="/auth/login", status_code=303)
    return app_main.templates.TemplateResponse(request, "auth_login_2fa.html", {"error": error})


@router.post("/auth/login/2fa")
def auth_login_2fa_submit(request: Request, code: str = Form(...)):
    if app_main._ip_login_rate_limited(app_main._client_ip(request)):
        return app_main.templates.TemplateResponse(
            request,
            "auth_login_2fa.html",
            {"error": "Too many login attempts from this network. Try again later."},
            status_code=429,
        )
    pending_id = request.session.get(app_main._PENDING_2FA_SESSION_KEY)
    if not pending_id:
        return RedirectResponse(url="/auth/login", status_code=303)

    user = db.fetchone("SELECT * FROM users WHERE id = %s", (pending_id,))
    if not user or not user["totp_enabled"] or not user["is_active"]:
        # Account state changed mid-flow (2FA disabled elsewhere, deactivated, or
        # deleted) — no valid pending login to complete.
        request.session.pop(app_main._PENDING_2FA_SESSION_KEY, None)
        return RedirectResponse(url="/auth/login", status_code=303)

    if user["totp_locked_until"] and user["totp_locked_until"] > datetime.now(timezone.utc):
        request.session.pop(app_main._PENDING_2FA_SESSION_KEY, None)
        minutes_left = max(1, int((user["totp_locked_until"] - datetime.now(timezone.utc)).total_seconds() // 60) + 1)
        return RedirectResponse(
            url=f"/auth/login?error=Too+many+failed+attempts.+Try+again+in+{minutes_left}+minutes",
            status_code=303,
        )

    code = code.strip()
    valid = bool(code) and pyotp.TOTP(config.decrypt_secret(user["totp_secret"])).verify(code, valid_window=1)
    if not valid and code:
        # Falls back to a one-time recovery code (issue #83's lost-authenticator path)
        # — only tried if the TOTP check itself failed, so a valid 6-digit code is
        # never wasted second-guessing it against the recovery-code table.
        valid = app_main._consume_recovery_code_if_valid(user["id"], code) is not None

    if not valid:
        attempts = user["totp_failed_attempts"] + 1
        if attempts >= app_main._TOTP_LOGIN_MAX_ATTEMPTS:
            db.execute(
                "UPDATE users SET totp_failed_attempts = %s, totp_locked_until = now() + (%s * interval '1 minute') WHERE id = %s",
                (attempts, app_main._TOTP_LOGIN_LOCKOUT_MINUTES, user["id"]),
            )
            # Crossing the lockout threshold ends this login attempt just as
            # definitively as the two early-return branches above — clear the
            # pending state AND redirect straight to /auth/login with the lockout
            # message immediately, rather than back to /auth/login/2fa, so an
            # immediate follow-up GET can't re-render a code-entry form the account
            # is now locked out of even for one extra round trip.
            request.session.pop(app_main._PENDING_2FA_SESSION_KEY, None)
            return RedirectResponse(
                url=f"/auth/login?error=Too+many+failed+attempts.+Try+again+in+{app_main._TOTP_LOGIN_LOCKOUT_MINUTES}+minutes",
                status_code=303,
            )
        db.execute(
            "UPDATE users SET totp_failed_attempts = %s WHERE id = %s",
            (attempts, user["id"]),
        )
        return RedirectResponse(url="/auth/login/2fa?error=Invalid+code", status_code=303)

    db.execute(
        "UPDATE users SET last_login_at = now(), totp_failed_attempts = 0, totp_locked_until = NULL WHERE id = %s",
        (user["id"],),
    )
    app_main._set_authenticated_session(request, user["id"])
    return RedirectResponse(url="/", status_code=303)


@router.get("/auth/register", response_class=HTMLResponse)
def auth_register_page(request: Request, error: str = ""):
    return app_main.templates.TemplateResponse(request, "auth_register.html", {"error": error})


@router.post("/auth/register")
def auth_register_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    if len(password) < 8:
        return RedirectResponse(
            url="/auth/register?error=Password+must+be+at+least+8+characters", status_code=303
        )

    existing = db.fetchone(
        "SELECT id FROM users WHERE auth_provider = 'local' AND auth_provider_id = %s",
        (email,),
    )
    if existing:
        return RedirectResponse(
            url="/auth/register?error=An+account+with+that+email+already+exists", status_code=303
        )

    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    user, denied = app_main._resolve_or_create_user("local", email, email, None, None)
    if denied:
        return denied
    db.execute("UPDATE users SET password_hash = %s WHERE id = %s", (password_hash, user["id"]))

    app_main._set_authenticated_session(request, user["id"])
    return RedirectResponse(url="/", status_code=303)


@router.get("/auth/reset-password/{token}", response_class=HTMLResponse)
def auth_reset_password_page(request: Request, token: str, error: str = ""):
    valid = bool(_valid_reset_token(token))
    return app_main.templates.TemplateResponse(
        request,
        "auth_reset_password.html",
        {"valid": valid, "token": token, "error": error},
        status_code=200 if valid else 400,
    )


@router.post("/auth/reset-password/{token}")
def auth_reset_password_submit(request: Request, token: str, password: str = Form(...)):
    reset = _valid_reset_token(token)
    if not reset:
        return app_main.templates.TemplateResponse(
            request,
            "auth_reset_password.html",
            {"valid": False, "token": token, "error": ""},
            status_code=400,
        )
    if len(password) < 8:
        return RedirectResponse(
            url=f"/auth/reset-password/{token}?error=Password+must+be+at+least+8+characters",
            status_code=303,
        )

    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    db.execute(
        "UPDATE users SET password_hash = %s, failed_login_attempts = 0, locked_until = NULL WHERE id = %s",
        (password_hash, reset["user_id"]),
    )
    db.execute(
        "UPDATE password_resets SET used_at = now() WHERE token_hash = %s",
        (sessions.hash_token(token),),
    )

    return RedirectResponse(url="/auth/login", status_code=303)


@router.post("/auth/logout")
def auth_logout(request: Request):
    app_main._end_session(request)
    return RedirectResponse(url="/", status_code=303)
