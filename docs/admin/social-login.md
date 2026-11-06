# Social login

## Social login (Google/Discord, optional)

Google and Discord "Sign in with..." are optional alternatives to local email+password,
admin-configured per-instance via **Admin → Instance Config → OAuth settings** (or the
`GOOGLE_CLIENT_ID` / `DISCORD_CLIENT_ID` env vars as a fallback — see the table below).
This is a one-time,
instance-wide setup: the client id/secret are never seen or entered by individual users,
who just click Connect/Sign-in and authenticate with their own provider account, the
same as any "Sign in with Google" button anywhere on the web.

1. Create an OAuth app in the [Google Cloud Console](https://console.cloud.google.com/)
   and/or the [Discord Developer Portal](https://discord.com/developers/applications).
2. Register both redirect URIs for whichever provider(s) you're setting up (`{provider}`
   is `google` or `discord`), using your instance's actual public URL:
   - `https://your-instance-domain/auth/callback/{provider}`
   - `https://your-instance-domain/auth/link-callback/{provider}`
3. Paste the client id/secret into Admin → Instance Config → OAuth settings and save —
   no restart needed.

**Google-specific note:** unless you complete Google's app-verification review (not
needed for a small invite-only instance — reviewable, higher-friction, and pointless
overhead if you're never going to have the general public sign in), the OAuth app stays
in Google's **Testing** publishing status. In that status Google restricts sign-in to
accounts you've explicitly added as a **test user** (Google Cloud Console → OAuth
consent screen → Audience → Test users, cap 100) — so when you invite a new user who
wants to use Google login, add their Google email there first, or they'll hit an
"app not verified" block on their first attempt. This is normal, expected behavior for
any app that isn't publicly verified, not a bug — it's the tradeoff for skipping
Google's review process. Discord has no equivalent gate for the `identify`/`email`
scopes this app requests; any Discord account can connect immediately, no admin
pre-approval step needed.
