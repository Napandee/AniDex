# Multi-user and admin

AniDex supports multiple accounts on one instance, invite-only. The very first account
created on an empty instance becomes admin automatically; every account after that
needs an invite from an admin.

## Signing in

- **Local email + password** is the default, zero-config option.
- **Google / Discord** sign-in can be enabled per-instance by an admin
  (Admin → Instance Config). Linking a social account to an existing local account is
  always an explicit action you take from Settings — it never happens automatically
  just because the email address matches, since a bare provider-supplied email isn't
  treated as proof of identity here.
- **Two-factor authentication** (TOTP) is optional, per local account, set up from
  Settings via a QR code. You get one-time hashed recovery codes for the lost-device
  case. This only applies to local email+password login, not social sign-in.

## Sessions

Settings shows your own active sessions and lets you revoke any of them individually —
useful if you signed in somewhere you no longer trust, without needing to change your
password.

## "Also watching" (opt-in)

If you opt in, other users on the same instance can see that you also have a given
show in your library. It's off by default, and configurable per-user:

- Hide specific tags or genres from being counted toward this signal.
- An "anonymize my activity" option if you want the aggregate signal without your own
  identity attached.

Nothing about another user's library is ever surfaced unless they've opted in.

## Admin panel

Admins get a tabbed panel:

- **Users** — manage accounts, including soft deactivation (a deactivated user can't
  sign in but their data isn't deleted).
- **Invites** — issue invites for new accounts.
- **Instance Config** — sync schedule time (instance-wide, not per-user), OAuth
  provider setup.
- **Operability** — instance health readout (including whether AniList itself has
  recently signalled a rate limit, so a sync slowdown is visible instead of silently
  retrying in the background) and the admin audit log (which admin did what, when).
- **Data Quality** — sync drift, orphaned rows, and other data-integrity signals
  across every user on the instance (the admin-wide counterpart to the personal
  Library Health card on [Stats](stats.md)).

Admins can also trigger a one-click backup export covering every user's library and
personal data in one file.
