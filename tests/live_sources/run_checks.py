"""Single entry point for every live-source check in this directory.

    python3 -m tests.live_sources.run_checks               # every check
    python3 -m tests.live_sources.run_checks twitter        # one, by name
    python3 -m tests.live_sources.run_checks twitter anime_pictures
    python3 -m tests.live_sources.run_checks --list

Not part of `run_tests.sh` or CI, and never should be - see README.md in
this directory for why. Each check prints its own belief-by-belief
PASS/BELIEF BROKEN/SOURCE UNAVAILABLE report (see harness.py), then this
prints one summary line per check. Exit status is 0 only if every belief
in every check named passed outright.
"""
from __future__ import annotations

import sys
from typing import Dict

from .. import _path  # noqa: F401

from . import (
    check_anime_pictures, check_google_lens_dependency, check_hydrus, check_rule34us, check_twitter,
)
from .harness import Check, Outcome, overall

CHECKS: Dict[str, Check] = {
    "anime_pictures": check_anime_pictures.CHECK,
    "google_lens_dependency": check_google_lens_dependency.CHECK,
    "hydrus": check_hydrus.CHECK,
    "rule34us": check_rule34us.CHECK,
    "twitter": check_twitter.CHECK,
}


def main(argv) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv and argv[0] == "--list":
        for name in CHECKS:
            print(name)
        return 0

    names = argv or list(CHECKS)
    unknown = [name for name in names if name not in CHECKS]
    if unknown:
        print(f"Unknown check(s): {', '.join(unknown)}. --list for the full set.")
        return 2

    outcomes: Dict[str, Outcome] = {}
    for name in names:
        reports = CHECKS[name].run()
        outcomes[name] = overall(reports)
        print()

    print("=== summary ===")
    for name, outcome in outcomes.items():
        print(f"{name}: {outcome.value}")

    # Both non-pass outcomes exit non-zero - a cron job must notice
    # either - but BELIEF_BROKEN and SOURCE_UNAVAILABLE are not the same
    # news, and the summary above (not the exit code alone) is what
    # distinguishes "go fix this" from "the subject is down, try later".
    return 0 if all(outcome is Outcome.PASS for outcome in outcomes.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
