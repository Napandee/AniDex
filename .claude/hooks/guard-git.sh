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

# A token that can never occur in real tokenized input (git subcommands,
# paths, flags) — used below as an explicit, unambiguous invocation
# boundary. \x1f is ASCII Unit Separator: it cannot be typed into a shell
# command by anyone, and it never survives as a literal byte through the
# normalisation this guard already does.
SENTINEL=$'\x1f'

# Normalise the command before tokenizing (2026-09-08 review round 4,
# Critical 1 / Critical 2):
#
# 1. Collapse a backslash immediately followed by a newline (bash's line-
#    continuation idiom, e.g. `git add \` then `  -A` on the next line) into
#    a single space FIRST, before anything else runs. This is the one case
#    where a newline must NOT become an invocation boundary — the two
#    physical lines are one logical command. Done with the classic
#    slurp-the-whole-stream sed idiom (`:a;N;$!ba`) because a plain `s///`
#    only ever sees one line at a time and can't match across the newline
#    it's trying to remove.
# 2. Fold the shell control operators `; & |`, real newlines/carriage
#    returns, and `#` (an unquoted, unescaped `#` starts a comment that runs
#    to end-of-line in real bash — treated the same as a control operator
#    here, since a comment can never contain a live, executable invocation)
#    into SENTINEL — a dedicated boundary TOKEN, not a space. Round 3 folded
#    these into plain spaces so a single `read` could still see every
#    physical line of a multi-line block; that fix must not regress, and it
#    doesn't — SENTINEL still lands in the token stream at every one of
#    those positions, so the walker below (which scans every position, not
#    just the first) still finds every invocation on every line.
#
#    What changes is that git_invocations() below now stops each
#    invocation's argument tail AT the next SENTINEL instead of running to
#    the end of the whole folded string. Previously two chained commands —
#    or a command followed by a comment mentioning the safe flag — were
#    indistinguishable from one, which is exactly how a later
#    `--force-with-lease` could suppress an earlier bare `--force` (round-3
#    fault: false ALLOW on `git push --force origin main && git push
#    --force-with-lease origin dev`, `... ; echo "should have used
#    --force-with-lease"`, and `... # prefer --force-with-lease`). Because
#    every invocation now gets its own bounded `rest`, rule 3 (bulk add) can
#    also safely scan every token of `rest` instead of only the first —
#    closing the `git add -- .` / `git add -v -A` / `git add \`-newline-`-A`
#    gap without reopening any over-denial (see is_bulk_add_token below).
#
#    A stray, unmatched sentinel can only ever narrow a boundary that used
#    to be fuzzy — it can cause a missed *pattern within one invocation's
#    tail* at worst (a false ALLOW risk in theory), which is exactly why
#    this fix's whole point is making sure the boundary lands in the right
#    place (at real control operators/comments/newlines, never mid-token,
#    never eaten by the backslash-newline case). It can never merge two
#    invocations together the way plain-space folding could.
tokenize() {
  printf '%s' "$1" \
    | sed ':a;N;$!ba;s/\\\n/ /g' \
    | tr '\n\r;&|#' "${SENTINEL}${SENTINEL}${SENTINEL}${SENTINEL}${SENTINEL}${SENTINEL}" \
    | sed "s/${SENTINEL}/ ${SENTINEL} /g"
}

# git_invocations <cmd> — for every `git` occurrence in <cmd>, prints one
# line "<subcommand><TAB><rest of that invocation's arguments>". Walks the
# tokens after each `git`: skips any token starting with '-' (a global
# option, whatever it is, per the design note above), and — only for the
# handful of options that take a separate value — also skips the token
# right after it. The first bare (non-'-') token reached is that
# invocation's subcommand; everything after it, UP TO THE NEXT SENTINEL (or
# the end of the token stream, whichever comes first), is its argument
# tail. Every `git` occurrence is walked, not just the first, so
# `git status && git add -A` is checked as two invocations, each with its
# own bounded tail (2026-09-08 review round 4 — see tokenize() above).
git_invocations() {
  local -a toks
  read -ra toks <<< "$(tokenize "$1")"
  local n=${#toks[@]} k t
  for ((k = 0; k < n; k++)); do
    # Strip one matched pair of surrounding quotes ("..." or '...') from a
    # token in place — no subshell fork (2026-09-08 review round 4,
    # Important 3: the previous `toks[k]=$(strip_quotes "${toks[k]}")` paid
    # one fork per token — 6ms for `git status`, 320ms at 400 tokens,
    # 3,385ms at 4,000 tokens, a cost every heredoc-heavy Bash call in six
    # repos paid on every single invocation). Only a WHOLE-token pair
    # counts (the same token starts and ends with the same quote
    # character), not a stray quote left over from a multi-word quoted
    # phrase that tokenizing split apart. `git add "-A"` and `git add -A`
    # are semantically identical (bash strips the quotes before git ever
    # sees them), so every exact-token comparison below must treat them the
    # same, or a quoted flag defeats it silently (2026-09-08 review round
    # 3). Restricting this to whole-token pairs specifically avoids turning
    # a plain quoted string like `echo "git add -A is dangerous"` into what
    # looks like a real git invocation — that phrase tokenizes to a lone
    # leading `"git` and a lone trailing `dangerous"`, neither of which is
    # a matched pair, so neither is stripped, and `"git` correctly still
    # fails the `is_git_token` check above.
    t="${toks[k]}"
    case "$t" in
      \"*\") t="${t#\"}"; t="${t%\"}" ;;
      \'*\') t="${t#\'}"; t="${t%\'}" ;;
    esac
    toks[k]="$t"
  done
  local i j sub rest
  for ((i = 0; i < n; i++)); do
    is_git_token "${toks[i]}" || continue
    j=$((i + 1))
    sub=""
    while [ "$j" -lt "$n" ]; do
      t="${toks[j]}"
      case "$t" in
        "$SENTINEL")
          # This `git` invocation ends (comment, `;`, `&`, `|`, or a real
          # newline) before any subcommand was found — e.g. a bare `git`
          # followed by an unrelated command. Nothing to check.
          j=$((j + 1))
          break
          ;;
        -*)
          if is_value_opt "$t" && [ $((j + 1)) -lt "$n" ]; then
            case "${toks[j + 1]}" in
              -* | "$SENTINEL") : ;;
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
      t="${toks[j]}"
      [ "$t" = "$SENTINEL" ] && break
      rest="$rest $t"
      j=$((j + 1))
    done
    printf '%s\t%s\n' "$sub" "${rest# }"
  done
}

