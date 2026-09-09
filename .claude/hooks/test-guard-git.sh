#!/usr/bin/env bash
# .claude/hooks/test-guard-git.sh — proves guard-git.sh denies what it must and
# allows the near-misses. A hook never observed denying, and never observed
# allowing a near-miss, is not known to work.
#
# 2026-09-09 rewrite: guard-git.sh dropped its hand-rolled tokenizer for three
# `grep -E` checks over the raw command string (see the design note at the top
# of guard-git.sh — five rounds of the tokenizer approach each passed their
# own tests while carrying a complete bypass; docs/FAULTS.md). That trade
# shows up here as real expectation changes, not just additions:
#   - Cases that only worked because the old tokenizer resolved a real git
#     invocation's subcommand/tail (global options before the subcommand,
#     quoted flags, a backslash-alias prefix, a chained/multi-line bulk-add)
#     are now ALLOW: R3 (bulk add) is an anchored whole-trimmed-command match
#     only, by deliberate design (see guard-git.sh). These are marked below
#     with a comment, not deleted — they document the accepted loss.
#   - One case flips the other way, to DENY: raw substring matching has no
#     concept of "this flag belongs to a different command in the chain," so
#     an unrelated `-f` elsewhere in a chain can now trip the force-push
#     check. This is the documented over-denial trade-off, not a bug.
set -uo pipefail
HOOK="$(dirname "$0")/guard-git.sh"
pass=0; fail=0

