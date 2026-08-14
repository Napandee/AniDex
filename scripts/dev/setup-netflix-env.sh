#!/usr/bin/env bash
# Interactive helper for (re-)populating .env.netflix.local — run this instead of
# hand-crafting a one-liner every time cookies need refreshing (they expire).
# Values are read straight into the file via `read`, never passed through a
# command line or a format string, so there's no risk of the % in a URL-encoded
# cookie value getting misinterpreted (printf's format-string bug, if you build
# this by hand: any % in the value gets parsed as a format specifier).
set -euo pipefail
cd "$(dirname "$0")/../.."

read -rsp "Netflix 'NetflixId' cookie value: " netflix_id; echo
read -rsp "Netflix 'SecureNetflixId' cookie value: " secure_netflix_id; echo
echo
echo "For the Falcor API probe (scripts/dev/probe_netflix_falcor.py), also paste:"
echo "  devtools -> Network -> the pathEvaluator/viewingActivity request -> Headers"
echo "  -> Request Headers -> the full 'cookie' row value (one long string)."
read -rsp "Full Netflix cookie header (optional, enter to skip): " cookie_header; echo
echo "  Same request's POST body decodes to {\"guid\": \"...\"} — that guid value:"
read -rp  "Netflix profile guid (optional, enter to skip): " profile_guid
echo
read -rp  "AniList username (optional, enter to skip): " anilist_username
read -rsp "AniList token (optional, enter to skip): " anilist_token; echo

{
  printf 'NETFLIX_ID_COOKIE=%s\n' "$netflix_id"
  printf 'NETFLIX_SECURE_ID_COOKIE=%s\n' "$secure_netflix_id"
  printf 'NETFLIX_COOKIE_HEADER=%s\n' "$cookie_header"
  printf 'NETFLIX_PROFILE_GUID=%s\n' "$profile_guid"
  printf 'ANILIST_USERNAME=%s\n' "$anilist_username"
  printf 'ANILIST_TOKEN=%s\n' "$anilist_token"
} > .env.netflix.local
chmod 600 .env.netflix.local

echo "Wrote $(pwd)/.env.netflix.local"