# Tokens that stage the whole working tree / a whole directory. Matched as
# a WHOLE token, never a substring or prefix — `.gitignore` and
# `./docs/FAULTS.md` are real, specific paths and must keep allowing
# (2026-09-08 review round 4, Critical 2 — the old check only ever looked
# at the first token of `rest`, so `git add -v -A`, `git add -- .`,
# `git add ./`, `git add :/`, and a bulk flag pushed past a line-
# continuation all slipped through as real bulk stages).
is_bulk_add_token() {
  case "$1" in
    -A | --all | . | ./ | :/) return 0 ;;
    *) return 1 ;;
  esac
}

rest_has_bulk_add_token() {
  local -a atoks
  read -ra atoks <<< "$1"
  local at
  for at in "${atoks[@]}"; do
    is_bulk_add_token "$at" && return 0
  done
  return 1
}

# --- the three rules, applied per git invocation ----------------------------
while IFS=$'\t' read -r sub rest; do
  case "$sub" in
    add | stage)
      # `git stage` is git's own synonym for `add` (2026-09-08 review round
      # 4, Minor 1) — same two rules apply.
      #
      # 1. Never stage the private context tier or scratch files. All repos.
      if printf '%s' "$rest" | grep -Eq '\.claude/(context|scratch)'; then
        deny "Blocked: staging .claude/context or .claude/scratch. These are gitignored on purpose — context is a symlink to the private claude-context repo, scratch is working files. Committing them published private files to a public repo twice on 2026-09-08 (docs/FAULTS.md). Stage the specific files you meant instead."
      fi
      # 3. Bulk staging. Public repos only — the harm here is publication.
      # Scans every token of this invocation's own argument tail, not just
      # the first (round 4, Critical 2) — safe to do now that `rest` can no
      # longer bleed in from a later command (round 4, Critical 1).
      if [ "$PUBLIC" -eq 1 ] && rest_has_bulk_add_token "$rest"; then
        deny "Blocked: bulk 'git add' in a public repo. It stages whatever happens to be untracked, which is how private files reached a public repo on 2026-09-08 — twice (docs/FAULTS.md). Name the files you actually mean, or 'git add -p'."
      fi
      ;;
    push)
      # 2. Bare force-push. All repos. --force-with-lease is the safe form
      # and passes. Matches --force (exact token) and any short-option
      # bundle containing 'f' (-f, -uf, -fu, ...) — bundled short flags are
      # ordinary git syntax and previously defeated this rule entirely.
      # Both the positive match and the --force-with-lease exemption are
      # now exact per-token comparisons over this invocation's own bounded
      # argument tail (2026-09-08 review round 4, Critical 1) rather than a
      # substring grep over a string that could run into a LATER command —
      # that's what previously let `git push --force origin main &&
      # git push --force-with-lease origin dev` (and the `;`/`#`/newline
      # equivalents) suppress a real bare force-push.
      force_hit=0
      lease_hit=0
      read -ra rtoks <<< "$rest"
      for rt in "${rtoks[@]}"; do
        case "$rt" in
          --force-with-lease) lease_hit=1 ;;
          --force) force_hit=1 ;;
          --*) : ;;
          -*) case "$rt" in *f*) force_hit=1 ;; esac ;;
        esac
      done
      if [ "$force_hit" -eq 1 ] && [ "$lease_hit" -eq 0 ]; then
        deny "Blocked: bare 'git push --force' (or a -f/-uf/-fu... short-flag bundle) overwrites the remote unconditionally. Use --force-with-lease, which refuses if the remote moved since you fetched. If you genuinely need a bare force (a history rewrite), run it yourself outside the agent."
      fi
      ;;
  esac
done < <(git_invocations "$cmd")

exit 0
