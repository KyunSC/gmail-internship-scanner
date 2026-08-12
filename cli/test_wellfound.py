"""Checks for Wellfound digest handling.

No pytest in requirements.txt — same plain-script pattern as test_target_term.py:
import the real symbols, print failures, exit non-zero.

    venv/bin/python test_wellfound.py

fixtures/wellfound_digest.txt is a real digest body, saved exactly as
_extract_body produced it BEFORE the Wellfound footer marker was added to
_FOOTER_MARKERS — so the test runs _strip_footer over it the way extraction now
would, and a regression in either the footer marker or the splitter shows up
here.
"""

import sys
from pathlib import Path

from scanner import (
    _all_intern_listings_excluded,
    _has_internship_signal,
    _is_qualifying_listing,
    _is_aggregator,
    _split_aggregator_listings,
    _strip_footer,
)

WELLFOUND_SENDER = "Wellfound <team@hi.wellfound.com>"
FIXTURE = Path(__file__).parent / "fixtures" / "wellfound_digest.txt"

# The fixture's two listings, both at INTELCOM, both QA-flavoured despite the
# software wording — an SDET role and a Quality Assurance role. Montreal and
# in-office, so location passes and the excluded-role test is what must reject
# them. Neither names a term, so the season gate rejects them too.
FIXTURE_LISTING_COUNT = 2

# A digest with one genuinely wanted listing (Montreal, software, Summer 2027)
# buried among full-time and off-location noise. Cards carry the literal
# "| Internship" label Wellfound stamps on every row regardless of the role.
GOOD_BODY = (
    "< Hi Sunny! I've found 3 new jobs that might interest you! "
    "Ready to Interview Open to offers Closed to Offers "
    "Staff Backend Engineer Acme Corp / 51-200 Employees $180-220k | "
    "In office, San Francisco | 8 years of exp | Internship Actively Hiring Learn More < "
    "Software Engineer Intern (Summer 2027) Nimbus Labs / 11-50 Employees | "
    "In office, Montreal | 0 years of exp | Internship Actively Hiring Learn More < "
    "Director of Sales Widgets Inc / 5000+ Employees | Remote only, Austin | "
    "12 years of exp | Internship Actively Hiring B2B Growth Stage +3 Learn More < "
    "Not finding what you had in mind? Try updating your preferences < "
    "You're receiving this notification because you're looking for jobs on Wellfound "
    "228 Park Ave S PMB 40533 · New York, NY 10003-1502 Click here to unsubscribe <"
)

# Same shape, nothing wanted: the software row is San Francisco on-site and the
# Montreal / remote rows are non-software. No single listing carries software +
# location together. Before the splitter existed this whole body was one chunk,
# where "Montreal" from one row plus "Software" from another plus the
# boilerplate "Internship" label were enough to qualify the email.
BAD_BODY = (
    "< Hi Sunny! I've found 3 new jobs that might interest you! "
    "Ready to Interview Open to offers Closed to Offers "
    "Principal Software Architect Acme Corp / 501-1000 Employees | "
    "In office, San Francisco | 12 years of exp | Internship Actively Hiring Learn More < "
    "Regional Account Executive Widgets Inc / 51-200 Employees | "
    "In office, Montreal | 7 years of exp | Internship Actively Hiring Learn More < "
    "Senior Talent Partner Globex / 5000+ Employees | Remote only, Austin | "
    "9 years of exp | Internship Actively Hiring Learn More < "
    "Not finding what you had in mind? Try updating your preferences < "
    "You're receiving this notification because you're looking for jobs on Wellfound "
    "228 Park Ave S PMB 40533 · New York, NY 10003-1502 Click here to unsubscribe <"
)

# Wellfound stamps "| Internship" on every card regardless of the actual role —
# this is the real 2026-04-14 digest, where a Senior (Level 3) analyst wanting
# 12 years of experience is labelled an internship. Keyword filtering cannot
# tell that apart from a real intern listing, so a remote software row like this
# DOES pass _has_internship_signal. The season gate is what drops it, and this
# body pins that division of labour: if the label ever starts qualifying emails
# on its own, the second assertion below fails.
LABEL_NOISE_BODY = (
    "< Hi Sunny! I've found 2 new jobs that might interest you! "
    "Ready to Interview Open to offers Closed to Offers "
    "Salesforce Field Service Administrator Crane NXT / 51-200 Employees $100–120k | "
    "Remote only, Mount Prospect | 5 years of exp | Internship Actively Hiring Learn More < "
    "Senior Data Engineer Lockheed Martin / 5000+ Employees | Remote only, Fort Worth | "
    "12 years of exp | Internship Actively Hiring Learn More < "
    "Not finding what you had in mind? Try updating your preferences < "
    "You're receiving this notification because you're looking for jobs on Wellfound "
    "228 Park Ave S PMB 40533 · New York, NY 10003-1502 Click here to unsubscribe <"
)

