#!/usr/bin/env python3
"""Keep/drop cases for TARGET_TERM_REGEX, the Summer-2027 season gate.

Plain script, no pytest (requirements.txt has none). Run from cli/:

    venv/bin/python test_target_term.py

Exits non-zero on the first failing set. Imports the real constant so it tests
the shipped regex rather than a copy of it.
"""

import sys

from scanner import TARGET_TERM_REGEX

# Listings that DO target Summer 2027 — dropping any of these marks a real
# internship read and loses it, which is the failure this gate exists to avoid.
MUST_KEEP = [
    "Summer 2027 Software Engineer Intern",
    "Summer Internship 2027",
    "2027 Summer Internship Program",
    "Summer Analyst 2027",
    "2027 Summer Analyst Program",
    "Summer Technology Analyst 2027",
    "Stage été 2027",
    "Summer '27 internship",
    "summer2027",
    "Summer  2027 internship",
    "SU27 co-op term",
    "S2027 internship",
    "May 2027 - August 2027 internship",
    "May 2027 start",
    "Internship starting June 2027",
    "June 2027 - August 2027 internship",
    "Jun 2027 – Aug 2027",
    "Term: June - August 2027",
    "May to September 2027",
    "Start date: June 1, 2027",
    "starts May 3rd, 2027",
    "Duration: 05/2027 - 08/2027",
    "Start date: 2027-06-01",
    "4-month internship beginning June 2027",
    "16-week term, April 2027 through August 2027",
    "Stage mai 2027",
    "de mai à août 2027",
    "Fall 2026 and Summer 2027 openings",
]

# Listings for some other term (or no term at all). Keeping one of these only
# costs an unread email, but each is a case the gate is meant to catch.
MUST_DROP = [
    "Fall 2027 internship",
    "Winter 2027 (January - April)",
    "Winter 2027 term (Jan 2027 - Apr 2027)",
    "September 2027 - December 2027 co-op",
    "August 2027 - December 2027",
    "Summer 2026 internship",
    "May 2026 - August 2026",
    "Summer 2028 internship",
    "Full-time starting January 2027",
    "Apply by December 2027",
    "internship from July 2027 through September 2027",
    "Start date: 2027-09-01",
    "Duration: 09/2027 - 12/2027",
    "Class of 2027 graduates",
    "we hired 2027 interns last year",
    "reference number 05/2027",
    "version 6/2027",
    "ticket #2027",
]


def main() -> int:
    failures = []

    for text in MUST_KEEP:
        if not TARGET_TERM_REGEX.search(text):
            failures.append(f"  should KEEP but dropped: {text!r}")

    for text in MUST_DROP:
        m = TARGET_TERM_REGEX.search(text)
        if m:
            failures.append(f"  should DROP but kept: {text!r} (matched {m.group(0)!r})")

    total = len(MUST_KEEP) + len(MUST_DROP)
    if failures:
        print(f"FAILED {len(failures)}/{total} cases:")
        print("\n".join(failures))
        return 1

    print(f"OK — {total}/{total} cases pass "
          f"({len(MUST_KEEP)} keep, {len(MUST_DROP)} drop)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
