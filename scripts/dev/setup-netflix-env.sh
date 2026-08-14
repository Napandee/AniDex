#!/usr/bin/env bash
# Interactive helper for (re-)populating .env.netflix.local — run this instead of
# hand-crafting a one-liner every time cookies need refreshing (they expire).
# Values are read straight into the file via `read`, never passed through a
# command line or a format string, so there's no risk of the % in a URL-encoded
# cookie value getting misinterpreted (printf's format-string bug, if you build
# this by hand: any % in the value gets parsed as a format specifier).
#
# Re-running this loads whatever's already in .env.netflix.local as defaults —
# press enter on any prompt to keep the existing value instead of re-pasting it.
# Some values here (NETFLIX_COOKIE_HEADER especially) are raw HTTP header strings
# full of semicolons/ampersands/parentheses — unquoted `KEY=value` breaks `source`
# on those. Every value this script writes is single-quoted for exactly that
# reason; shquote() does the quoting (escaping any literal ' in the value).
set -euo pipefail
cd "$(dirname "$0")/../.."

shquote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

if [ -f .env.netflix.local ]; then
  set -a
  # shellcheck disable=SC1091
  source .env.netflix.local
  set +a
fi

# $1 = prompt label, $2 = existing value (may be empty), $3 = 1 for hidden input
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

netflix_id=$(prompt_keep_existing "Netflix 'NetflixId' cookie value" "${NETFLIX_ID_COOKIE:-}" 1)
secure_netflix_id=$(prompt_keep_existing "Netflix 'SecureNetflixId' cookie value" "${NETFLIX_SECURE_ID_COOKIE:-}" 1)
echo
echo "For the Falcor API probe (scripts/dev/probe_netflix_falcor.py), also paste:"
echo "  devtools -> Network -> the pathEvaluator/viewingActivity request -> Headers"
echo "  -> Request Headers -> the full 'cookie' row value (one long string)."
cookie_header=$(prompt_keep_existing "Full Netflix cookie header" "${NETFLIX_COOKIE_HEADER:-}" 1)
echo "  Same request's POST body decodes to {\"guid\": \"...\"} — that guid value:"
profile_guid=$(prompt_keep_existing "Netflix profile guid" "${NETFLIX_PROFILE_GUID:-}" 0)
echo
anilist_username=$(prompt_keep_existing "AniList username" "${ANILIST_USERNAME:-}" 0)
anilist_token=$(prompt_keep_existing "AniList token" "${ANILIST_TOKEN:-}" 1)

{
  printf 'NETFLIX_ID_COOKIE=%s\n' "$(shquote "$netflix_id")"
  printf 'NETFLIX_SECURE_ID_COOKIE=%s\n' "$(shquote "$secure_netflix_id")"
  printf 'NETFLIX_COOKIE_HEADER=%s\n' "$(shquote "$cookie_header")"
  printf 'NETFLIX_PROFILE_GUID=%s\n' "$(shquote "$profile_guid")"
  printf 'ANILIST_USERNAME=%s\n' "$(shquote "$anilist_username")"
  printf 'ANILIST_TOKEN=%s\n' "$(shquote "$anilist_token")"
} > .env.netflix.local
chmod 600 .env.netflix.local

echo "Wrote $(pwd)/.env.netflix.local"
echo
echo "If a value ever gets mangled after pasting a very long string (e.g. it lands"
echo "on the wrong line), it's likely your terminal choking on the paste length —"
echo "open .env.netflix.local directly in a text editor instead and fix that one"
echo "line by hand, no need to redo the whole interactive flow."
