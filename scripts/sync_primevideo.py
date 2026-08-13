#!/usr/bin/env python3
"""
Prime Video -> AniList sync — NOT YET IMPLEMENTED.

Placeholder for scripts/sync_primevideo.py, tracked in issue #15. Intended
shape, mirroring the existing sync_crunchyroll.py: pull this user's Prime
Video watch history (via Amazon's private/internal API, using their own
authenticated session — Prime Video has no public API for this), diff
against last-known state, and push progress updates to AniList using the
same "never go backwards, never touch score" discipline sync_crunchyroll.py
uses.

Nothing about the actual Prime Video API integration has been researched or
verified as part of this stub — see issue #15's acceptance criteria for what
needs to land before this can be implemented for real, and its Context
section for why it's flagged as an explicit research spike (undocumented
private API, ToS considerations) rather than a known, ready-to-build pattern.

Exit 0 = success, Exit 1 = fatal error. Matches the other scripts/sync_*.py
scripts' contract once implemented.
"""

import sys


def main() -> None:
    raise NotImplementedError(
        "Prime Video sync is not implemented yet — see issue #15 for the "
        "research spike and acceptance criteria that need to land first."
    )


if __name__ == "__main__":
    try:
        main()
    except NotImplementedError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
