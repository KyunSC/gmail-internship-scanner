#!/usr/bin/env python3
"""Match/no-match cases for _mentions_location, the Canada-wide location gate.

Plain script, no pytest (requirements.txt has none). Run from cli/:

    venv/bin/python test_location.py

Imports the real function so it tests the shipped regexes rather than a copy.
"""

import sys

from scanner import _mentions_location

# Anywhere in Canada, including remote roles open to Canada. Rows are shaped
# like the location fields of LinkedIn cards and GitHub listings.
MUST_MATCH = [
    "Montreal, QC",
    "Montréal, Québec, Canada",
    "Laval, QC (Hybrid)",
    "Toronto, ON",
    "Vancouver, British Columbia, Canada",
    "Calgary, Alberta, Canada",
    "Waterloo, ON",
    "Ottawa",
    "Toronto, in person",
    "Canada (Remote)",
    "Remote in Canada",
    "Remote - Canada",
    "London, ON",
    "Halifax, NS",
    "Software Engineer Intern Air Canada · Montreal, QC",
]

# Outside Canada, a bare "remote", and Canadian words that aren't locations.
MUST_NOT_MATCH = [
    "Remote in USA",
    "Austin, TX (Remote)",
    "Remote only, Austin",
    "Seattle, WA",
    "New York, NY",
    "KPMG Canada · Seattle, WA",
    "Canadian Tire · New York, NY",
    "Provide responsive onsite and remote IT support",
    "London, UK",
    "New Brunswick, NJ",
    "Ontario, CA",
    "Waterloo, IA",
    "Focus on growth, on time",
    "Remote",
]


def main() -> int:
    failures = []

    for text in MUST_MATCH:
        if not _mentions_location(text):
            failures.append(f"  should MATCH but didn't: {text!r}")

    for text in MUST_NOT_MATCH:
        if _mentions_location(text):
            failures.append(f"  should NOT match but did: {text!r}")

    total = len(MUST_MATCH) + len(MUST_NOT_MATCH)
    if failures:
        print(f"FAILED {len(failures)}/{total} cases:")
        print("\n".join(failures))
        return 1

    print(f"OK — {total}/{total} cases pass "
          f"({len(MUST_MATCH)} match, {len(MUST_NOT_MATCH)} no match)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