check() { # check <expect: deny|allow> <args> <command>
  local expect="$1" args="$2" cmd="$3" out rc verdict
  out=$(printf '{"tool_name":"Bash","tool_input":{"command":%s}}' \
        "$(printf '%s' "$cmd" | jq -Rs .)" | bash "$HOOK" $args)
  rc=$?
  # A guard that crashed (or was replaced by `exit 1`, or anything else
  # non-zero) is NOT "allowed" — it's unknown, and a PreToolUse hook exiting
  # non-zero-with-stderr can outright BLOCK the tool call. Treating a crash
  # as a pass here is how a guard replaced by a bare `exit 1` scored
  # passed=17 and one that crashed on stderr scored passed=20 in an earlier
  # round — every `allow` assertion was passing against a guard that did
  # nothing. check_failopen() below already got this right; check() matches
  # it. This must survive the 2026-09-09 rewrite untouched.
  if [ "$rc" -ne 0 ]; then
    fail=$((fail+1)); printf '  FAIL  want=%s got=CRASH(rc=%s)  %s\n' "$expect" "$rc" "$cmd"
    return
  fi
  # Parse the actual field, not a whitespace-shaped guess at it — grepping for
  # a literal '"permissionDecision":"deny"' substring silently depends on the
  # guard's JSON being compact with no space after the colon.
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

echo "-- explicit required allow set (task list, args-agnostic) --"
check allow ""       'git push --force-with-lease origin main'
check allow ""       'git add -p'
check allow ""       'git add .gitignore'
check allow ""       'git add file.txt'
check allow ""       'git status'
check allow ""       'ls -A'
check allow ""       'rm -f /tmp/x'

echo "-- private paths: denied everywhere (R1 is a plain substring/word check, unaffected by the rewrite) --"
check deny  ""       'git add .claude/context'
check deny  --public 'git add .claude/scratch/notes.html'
check allow ""       'cat .claude/scratch/notes.html'

echo "-- force push: bare denied, with-lease allowed --"
check deny  ""       'git push --force origin main'
check deny  ""       'git push -f origin main'
check allow ""       'git push --force-with-lease origin main'
check allow ""       'git push origin main'

echo "-- git global options before the subcommand: R1/R2 (substring) still catch these; R3's anchored whole-command match (rewrite) no longer does --"
check allow --public 'git -C /tmp add -A' # R3 loss: 'git -C /tmp add -A' is not the literal string 'git add -A' — accepted (see guard-git.sh design note)
check deny  ""       'git -C /tmp add .claude/context/x.md'
check deny  ""       'git --git-dir=/tmp/.git push --force origin main'
check allow ""       'git -C /tmp status'
check allow ""       'git -C /tmp push --force-with-lease origin main'

echo "-- bundled short flags must not defeat force-push detection --"
check deny  ""       'git push -uf origin main'

echo "-- tokenizer-defeat cases from the old design: R1/R2 substring checks still catch what they always caught; R3's anchor no longer catches the bulk-add ones (rewrite) --"
check allow --public 'git --no-pager add -A' # R3 loss: not an exact match to any canonical bulk-add string — accepted
check allow --public 'git -p add -A'         # R3 loss: same as above
check deny  ""       'git --bare add .claude/context/x'
check deny  ""       'git --namespace foo push --force origin main'
check allow --public 'git -c user.name=x add -A' # R3 loss: same as above
check allow ""       'git --no-pager status'
check allow ""       'git -C /tmp log --oneline'
check allow --public 'git status && git add -A'  # R3 loss: chained command, not a whole-string match — this is the documented example of what R3 deliberately misses
check allow --public 'git add -p && git commit'

echo "-- multi-line commands: R1/R2 substring checks scan every line for free; R3's anchor requires the WHOLE trimmed command to match, so a bulk-add on its own line is now missed (rewrite, accepted) --"
check allow --public 'cd /tmp
git add -A' # R3 loss: trimmed command is "cd /tmp\ngit add -A", not "git add -A" — accepted, same shape as 'cd /tmp && git add -A'
check deny  ""       'echo hi
git push --force origin main'
check deny  ""       'set -e
git add .claude/context/x'
check allow --public 'git add -p
git commit'

echo "-- quoted flags: R1's path substring match is quote-agnostic and still works; R3's literal match and R2's flag regex are not, so these are now accepted losses (rewrite) --"
check allow --public 'git add "-A"'      # R3 loss: literal string differs from 'git add -A' by the quote characters
check allow --public "git add '-A'"      # R3 loss: same, single-quoted
check allow --public 'git add "."'       # R3 loss: same, quoted dot
check allow ""       'git push "-f" origin main' # R2 loss: the quote character breaks the "-f" adjacency the short-flag-bundle regex requires

echo "-- alias-bypass (\git) and path-qualified git: R1/R2's plain 'git' substring match still finds these; R3's anchor does not (rewrite, accepted for the bulk-add case) --"
check deny  ""       '\git push --force origin main'
check allow --public '\git add -A' # R3 loss: literal string differs from 'git add -A' by the leading backslash
check deny  ""       '/usr/bin/git push --force origin main'

echo "-- a quoted string that merely mentions git is not an invocation --"
check allow ""       'echo "git add -A is dangerous"'

echo "-- force-push suppression must not be reachable from a later command (fixed for good this time: removing the --force-with-lease suppression check entirely, not just re-scoping it, is what closes this) --"
check deny  ""       'git push --force origin main && git push --force-with-lease origin dev'
check deny  ""       'git push --force origin main; echo "should have used --force-with-lease"'
check deny  ""       'git push --force origin main # prefer --force-with-lease'
check deny  ""       'git push --force origin main
echo --force-with-lease'

echo "-- old tokenizer bulk-add tail-scanning cases: R3's anchor no longer catches any tail variant that isn't itself one of the six canonical whole-command strings (rewrite, accepted) --"
check allow --public 'git add -- .'      # R3 loss
check allow --public 'git add ./'        # R3 loss
check allow --public 'git add :/'        # R3 loss
check allow --public 'git add -v -A'     # R3 loss
check allow --public 'git add -u -A'     # R3 loss
check allow --public 'git add --verbose -A' # R3 loss
check allow --public 'git add \
-A' # R3 loss: line-continuation form, not an exact match either

echo "-- near-misses after the rewrite: most still allow; one now over-denies by design (accepted) --"
check allow ""       'git push --force-with-lease origin main'
check allow --public 'git add file.txt; ls -A'
check deny  ""       'git push origin main; rm -f /tmp/x' # accepted over-denial: R2 is a raw substring check with no concept of "this -f belongs to a different command in the chain" — the command contains 'git', the word 'push', and a bounded '-f' (from 'rm -f'), so it denies even though the actual git push has no force flag. Split into two tool calls, or run 'rm -f' separately, to avoid this.
check allow --public 'git add -p'
check allow --public 'git add .gitignore'

echo "-- 'git stage' is git's own synonym for 'add' --"
check deny  --public 'git stage -A'
check deny  ""       'git stage .claude/context/x'

echo "-- 2026-09-09 rewrite regression cases: the exact recorded tokenizer bugs, now fixed by deleting the tokenizer --"
check allow --public 'git add "notes#todo" -A' # accepted R3 loss, NOT a bug: the old tokenizer's '#'-folding bug made this ALLOW when it should have denied a real 'git add -A'; the fix removes the tokenizer (and its '#'-as-comment-boundary logic) entirely rather than patching it again. Under R3's anchored design this was never going to be a whole-string match in the first place, so it stays allow — but for the right reason now (design, not a parsing bug).
check deny  ""       'git push -m "reason #123" --force origin main' # R2 fix: no tokenizer to have its '#' truncate the tail before --force is seen — plain substring match sees --force regardless
check deny  ""       'git add "note#1" .claude/context/x' # R1 fix: same — no '#'-truncation possible when there's no tokenizer
check deny  ""       'cd /tmp
git push --force origin main' # multi-line sanity check: still denied after the rewrite

echo "-- malformed/empty input: fails open, loudly, never blocks --"
check_failopen "malformed JSON"          'not json at all'
check_failopen "empty stdin"             ''
check_failopen "valid JSON, no command"  '{"tool_name":"Bash","tool_input":{}}'

echo
echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
