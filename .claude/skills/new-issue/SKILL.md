---
name: new-issue
description: File a new GitHub issue using this repo's task template, check for near-duplicates first, and (if a GitHub Projects roadmap board is documented for this repo) add it to the board with every custom field set from a real read of what it depends on — instead of hand-copying field/option IDs from memory each time. Use when the user asks to file/create/open a new issue, or to "add this to the roadmap".
---

# new-issue

Files a GitHub issue the way this project's conventions require, then wires it onto the
roadmap board if one exists — without you re-deriving cryptic Projects field/option IDs
by hand every session.

## 1. Find the template and duplicate-check first

- Locate the issue template: `.github/ISSUE_TEMPLATE/task.md` (or whatever `.md` file
  exists in that directory — read it, don't assume the section names).
- Before creating anything, search for likely duplicates:
  `gh issue list --search "<key terms>" --state all --limit 15`
  (or `gh search issues` for cross-field search). If something close already exists,
  surface it to the user and ask whether to proceed, reopen, or comment on the existing
  issue instead of filing a new one. Don't silently skip this step.

## 2. Draft the issue

Fill every section of the template with real content — no leftover placeholder text,
no skipped sections unless the template itself says a section is deletable when
inapplicable (e.g. "Open questions — delete if genuinely nothing open"). Write the
Context section so a reader with zero conversation history understands *why* this
exists, not just what it is.

Then create it:
```
gh issue create --title "<title>" --body "<rendered template>" --label "<label if applicable>"
```
Don't assign it yet — per this repo's convention, assignment happens only when work
actually starts, as a separate step (see step 5).

## 3. Find the roadmap board, if one exists

Check `CLAUDE.local.md` (gitignored, local-only — read it directly, it won't be in a
fresh worktree checkout) for a "Roadmap board" section documenting: project number,
owner, and a table of field names → field IDs → option IDs. If `CLAUDE.local.md` has no
such section and no other doc in the repo mentions a GitHub Projects board, **ask the
user** whether one exists before inventing one — don't assume every repo has a board.

If a board is documented but you're not confident the cached IDs are still fresh (the
doc itself should say something like "re-verify if these ever look stale"), that's fine
to trust on the first attempt — only re-fetch if step 4 actually fails.

## 4. Add the issue to the board and set its fields

```
gh project item-add <project-number> --owner <owner> --url <issue-url>
```

Then set each custom field the board defines, using
`--single-select-option-id` writes with the cached IDs from `CLAUDE.local.md`:

```
gh project item-edit --id <item-id> --project-id <project-id> \
  --field-id <field-id> --single-select-option-id <option-id>
```

Base each field's value on a **real read of the issue's actual dependencies** (what it
blocks, what blocks it, how it relates to already-open issues) — not just its label or a
default guess. This mirrors how the board's own documentation says Phase/Priority should
be derived.

If any `item-edit` call fails (e.g. "option not found"), the cached IDs have drifted.
Re-fetch fresh ones with:
```
gh project field-list <project-number> --owner <owner> --format json
```
retry once with the corrected ID, and tell the user the IDs in `CLAUDE.local.md` are
stale and should be updated (don't silently patch that file yourself — the user should
see the diff, since it's their local infra notes).

## 5. Report back

Give the user: the issue URL, confirmation it's on the board with which field values,
and any duplicate-candidates you surfaced in step 1 but didn't block on. Remind them
that assignment (`gh issue edit <n> --add-assignee <owner>`) happens separately, once
work actually starts — don't do it now.
