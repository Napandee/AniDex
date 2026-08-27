# MCP server

AniDex exposes an [MCP](https://modelcontextprotocol.io) server so an AI client — Claude
Code, or any other MCP-compatible client — can read, and optionally write, your own
library, notes, stats, and recommendation data directly, instead of you copy-pasting
things back and forth.

It runs as part of the same app process, mounted at `/mcp` — no separate service to
install or keep running.

## Setting it up

1. Go to **Settings → API Access**, and find the **Personal access tokens** section.
2. Create a token, choosing a **scope**:
   - `read` — can call any of the four read tools below. Can't change anything.
   - `read_write` — can also call the five write tools below.
3. Copy the token immediately — like a GitHub PAT, it's only shown once at creation
   time. If you lose it, revoke it and issue a new one.
4. Point your MCP client at `https://your-anidex-host/mcp` with
   `Authorization: Bearer adx_pat_...`.

A token only ever resolves to the account that issued it — there's no way for a token
to read or write another user's data, even on a multi-user instance.

## Read tools (either scope)

### `list_library_entries(status=None, limit=500)`

Your library: title, status, progress, score, personal tags. Optionally filter to one
status (`WATCHING`, `COMPLETED`, `DROPPED`, `PLANNING`, `PAUSED`, `REPEATING`).

### `list_personal_notes(anime_id=None)`

Drop reasons, custom tags, mood tags, freeform notes, watch-next priority, and the
favorite flag. Optionally filter to a single anime.

### `list_recommendations(include_dismissed=False, limit=100)`

Candidate anime, match score, and the reason payload (which genres/tags/studio
matched, and whether it came from similarity or seasonal discovery). Excludes
dismissed/snoozed candidates by default, matching what the `/recommendations` page
shows.

### `get_stats()`

Status totals, total episodes, watch time, completion rate, mean score, score
distribution, and top genres. Covers the core `/stats` numbers — not every specialized
breakdown the full page has (the activity heatmap and drop-pattern analysis aren't
included).

## Write tools (`read_write` scope only)

Every write tool takes an explicit anime id (or an explicit list of ids) — never a
filter or search term that could resolve to an unbounded set of anime at execution
time. This is deliberate: it bounds how much damage one bad reasoning step by an AI
client can do to a single, named entry.

### `update_personal_notes(anime_id, drop_reason=None, notes=None, personal_tags=None, mood=None, watch_next_priority=None, anilist_id_override=None)`

Replaces your notes for one anime. **This is a full replace, not a merge** — any field
you leave unset here gets *cleared* on the stored row, the same as submitting the
Settings form with that field blank. Call `list_personal_notes` first if you want to
change one field while keeping the others.

### `bulk_apply_tags(anime_ids, tags)`

Adds tags to a list of anime you name explicitly. Additive — merges into each entry's
existing tags rather than replacing them. Capped at 200 ids per call.

### `set_rating(anime_id, score)`

Sets your star rating (0–5) for one anime. Pushed to AniList immediately.

### `set_status(anime_id, status)`

Sets list status (`WATCHING`, `COMPLETED`, `DROPPED`, `PLANNING`, `PAUSED`,
`REPEATING`) for one anime. Pushed to AniList immediately.

### `set_progress(anime_id, progress)`

Sets episode progress for one anime. Pushed to AniList immediately.

## What these tools deliberately don't do

- No bulk status changes, no bulk rating changes — only tags support a bulk form, and
  even that requires an explicit id list.
- No "find and update" style tool — you always need the anime's AniList id first
  (from a read tool or the app itself).
- Every write goes through the exact same internal function the app's own HTTP routes
  use — an MCP write can't do anything the web UI itself couldn't already do.
