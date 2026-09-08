#!/usr/bin/env bash
# .claude/hooks/test-guard-git.sh — proves guard-git.sh denies what it must and
# allows the near-misses. A hook never observed denying, and never observed
# allowing a near-miss, is not known to work.
set -uo pipefail
HOOK="$(dirname "$0")/guard-git.sh"
pass=0; fail=0

check() { # check <expect: deny|allow> <args> <command>
  local expect="$1" args="$2" cmd="$3" out verdict
  out=$(printf '{"tool_name":"Bash","tool_input":{"command":%s}}' \
        "$(printf '%s' "$cmd" | jq -Rs .)" | bash "$HOOK" $args)
  # Parse the actual field, not a whitespace-shaped guess at it — grepping for
  # a literal '"permissionDecision":"deny"' substring silently depends on the
  # guard's JSON being compact with no space after the colon (2026-09-08
  # review, Important 3).
  if printf '%s' "$out" | jq -e '.hookSpecificOutput.permissionDecision=="deny"' >/dev/null 2>&1; then
    verdict=deny
  else
    verdict=allow
  fi
  if [ "$verdict" = "$expect" ]; then pass=$((pass+1)); printf '  ok    %-8s %s\n' "$expect" "$cmd"
  else fail=$((fail+1)); printf '  FAIL  want=%s got=%s  %s\n' "$expect" "$verdict" "$cmd"; fi
}

check_failopen() { # check_failopen <label> <raw stdin> — proves a guard that
  # can't parse its input still exits 0 with no denial, and says so loudly.
  local label="$1" input="$2" out rc tmp_err warned
  tmp_err=$(mktemp)
  out=$(printf '%s' "$input" | bash "$HOOK" 2>"$tmp_err")
  rc=$?
  warned=no; grep -q "WARNING" "$tmp_err" && warned=yes
  rm -f "$tmp_err"
  if [ "$rc" -eq 0 ] && [ -z "$out" ] && [ "$warned" = yes ]; then
    pass=$((pass+1)); printf '  ok    allow    %s (exit=0, no stdout, warned on stderr)\n' "$label"
  else
    fail=$((fail+1)); printf '  FAIL  %s: exit=%s stdout=%q warned=%s\n' "$label" "$rc" "$out" "$warned"
  fi
}

echo "-- bulk staging: denied only with --public --"
check deny  --public 'git add -A'
check deny  --public 'git add .'
check deny  --public 'git add --all'
check allow ""       'git add -A'
check allow ""       'git add .'

echo "-- near-misses that must keep working --"
check allow --public 'git add -p'
check allow --public 'git add src/main.py'
check allow --public 'git add .gitignore'
check allow --public 'git add ./docs/FAULTS.md'
check allow --public 'git status'

echo "-- private paths: denied everywhere --"
check deny  ""       'git add .claude/context'
check deny  --public 'git add .claude/scratch/notes.html'
check allow ""       'cat .claude/scratch/notes.html'

echo "-- force push: bare denied, with-lease allowed --"
check deny  ""       'git push --force origin main'
check deny  ""       'git push -f origin main'
check allow ""       'git push --force-with-lease origin main'
check allow ""       'git push origin main'

echo "-- git global options before the subcommand must not defeat detection --"
check deny  --public 'git -C /tmp add -A'
check deny  ""       'git -C /tmp add .claude/context/x.md'
check deny  ""       'git --git-dir=/tmp/.git push --force origin main'
check allow ""       'git -C /tmp status'
check allow ""       'git -C /tmp push --force-with-lease origin main'

echo "-- bundled short flags must not defeat force-push detection --"
check deny  ""       'git push -uf origin main'

echo "-- flag enumeration cannot work: parse the token stream instead (round 2) --"
check deny  --public 'git --no-pager add -A'
check deny  --public 'git -p add -A'
check deny  ""       'git --bare add .claude/context/x'
check deny  ""       'git --namespace foo push --force origin main'
# NOTE: the coordinator's round-2 message specified this case as
#   check deny  ""  'git -c user.name=x add -A'
# i.e. denied even in a PRIVATE repo. That contradicts the documented,
# already-passing design: bulk-add (rule 3) is gated on --public by
# construction (see the two `check allow "" 'git add -A'/'git add .'` cases
# above, and the "Install" comment at the top of guard-git.sh) — making rule
# 3 fire unconditionally to satisfy this one case would flip those two
# existing allows to denies, which is exactly the "weaken an existing case"
# outcome we were told never to do. This reads as a copy/paste slip (missing
# --public, matching the two sibling cases immediately above it, which test
# the identical "-A after some skippable flag" shape). Flagged instead of
# silently "fixed": running it as written below, with --public, to prove
# -c's separate-value-skip doesn't break bulk-add detection — the actual
# thing this case appears designed to test.
check deny  --public 'git -c user.name=x add -A'
check allow ""       'git --no-pager status'
check allow ""       'git -C /tmp log --oneline'
check deny  --public 'git status && git add -A'
check allow ""       'git add -p && git commit'

echo "-- multi-line commands: every line must be checked, not just the first (round 3) --"
check deny  --public 'cd /tmp
git add -A'
check deny  ""       'echo hi
git push --force origin main'
check deny  ""       'set -e
git add .claude/context/x'
check allow --public 'git add -p
git commit'

echo "-- quoted flags must not defeat exact-token comparisons (round 3) --"
check deny  --public 'git add "-A"'
check deny  --public "git add '-A'"
check deny  --public 'git add "."'
check deny  ""       'git push "-f" origin main'

echo "-- alias-bypass (\\git) and path-qualified git must still be recognized (round 3) --"
check deny  ""       '\git push --force origin main'
check deny  --public '\git add -A'
check deny  ""       '/usr/bin/git push --force origin main'

echo "-- a quoted string that merely mentions git is not an invocation (round 3) --"
check allow ""       'echo "git add -A is dangerous"'

echo "-- malformed/empty input: fails open, loudly, never blocks --"
check_failopen "malformed JSON"          'not json at all'
check_failopen "empty stdin"             ''
check_failopen "valid JSON, no command"  '{"tool_name":"Bash","tool_input":{}}'

echo
echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
