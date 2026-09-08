#!/usr/bin/env python3
"""Check that this repo's Claude context stays split and stays private.

Run from the repo root: python3 docs/superpowers/verify-context-split.py

Checks:
  1. privacy   .claude/context is gitignored, and no line of a private context
               file appears in any tracked file
  2. size      CLAUDE.md's non-guard content stays within budget
  3. pointers  every docs/*.md and .claude/context/*.md has a pointer
  4. imports   no @import directives (imports are eager and cost full tokens)

There is deliberately no coverage check here. Coverage compares against a
pre-split baseline, which only exists during a migration; see the
homelab-scripts or anifillerpedia verifier for that variant.
"""
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
GUARD_HEADING = "## Guardrails"
MAX_NONGUARD = 2000          # everything in CLAUDE.md except the guard block
MIN_LEAK_LINE = 50           # only lines this long are distinctive enough to test

# <!-- ... --> blocks are setup instructions, deleted when the template is
# instantiated. They are excluded from the size budget but reported, so an
# unfilled template does not fail the check and a filled one does not keep them.
COMMENT = re.compile(r"<!--.*?-->", re.S)


def read(p):
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def main():
    claude = read(REPO / "CLAUDE.md")
    local = read(REPO / "CLAUDE.local.md")
    # AniDex: docs/ root and docs/user-guide/ are USER documentation with their
    # own audience. Only docs/dev/ is Claude-facing reference produced by the
    # split, so only that is pointer-checked.
    docs = [p for p in sorted((REPO / "docs" / "dev").glob("*.md"))]
    ctx_dir = REPO / ".claude" / "context"
    ctx = [p for p in sorted(ctx_dir.glob("*.md"))] if ctx_dir.exists() else []
    failures = []

    # 1. privacy
    problems = []
    if ctx_dir.exists() or ctx_dir.is_symlink():
        ignored = subprocess.run(
            ["git", "-C", str(REPO), "check-ignore", ".claude/context"],
            capture_output=True, text=True).returncode == 0
        if not ignored:
            problems.append(".claude/context is NOT gitignored")
        tracked = subprocess.run(["git", "-C", str(REPO), "ls-files"],
                                 capture_output=True, text=True, check=True).stdout.split()
        blobs = {f: read(REPO / f) for f in tracked
                 if pathlib.Path(f).suffix in {".md", ".txt", ".py", ".yml", ".yaml"}}
        for c in ctx:
            for line in read(c).split("\n"):
                line = line.strip()
                if len(line) < MIN_LEAK_LINE:
                    continue
                for f, text in blobs.items():
                    if line in text:
                        problems.append(f"{c.name} content appears in tracked {f}")
                        break
    if problems:
        failures.append("privacy")
        print(f"FAIL privacy: {len(problems)} problem(s)")
        for x in dict.fromkeys(problems):
            print(f"       {x}")
    else:
        print("PASS privacy: private context is not tracked")

    # 2. size
    stripped = COMMENT.sub("", claude)
    n_comments = len(COMMENT.findall(claude))
    guard = ""
    claude, claude_full = stripped, claude
    if GUARD_HEADING in claude:
        after = claude[claude.index(GUARD_HEADING):]
        nxt = after.find("\n## ", 1)
        guard = after if nxt == -1 else after[:nxt]
    nonguard = len(claude) - len(guard)
    if nonguard > MAX_NONGUARD:
        failures.append("size")
        print(f"FAIL size: CLAUDE.md non-guard content is {nonguard} (limit {MAX_NONGUARD}); "
              f"total {len(claude_full)}, guards {len(guard)}")
    else:
        print(f"PASS size: CLAUDE.md non-guard content is {nonguard} (limit {MAX_NONGUARD}); "
              f"total {len(claude_full)}, guards {len(guard)}")
    if n_comments:
        print(f"      note: {n_comments} <!-- --> block(s) excluded from the budget; "
              f"delete them once the template is filled in")

    # 3. pointers
    pointers = claude_full + "\n" + local
    want = docs + ctx
    unpointed = [p.name for p in want if p.name not in pointers]
    if unpointed:
        failures.append("pointers")
        print(f"FAIL pointers: no pointer for {', '.join(unpointed)}")
    else:
        print(f"PASS pointers: all {len(want)} split file(s) pointed to")

    # 4. imports
    bad = [p.name for p in [REPO / "CLAUDE.md", REPO / "CLAUDE.local.md"] + docs + ctx
           if any(l.startswith("@") for l in read(p).split("\n"))]
    if bad:
        failures.append("imports")
        print(f"FAIL imports: @import in {', '.join(bad)}")
    else:
        print("PASS imports: no @import directives")

    if failures:
        print(f"\n{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
