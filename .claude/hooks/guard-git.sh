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

# --- git subcommand extraction (2026-09-08 review, Critical 1 round 2) -----
#
# Enumerating global-option *flags* to detect and skip them is a losing game:
# git has dozens (--no-pager, -p, --bare, --literal-pathspecs, ...) and gains
# more over time, so a guard that only recognizes "-C, -c, --git-dir=,
# --work-tree=" (round 1's fix) is defeated by any other one, e.g.
# `git --bare add -A`. Instead: ANY token starting with '-' is treated as a
# global option and skipped generically, whatever it is — no enumeration
# needed to recognize one. The only flags that must be named explicitly are
# the handful that take their value as a SEPARATE following token (`-C
# <path>`, not `-C=<path>`); without naming those, the walk would misread
# the value token itself as the subcommand. The `--opt=value` single-token
# form needs no special case — it's already one token, skipped like any
# other '-' token.

# Global options that take a separate value token. NOT an attempt to
# enumerate every git flag — only the ones whose value could otherwise be
# mistaken for the subcommand if not skipped along with the flag.
is_value_opt() {
  case "$1" in
    -C | -c | --git-dir | --work-tree | --namespace | --exec-path | --super-prefix) return 0 ;;
    *) return 1 ;;
  esac
}

# Treat a token as `git` if, after stripping a leading backslash (the
# standard `\git` alias/function-bypass idiom — bash runs the literal
# command, skipping any shell alias or function named `git`) and any
# directory prefix (e.g. `/usr/bin/git`), it equals literally `git`
# (2026-09-08 review round 3 — neither form was recognized before).
is_git_token() {
  local t="$1"
  t="${t#\\}"
  t="${t##*/}"
  [ "$t" = "git" ]
}

# Strip one matched pair of surrounding quotes ("..." or '...') from a
# token — but only a WHOLE-token pair (the same token starts and ends with
# the same quote character), not a stray quote left over from a multi-word
# quoted phrase that tokenizing split apart. `git add "-A"` and `git add -A`
# are semantically identical (bash strips the quotes before git ever sees
# them), so every exact-token comparison below must treat them the same, or
# a quoted flag defeats it silently (2026-09-08 review round 3). Restricting
# this to whole-token pairs specifically avoids turning a plain quoted
# string like `echo "git add -A is dangerous"` into what looks like a real
# git invocation — that phrase tokenizes to a lone leading `"git` and a lone
# trailing `dangerous"`, neither of which is a matched pair, so neither is
# stripped, and `"git` correctly still fails the `is_git_token` check above.
strip_quotes() {
  local t="$1"
  case "$t" in
    \"*\") t="${t#\"}"; t="${t%\"}" ;;
    \'*\') t="${t#\'}"; t="${t%\'}" ;;
  esac
  printf '%s' "$t"
}

# Normalise the command before tokenizing: fold embedded newlines, carriage
# returns, and the shell control operators ; & | into plain spaces, then
# tokenize once. `read` treats newline as a RECORD terminator, not just an
# IFS field separator, so `read -ra toks <<< "$multiline_string"` silently
# read only the first physical line and dropped everything after it — an
# ordinary multi-line Bash block (`cd /tmp` then `git add -A` on the next
# line) was invisible to every rule (2026-09-08 review round 3, a Critical
# regression introduced by round 2's switch from grep to a token parser:
# grep checked every line for free, a single `read` call does not). Folding
# these into spaces means the walker below — which already scans every
# position in the token stream for a `git` occurrence, not just the first —
# needs no change itself to see every line. This can only make an
# invocation's argument-tail boundary fuzzier across chained commands,
# never hide a real invocation: over-matching here is acceptable, it can
# only cause a false deny, never a false allow.
tokenize() {
  printf '%s' "$1" | tr '\n\r;&|' ' '
}

# git_invocations <cmd> — for every `git` occurrence in <cmd>, prints one
# line "<subcommand><TAB><rest of that invocation's arguments>". Walks the
# tokens after each `git`: skips any token starting with '-' (a global
# option, whatever it is, per the design note above), and — only for the
# handful of options that take a separate value — also skips the token
# right after it. The first bare (non-'-') token reached is that
# invocation's subcommand; everything after it, to the end of the (already
# control-operator-folded) command, is its argument tail. Every `git`
# occurrence is walked, not just the first, so `git status && git add -A`
# is checked as two invocations. Because control operators are folded to
# spaces before this ever runs, a chained command's argument tail may
# include tokens that "belong" to a later invocation too — accepted
# over-matching (see tokenize() above), never a missed invocation.
git_invocations() {
  local -a toks
  read -ra toks <<< "$(tokenize "$1")"
  local n=${#toks[@]} k
  for ((k = 0; k < n; k++)); do
    toks[k]=$(strip_quotes "${toks[k]}")
  done
  local i j t sub rest
  for ((i = 0; i < n; i++)); do
    is_git_token "${toks[i]}" || continue
    j=$((i + 1))
    sub=""
    while [ "$j" -lt "$n" ]; do
      t="${toks[j]}"
      case "$t" in
        -*)
          if is_value_opt "$t" && [ $((j + 1)) -lt "$n" ]; then
            case "${toks[j + 1]}" in
              -*) : ;;
              *) j=$((j + 1)) ;;
            esac
          fi
          j=$((j + 1))
          ;;
        *)
          sub="$t"
          j=$((j + 1))
          break
          ;;
      esac
    done
    [ -n "$sub" ] || continue
    rest=""
    while [ "$j" -lt "$n" ]; do
      rest="$rest ${toks[j]}"
      j=$((j + 1))
    done
    printf '%s\t%s\n' "$sub" "${rest# }"
  done
}

