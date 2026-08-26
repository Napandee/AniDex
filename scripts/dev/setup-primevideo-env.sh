#!/usr/bin/env bash
# Interactive helper for (re-)populating .env.primevideo.local — same pattern as
# setup-netflix-env.sh (see that file's comment for why every value is single-quoted
# on write: these are raw HTTP cookie header strings full of characters that break an
# unquoted `KEY=value` under `source`).
#
# Re-running this loads whatever's already in .env.primevideo.local as defaults —
# press enter on any prompt to keep the existing value instead of re-pasting it.
set -euo pipefail
cd "$(dirname "$0")/../.."

shquote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

if [ -f .env.primevideo.local ]; then
  set -a
  # shellcheck disable=SC1091
  source .env.primevideo.local
  set +a
fi

prompt_keep_existing() {
  local label="$1" existing="$2" hidden="${3:-0}" hint value
  if [ -n "$existing" ]; then
    hint=" [already set — enter to keep]"
  else
    hint=" (enter to skip)"
  fi
  if [ "$hidden" = "1" ]; then
    read -rsp "$label$hint: " value; echo
  else
    read -rp "$label$hint: " value
  fi
  if [ -z "$value" ]; then
    echo "$existing"
  else
    echo "$value"
  fi
}

echo "For scripts/dev/probe_primevideo_history.py, from a real logged-in session:"
echo "  primevideo.com/settings/watch-history -> devtools -> Network tab -> the"
echo "  getWatchHistorySettingsPage request -> Headers -> Request Headers -> the"
echo "  full 'cookie' row value (one long string, not just individual cookie names)."
cookie_header=$(prompt_keep_existing "Full Prime Video cookie header" "${PRIMEVIDEO_COOKIE_HEADER:-}" 1)
echo
echo "Optional: if the probe's cold call (no nextToken) fails, paste a nextToken"
echo "captured from a real 'load more' scroll as a known-good starting point —"
echo "see notes/2026-08-14-netflix-prime-sync-research.md for how to decode/read one."
next_token=$(prompt_keep_existing "Prime Video nextToken (optional)" "${PRIMEVIDEO_NEXT_TOKEN:-}" 0)

{
  printf 'PRIMEVIDEO_COOKIE_HEADER=%s\n' "$(shquote "$cookie_header")"
  printf 'PRIMEVIDEO_NEXT_TOKEN=%s\n' "$(shquote "$next_token")"
} > .env.primevideo.local
chmod 600 .env.primevideo.local

echo "Wrote $(pwd)/.env.primevideo.local"
