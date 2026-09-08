# Data Sources

Where catalogue data comes from and the rules for importing it.

AniList GraphQL API — `https://graphql.anilist.co`, POST requests, no auth needed for
public reads, OAuth token needed for anything under the user's own list. Rate limit:
90 req/min on the free tier — batch queries, don't loop one-anime-per-request where a
paginated query works.

Streaming links (`externalLinks`, `streamingEpisodes`) are community-curated on AniList's
side and can lag real availability — show a "last synced" timestamp next to them rather
than presenting them as guaranteed-current.
