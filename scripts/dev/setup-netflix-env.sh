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
read -rp  "AniList username (optional, enter to skip): " anilist_username
read -rsp "AniList token (optional, enter to skip): " anilist_token; echo

{
  printf 'NETFLIX_ID_COOKIE=%s\n' "$netflix_id"
  printf 'NETFLIX_SECURE_ID_COOKIE=%s\n' "$secure_netflix_id"
  printf 'ANILIST_USERNAME=%s\n' "$anilist_username"
  printf 'ANILIST_TOKEN=%s\n' "$anilist_token"
} > .env.netflix.local
chmod 600 .env.netflix.local

echo "Wrote $(pwd)/.env.netflix.local"
