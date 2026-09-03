# Troubleshooting

## A sync keeps failing

1. **Check Settings → Sync & Credentials → Sync history.** Every run — scheduled or
   triggered by "Sync Now" — logs a row here with a status and, for a failed step,
   the real error detail (not just "something went wrong").
2. **Use the "Test connection" button** on the provider's credential card. It makes
   one lightweight real request against that provider's own API using your saved
   credential, without touching your library — the fastest way to tell "the
   credential itself is bad" from "something else broke."
3. **Check Admin → Instance Health** (admin accounts only) for anything broader —
   a pending-migration warning, or an aggregate failure-rate signal across users.

### Common causes, per provider

- **Crunchyroll / Netflix / Prime Video** (cookie-based): the saved session cookie
  has expired or been rotated by the provider. Recapture it the same way as initial
  setup — see [Sync providers](sync-providers.md)'s per-provider steps — and save it
  again in Settings. For Netflix and Prime Video specifically, an expired cookie gets
  its own distinctly worded failure message ("your connection has expired. Reconnect
  it in Settings.") instead of the generic sync-failed message, so you don't have to
  guess which of the causes below actually applies.
- **Prime Video specifically**: expect this far more often than Crunchyroll or
  Netflix. Amazon's watch-history session sits behind a shorter-lived tier than
  general browsing, so a cookie captured today can be dead again within a day even
  if you're still logged into primevideo.com everywhere else — this isn't a bug in
  AniDex, every third-party tool that reads this same endpoint hits the same limit.
  The [Prime Video Cookie Sync browser extension](../../browser-extension/primevideo-cookie-sync/)
  automates the recapture if this is happening often enough to be annoying.
- **Plex**: the stored server token was revoked, or the Plex Media Server AniDex
  syncs from is offline/unreachable from your AniDex instance's network. Reconnect
  via Settings' Plex card.
- **A provider changed their own API.** All four integrations read an unofficial,
  undocumented endpoint on the provider's side — any of them can break without
  warning if that provider changes something. If reconnecting a fresh credential
  doesn't fix it, that's the likely cause; there's nothing to configure your way
  around, it needs a code fix.

## A title matched the wrong AniList entry

Sync matching is title-based and can occasionally get it wrong, especially for a
title with no direct AniList match. This is a one-click manual fix — just correct
the status/progress on the right entry — and never touches your score or notes. If
it's happening a lot for one specific title (e.g. a franchise with an ambiguous
season naming pattern), Crunchyroll has a manual title-override table in Settings
for exactly that case; the other providers don't yet.

## Nothing has synced in a long time / a new title never gets created

Check whether **Force Full Resync** (Settings → Sync & Credentials → "Force Full
Resync") has ever actually completed for that provider — a first-connect or forced
resync deliberately never auto-creates new entries (to avoid flooding your real
AniList list on a first full walk), only a routine day-to-day incremental sync does.
If a resync keeps getting interrupted before finishing, it never gets the chance to
switch into that normal incremental mode.

## Where to look next

If none of the above explains it, the container's own logs have the full detail
Sync history's summary doesn't — worth checking (or asking whoever runs your
instance to check) if you're comfortable with that, or see
[Upgrading and migrations](../admin/upgrading.md) if the problem started right after
an update.
