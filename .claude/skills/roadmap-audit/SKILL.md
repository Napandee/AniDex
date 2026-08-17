---
name: roadmap-audit
description: Reconcile the GitHub Projects roadmap board against actual open-issue state — missing board entries, and stale cross-issue references left over from renumbering (e.g. an issue's body still pointing at "#69" after a repo recreation renumbered it to "#19"). Read-only reporting, not automatic edits. Use when the user asks to audit, sanity-check, or reconcile the roadmap/board, or periodically via /loop.
---

# roadmap-audit

Surfaces drift between the roadmap board and reality. This is a **reporting** skill —
it lists findings for a human decision, it does not rewrite issue text or board fields
on its own. (`new-issue` is the skill that writes to the board.)

## 1. Gather state

```
gh issue list --state open --limit 100 --json number,title,body,state,labels
gh project item-list <project-number> --owner <owner> --format json --limit 100
```

Get project number/owner from `CLAUDE.local.md`'s roadmap-board section, same as
`new-issue` does. If that section doesn't exist, ask the user rather than guessing.

## 2. Check: every open issue is on the board

Diff open-issue numbers against the board's item numbers. Per this repo's convention,
every new issue filed should land on the board — flag any open issue missing from it.
(A closed issue still showing on the board is expected/harmless — GitHub Projects
doesn't remove closed items automatically, and the repo's own convention says no manual
cleanup is needed there. Don't flag those as a problem.)

## 3. Check: stale cross-issue references

This is the check that matters most and needs actual reading, not just pattern
matching. For every open issue's title + body:

- Extract every `#NN` mention.
- Look up issue `NN` (`gh issue view NN --json number,title,state`).
- Read the sentence the `#NN` reference sits in, and compare it against what issue
  `NN` actually is *now*. Two failure modes to catch:
  - **Doesn't exist / wildly mismatched topic** — near-certain leftover from a
    renumbering (e.g. old-repo numbers after a recreation, like #21 referencing "#69"
    and "#70" when those numbers now belong to nothing, and the actual issues being
    described are #19 and #20).
  - **Exists and topic loosely matches, but state is CLOSED and the referencing text
    reads as a live blocker** ("blocked by #NN", "once #NN lands") — worth flagging
    even if the number itself is technically valid, since the dependency may no longer
    apply.
- False positives are expected and fine — a reference to an already-closed issue used
  as historical context ("this was split off from #12") is not stale. Use judgment on
  the surrounding sentence; don't flag every closed-issue reference indiscriminately.

## 4. Report

Produce a short list, per finding: issue number, the exact reference text, what it
currently resolves to (or "doesn't exist"), and a one-line suggested correction. Do not
edit the issues yourself — ask the user which findings to act on, then apply only the
ones they confirm (`gh issue edit <n> --body "..."` for a body fix, or point them at the
issue's edit UI for anything more involved than a text swap).

If nothing is found, say so plainly rather than padding the report — a clean audit is a
useful result too.
