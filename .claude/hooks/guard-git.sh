#!/usr/bin/env bash
# .claude/hooks/guard-git.sh — PreToolUse(Bash) guard.
#
# Denies three git patterns that caused real, recorded faults. Each denial names
# the fault; see docs/FAULTS.md.
#
# Install (.claude/settings.json):
#   public repo : "args": ["--public"]   — adds the bulk-add rule
#   private repo: no args                — bulk-add is convenience there, not risk
#
# Exit 0 with no stdout means "no decision": the normal permission flow runs.
#
# --- design (2026-09-09 rewrite) ---------------------------------------------
#
# The previous version of this guard was a hand-rolled tokenizer: it split the
# command into shell tokens, walked them to find each `git` invocation's
# subcommand and argument tail, and matched rules against those tokens. Five
# successive rounds of that approach each passed their own growing test suite
# while carrying a complete bypass — see docs/FAULTS.md, "five guard versions
# passed their own tests while wide open". Two of those five bypasses came
# directly from the tokenizer's own machinery: an unquoted `#` inside an
# argument was folded as a comment boundary and truncated the tail BEFORE the
# dangerous token was ever scanned (`git add "notes#todo" -A` → ALLOW, because
# the tokenizer didn't know that `#` was inside quotes), and the tokenizer's
# own sentinel byte (`\x1f`, chosen because it "cannot be typed into a shell
# command") turned out to be reachable through the JSON hook input, letting a
# crafted argument inject a fake invocation boundary and make the walker skip
# whole invocations. Each round's fix added more parsing to close the
# previous round's gap, and the added parsing created the next gap. Precision
# over raw command text bought nothing durable and cost five rounds.
#
# This rewrite deletes the tokenizer entirely — no token walk, no quote
# stripping, no sentinel, no subcommand extraction, no global-option
# skipping. In its place: three `grep -E` checks over the raw command string.
# `grep -E` evaluates every line of its input by default, so multi-line
# commands (heredocs, line continuations, `cmd1\ncmd2`) are handled for free,
# with no parsing at all.
#
# The trade-off this buys: precision is gone. These are substring/whole-line
# checks, not "is this token really part of a live git invocation." A `git`
# and a `push` and a `--force` that all happen to appear in one command will
# deny it even if they're in unrelated parts of a chain (e.g. inside an echo
# string, or in a later unrelated command). That is accepted and expected —
# over-denial is the deliberate cost of never bypassing again. A blocked
# command can always be split into two separate tool calls or run outside the
# agent; a bypassed guard cannot un-happen.
set -uo pipefail

PUBLIC=0
[ "${1:-}" = "--public" ] && PUBLIC=1

# Fail-open by design: if this guard can't parse its input (missing/broken jq,
# malformed JSON, an unexpected payload shape), it must not block every Bash
# call in the session for the rest of it — fail-closed here would make the
# guard itself an outage. But silent fail-open defeats the point of a guard,
# so a parse failure is surfaced loudly on stderr instead of disappearing.
if ! cmd=$(cat | jq -r '.tool_input.command // empty' 2>/dev/null); then
  echo "guard-git.sh: WARNING: could not parse hook input (jq failed or is missing) — NOT enforcing this call, allowing by default." >&2
  exit 0
fi
if [ -z "$cmd" ]; then
  echo "guard-git.sh: WARNING: hook input had no .tool_input.command — NOT enforcing this call, allowing by default." >&2
  exit 0
fi

deny() {
  jq -nc --arg r "$1" '{hookSpecificOutput:{hookEventName:"PreToolUse",
    permissionDecision:"deny", permissionDecisionReason:$r}}'
  exit 0
}

# --- matching-only normalisation (2026-09-09) --------------------------------
# Seven self-blocks in one day while building and documenting this guard led
# here. These transformations build $match_cmd, used by R1 and R2 below; $cmd
# itself is untouched. Each targets one observed false positive and nothing
# wider — see docs/FAULTS.md entries 12 and 13.
#
# (a) `worktree add`, `remote add` and `submodule add` are the only git
#     subcommands taking a bare `add`. R1 matched the word `add` anywhere, so
#     `git worktree add /tmp/wt <private-path>` was denied. Renaming the verb
#     in those three phrases removes the false match while leaving any real
#     staging in the same command fully visible to R1.
match_cmd=$(printf '%s' "$cmd" | sed -E 's/(worktree|remote|submodule)([[:space:]]+)add/\1\2ADD_SUBCMD/g')