# --- the three rules, applied per git invocation ----------------------------
while IFS=$'\t' read -r sub rest; do
  case "$sub" in
    add)
      # 1. Never stage the private context tier or scratch files. All repos.
      if printf '%s' "$rest" | grep -Eq '\.claude/(context|scratch)'; then
        deny "Blocked: staging .claude/context or .claude/scratch. These are gitignored on purpose — context is a symlink to the private claude-context repo, scratch is working files. Committing them published private files to a public repo twice on 2026-09-08 (docs/FAULTS.md). Stage the specific files you meant instead."
      fi
      # 3. Bulk staging. Public repos only — the harm here is publication.
      # Only the immediate next argument counts (matches -A/--all/. exactly
      # as a whole token, not e.g. .gitignore or ./docs/FAULTS.md).
      first="${rest%% *}"
      if [ "$PUBLIC" -eq 1 ] && { [ "$first" = "-A" ] || [ "$first" = "--all" ] || [ "$first" = "." ]; }; then
        deny "Blocked: bulk 'git add' in a public repo. It stages whatever happens to be untracked, which is how private files reached a public repo on 2026-09-08 — twice (docs/FAULTS.md). Name the files you actually mean, or 'git add -p'."
      fi
      ;;
    push)
      # 2. Bare force-push. All repos. --force-with-lease is the safe form
      # and passes. Matches --force (but not --force-with-lease) and any
      # short-option bundle containing 'f' (-f, -uf, -fu, ...) — bundled
      # short flags are ordinary git syntax and previously defeated this
      # rule entirely.
      if printf '%s' "$rest" | grep -Eq -- '(--force([^-]|$)|(^|[[:space:]])-[a-zA-Z]*f[a-zA-Z]*([[:space:]]|$))' \
         && ! printf '%s' "$rest" | grep -q -- '--force-with-lease'; then
        deny "Blocked: bare 'git push --force' (or a -f/-uf/-fu... short-flag bundle) overwrites the remote unconditionally. Use --force-with-lease, which refuses if the remote moved since you fetched. If you genuinely need a bare force (a history rewrite), run it yourself outside the agent."
      fi
      ;;
  esac
done < <(git_invocations "$cmd")

exit 0
