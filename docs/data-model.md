# Data model (for the curious)

You don't need to know any of this to use AniDex — this page is for understanding what
happens to your data, not a setup guide.

AniDex's database keeps three kinds of data in separate tables, on purpose:

## 1. AniList-sourced data

Your anime catalog and list status, as last synced from AniList. Fully rebuildable at
any time by re-running a sync — nothing here is precious in itself, because AniList is
the real source of truth for it. Includes the airing schedule cache and cached AniList
search results.

## 2. Personal layer

Drop reasons, custom tags, mood tags, freeform notes, rewatch notes, queue priority,
recommendation scores, streaming-service preferences, and saved collections. **This is
the layer sync jobs never touch and AniList has no place for.** If you ever wipe and
re-sync your AniList data, this layer survives untouched — it isn't rebuilt from
AniList, it's yours.

The one caveat: `recommendation_scores` is genuinely rebuilt by the weekly recommender
job (new candidates need fresh scores), but the `dismissed`/`snoozed` state you've set
on existing recommendations is always preserved across that rebuild.

## 3. Account and instance data

Your login, sessions, invites, notification settings, personal access tokens, and (for
admins) the audit log and instance-wide configuration. Not derived from AniList, not
part of the personal layer either — this is just "the account this app knows about
you," same as any other multi-user app.

## Where credentials live

Any real secret stored in AniDex — your AniList token, Crunchyroll/Netflix session
cookies, notification bot tokens, Prime Video/Plex credentials if connected — is
encrypted at rest in the database. The app can decrypt them to make an API call on your
behalf; a raw database dump alone doesn't hand someone your credentials.

## What never gets pushed to AniList

Drop reasons, custom tags, mood tags, notes, watch-next priority, and collections are
AniDex-only. The only three fields that ever write back to AniList are **status**,
**progress**, and **rating** — always through AniList's own `SaveMediaListEntry`
mutation, never anything broader.