# (b) The argument of a message flag is prose, not a command. A commit message
#     or PR body describing what this guard matches was itself denied — this
#     guard blocked the pull request that introduced it. Deliberately scoped to
#     message flags: stripping ALL quoted spans would let a quoted private path
#     through and reopen R1.
match_cmd=$(printf '%s' "$match_cmd" | sed -E \
  -e 's/(-m|--message|--body|--title)([[:space:]]+)"[^"]*"/\1\2MSGARG/g' \
  -e "s/(-m|--message|--body|--title)([[:space:]]+)'[^']*'/\1\2MSGARG/g")

# --- R1 — private paths (all repos) -----------------------------------------
# Never stage the private context tier or scratch files. Deny when the
# command mentions git, mentions add/stage as a word, and references one of
# the private path prefixes.
if printf '%s' "$match_cmd" | grep -Eq 'git' \
   && printf '%s' "$match_cmd" | grep -Eqw 'add|stage' \
   && printf '%s' "$match_cmd" | grep -Eq '\.claude/(context|scratch)'; then
  deny "Blocked: command references git, add/stage, and .claude/context or .claude/scratch. These are gitignored on purpose — context is a symlink to the private claude-context repo, scratch is working files. Committing them published private files to a public repo twice on 2026-09-08 (docs/FAULTS.md). Stage the specific files you meant instead. If you are only DESCRIBING these commands (a doc, a heredoc, a test probe), write the file with an editor tool rather than a shell heredoc — this guard matches command text and cannot tell description from execution."
fi

# --- R2 — bare force-push (all repos) ---------------------------------------
# --force-with-lease is the safe form and must keep passing. `--force([^-]|$)`
# already excludes it on its own — no separate suppression check is needed or
# wanted. A prior round's redundant "does the command also mention
# --force-with-lease somewhere" check is exactly what caused a recorded
# Critical: a LATER mention (a chained safe push, a trailing comment) disarmed
# the rule for an earlier bare --force.
#
# 2026-09-09: evaluated PER COMMAND-SEGMENT rather than over the whole string.
# Splitting on ; & | means an unrelated `rm -rf` in a chain no longer trips a
# git push elsewhere in it (`git push origin main && rm -rf /tmp/x` was denied
# — ordinary cleanup). It is also STRICTER: a second push later in a chain is
# now caught, which the whole-string match could miss. Deliberately splits on
# ; & | and newlines ONLY — no sentinel byte, no comment handling; those two
# produced the last two Criticals.
force_hit=0
while IFS= read -r seg; do
  printf '%s' "$seg" | grep -Eq 'git' || continue
  printf '%s' "$seg" | grep -Eqw 'push' || continue
  if printf '%s' "$seg" | grep -Eq -- '--force([^-]|$)|(^|[[:space:]])-[a-zA-Z]*f[a-zA-Z]*([[:space:]]|$)'; then
    force_hit=1
    break
  fi
done <<SEGMENTS
$(printf '%s' "$match_cmd" | tr ';&|' '\n')
SEGMENTS
if [ "$force_hit" -eq 1 ]; then
  deny "Blocked: bare 'git push --force' (or a -f/-uf/-fu... short-flag bundle) overwrites the remote unconditionally. Use --force-with-lease, which refuses if the remote moved since you fetched. If you genuinely need a bare force (a history rewrite), run it yourself outside the agent."
fi

# --- R3 — bulk add (public repos only) --------------------------------------
# Deliberately narrow: an anchored, whole-trimmed-command match against the
# exact handful of "stage everything" invocations, nothing else. This is a
# documented weakening, not an oversight: precise detection of bulk-add
# (catching it wherever it appears in a chain, e.g. `cd /tmp && git add -A`)
# needs per-token parsing to tell a real invocation from an unrelated mention
# — and five rounds of building exactly that parser each produced a new
# bypass (see docs/FAULTS.md). This rule catches the exact recorded fault
# (`git add -A` / `git add .` / `git add --all` run as the whole command, and
# their `git stage` synonyms) and knowingly misses chained/prefixed forms.
trimmed="$(printf '%s' "$cmd" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
if [ "$PUBLIC" -eq 1 ]; then
  case "$trimmed" in
    'git add -A' | 'git add .' | 'git add --all' | 'git stage -A' | 'git stage .' | 'git stage --all')
      deny "Blocked: bulk 'git add' in a public repo. It stages whatever happens to be untracked, which is how private files reached a public repo on 2026-09-08 — twice (docs/FAULTS.md). Name the files you actually mean, or 'git add -p'."
      ;;
  esac
fi

exit 0