# Non-digest Wellfound mail: no "Learn More", so the splitter must degrade to a
# single whole-body chunk rather than shattering on something else.
TRANSACTIONAL_BODY = (
    "Hey Sunny, Welcome to Wellfound! You must verify your email to get started. "
    "Your verification code is: 476416 (valid 15 minutes)"
)


def check(failures: list[str], cond: bool, msg: str) -> None:
    if not cond:
        failures.append(f"  {msg}")


def main() -> int:
    failures: list[str] = []

    if not FIXTURE.exists():
        print(f"FAILED — missing fixture {FIXTURE}")
        print("Regenerate with: venv/bin/python dump_sender.py wellfound --days 180 --all")
        return 1

    body = _strip_footer(FIXTURE.read_text(encoding="utf-8"))

    check(failures, _is_aggregator(WELLFOUND_SENDER),
          "Wellfound is not in AGGREGATOR_SENDERS — digests would skip the "
          "per-listing location check")

    # 1. The splitter actually splits. A silent fall-through to [body] is the
    #    failure this assertion exists to catch, so the count is exact.
    chunks = _split_aggregator_listings(WELLFOUND_SENDER, body)
    listings = [c for c in chunks if "Employees" in c]
    check(failures, len(listings) == FIXTURE_LISTING_COUNT,
          f"fixture split into {len(listings)} listing chunk(s), "
          f"expected {FIXTURE_LISTING_COUNT} (raw chunks: {len(chunks)})")

    # 2. No cross-contamination: a chunk holding two companies means the marker
    #    is wrong and per-listing judgement is judging listing pairs.
    for c in listings:
        check(failures, c.count("Employees") == 1,
              f"chunk spans multiple listings: {c[:80]!r}")

    # 3. Hand-labelled expectations for the fixture: an SDET role and a Quality
    #    Assurance role, both excluded, both undated.
    for c in listings:
        check(failures, not _is_qualifying_listing(c),
              f"QA/undated listing wrongly qualified: {c[:80]!r}")

    check(failures,
          not _has_internship_signal(
              "New jobs: Software Developer in Test Intern at INTELCOM COURIER "
              "CANADA and 1 more jobs", body, WELLFOUND_SENDER),
          "all-QA fixture digest was surfaced as an internship")

    # 4. Synthetic digests: the wanted listing survives, the all-full-time one
    #    does not.
    check(failures,
          _has_internship_signal("New jobs: Staff Backend Engineer at Acme Corp "
                                 "and 2 more jobs", GOOD_BODY, WELLFOUND_SENDER),
          "buried Montreal Summer 2027 intern listing was not surfaced")
    check(failures,
          not _all_intern_listings_excluded(WELLFOUND_SENDER, GOOD_BODY),
          "digest containing a Summer 2027 listing was dropped by the season gate")

    check(failures,
          not _has_internship_signal("New jobs: Principal Software Architect at "
                                     "Acme Corp and 2 more jobs", BAD_BODY,
                                     WELLFOUND_SENDER),
          "all-full-time digest was surfaced (cross-listing keyword union)")

    # Wellfound's blanket "| Internship" label means a senior remote software
    # row still passes the keyword gate; the season gate is what must drop it.
    label_subject = ("New jobs: Salesforce Field Service Administrator at "
                     "Crane NXT and 1 more jobs")
    check(failures,
          _all_intern_listings_excluded(WELLFOUND_SENDER, LABEL_NOISE_BODY),
          "undated full-time digest survived the season gate")
    check(failures,
          not (_has_internship_signal(label_subject, LABEL_NOISE_BODY, WELLFOUND_SENDER)
               and not _all_intern_listings_excluded(WELLFOUND_SENDER, LABEL_NOISE_BODY)),
          "12-years-of-experience digest would reach the LLM as an internship")

    # 5. Only the qualifying listing is season-checked. Without the splitter the
    #    undated rows would rescue an off-target digest, and vice versa.
    good_chunks = _split_aggregator_listings(WELLFOUND_SENDER, GOOD_BODY)
    qualifying = [c for c in good_chunks if _is_qualifying_listing(c)]
    check(failures, len(qualifying) == 1 and "Nimbus Labs" in qualifying[0],
          f"expected exactly the Nimbus Labs listing to qualify, got {qualifying}")

    # 6. Non-digest Wellfound mail degrades to one chunk instead of shattering.
    check(failures,
          _split_aggregator_listings(WELLFOUND_SENDER, TRANSACTIONAL_BODY)
          == [TRANSACTIONAL_BODY],
          "transactional Wellfound mail was split as a digest")
    check(failures,
          not _has_internship_signal("Action Required: Verify Your Account Email",
                                     TRANSACTIONAL_BODY, WELLFOUND_SENDER),
          "verification email was surfaced as an internship")

    if failures:
        print(f"FAILED {len(failures)} check(s):")
        print("\n".join(failures))
        return 1

    print("OK — Wellfound splitter, per-listing filtering and season gate all pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
