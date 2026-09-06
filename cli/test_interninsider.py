"""Checks for Intern Insider digest handling.

No pytest in requirements.txt — same plain-script pattern as test_wellfound.py:
import the real symbols, print failures, exit non-zero.

    venv/bin/python test_interninsider.py

The regression this file exists to pin: alerts@interninsider.me was not in
AGGREGATOR_SENDERS, so its mail took the non-aggregator path — no location
filter and no per-listing split. Every body was one chunk, and that chunk
carried the saved alert's NAME ("Summer 2027") from the header and footer
chrome. Since the name IS the term _is_off_target_term looks for, the season
gate was answered by the digest's own subject rather than by any listing, and
could never drop an Intern Insider email. Meanwhile the surfacing decision
collapsed to "does the word 'software' appear anywhere in the digest", which
kept Vancouver and Calgary internships the user cannot take.
"""

import sys

from scanner import (
    _all_intern_listings_excluded,
    _clean_interninsider_body,
    _has_internship_signal,
    _is_aggregator,
    _is_off_target_term,
    _is_qualifying_listing,
    _split_aggregator_listings,
)

SENDER = "Intern Insider <alerts@interninsider.me>"

HEADER = (
    '{n} new match{es} for "Summer 2027" Hourly Hi Sunny, {n} new internship{s} '
    'matching your saved filter Here are the newest roles matching your '
    '"Summer 2027" filter. '
)
FOOTER = (
    "Applying early is one of the biggest levers for landing an interview. "
    "These listings tend to fill fast. You're receiving this email because you "
    'created a hourly email alert for "Summer 2027" in Intern Insider. '
    "Edit your preferences or unsubscribe here ."
)


def digest(*cards: str, truncated: int = 0) -> str:
    """Assemble a real-shaped digest body: chrome, cards each closed by their
    posting age, optional "+ N more" trailer, footer."""
    n = len(cards)
    head = HEADER.format(n=n, es="es" if n > 1 else "", s="s" if n > 1 else "")
    body = head + " ".join(f"{c} just now" for c in cards)
    if truncated:
        body += f" + {truncated} more, open in Intern Insider to view "
    return body + " " + FOOTER


# Real listings from the live corpus. None are Montreal, none are remote.
CALGARY_SWE = ("Software Engineering Intern Hexagon Manufacturing Intelligence "
               "Calgary, Alberta, Canada Implement tools and automation to "
               "enhance developer productivity")
VANCOUVER_SWE = ("Software Engineering Intern Rivian and Volkswagen Group "
                 "Technologies Vancouver, British Columbia, Canada Design, "
                 "develop, and test software features within an existing "
                 "infotainment, mobile, or connectivity application")
TORONTO_STRATEGY = ("Student, Junior Strategy Analyst (AI Strategy) Sun Life "
                    "Toronto, Ontario, Canada Develop executive-ready "
                    "presentations and strategic recommendations for senior "
                    "leadership")
# The one shape that should survive: Montreal, software, and a term stated by
# the listing itself rather than inherited from the alert name.
MONTREAL_SWE = ("Software Developer Intern (Summer 2027) Nimbus Labs "
                "Montreal, Quebec, Canada Build backend services in Python")

# Weekly marketing newsletter from the same domain. No card structure and no
# header sentence, so the splitter must decline it rather than shatter it on a
# stray relative-time phrase.
NEWSLETTER_BODY = (
    "View image: ( Caption: # What's up, Insider! \U0001f44b --- ### Here's what "
    "we have for you this week: * Featured internships from **NVIDIA, Figma, "
    "KPMG,** + more! * Take a look at our Summer 2027 board, updated 2 days ago "
    "* Software engineering roles in Montreal and beyond"
)


def check(failures: list[str], cond: bool, msg: str) -> None:
    if not cond:
        failures.append(f"  {msg}")


def main() -> int:
    failures: list[str] = []

    # 1. The sender is an aggregator, which is what routes it to the
    #    per-listing intern + software + location + season gate.
    check(failures, _is_aggregator(SENDER),
          "interninsider.me is not recognized as an aggregator")

    # 2. Chrome is stripped: neither "Summer 2027" echo survives cleaning, and
    #    the "+ N more, open in Intern Insider to view" trailer goes too — the
    #    literal "Intern Insider" in it reads as an intern keyword.
    cleaned = _clean_interninsider_body(digest(CALGARY_SWE, truncated=4))
    check(failures, "Summer 2027" not in cleaned,
          f"alert-name echo survived cleaning: {cleaned!r}")
    check(failures, "Intern Insider" not in cleaned,
          f'"+ N more" trailer survived cleaning: {cleaned!r}')
    check(failures, "Hexagon" in cleaned,
          f"cleaning ate the listing: {cleaned!r}")

    # 3. Cards split one-per-listing on their posting age.
    chunks = _split_aggregator_listings(SENDER, digest(CALGARY_SWE, VANCOUVER_SWE))
    check(failures, len(chunks) == 2,
          f"expected 2 chunks, got {len(chunks)}: {chunks}")
    check(failures, all("just now" not in c for c in chunks),
          f"posting age leaked into a chunk: {chunks}")

    # 4. The season gate is no longer answered by the alert name. This is the
    #    core regression: before the split, "Summer 2027" from the header made
    #    _all_intern_listings_excluded False for every Intern Insider email.
    body = digest(CALGARY_SWE, VANCOUVER_SWE)
    check(failures, _all_intern_listings_excluded(SENDER, body),
          "off-target digest rescued by the alert name in its own chrome")
    check(failures, all(_is_off_target_term(c) for c in chunks),
          f"a card claimed the target term it never states: {chunks}")

    # 5. Out-of-province internships are dropped on location, which the
    #    non-aggregator path never checked at all.
    for label, card in (("Calgary", CALGARY_SWE), ("Vancouver", VANCOUVER_SWE)):
        b = digest(card)
        check(failures, not _has_internship_signal("1 new match", b, SENDER),
              f"{label} internship surfaced despite the location filter")

    # 6. Toronto business-strategy roles are dropped as well — the LLM arm kept
    #    one of these, in violation of both its location and full-time rules.
    b = digest(TORONTO_STRATEGY)
    check(failures, not _has_internship_signal("1 new match", b, SENDER),
          "Toronto strategy-analyst role surfaced")

    # 7. A wanted listing still gets through, and does so on its own merits:
    #    Montreal, software, and Summer 2027 stated by the card itself.
    good = digest(MONTREAL_SWE, CALGARY_SWE, TORONTO_STRATEGY)
    check(failures, _has_internship_signal("3 new matches", good, SENDER),
          "digest containing a Montreal Summer 2027 internship was dropped")
    check(failures, not _all_intern_listings_excluded(SENDER, good),
          "on-target Montreal listing was rejected by the season gate")
    qualifying = [c for c in _split_aggregator_listings(SENDER, good)
                  if _is_qualifying_listing(c) and not _is_off_target_term(c)]
    check(failures, len(qualifying) == 1 and "Nimbus Labs" in qualifying[0],
          f"expected exactly the Nimbus Labs listing to qualify, got {qualifying}")

    # 8. Weekly newsletters from the same domain degrade to one chunk instead of
    #    shattering on the "2 days ago" that appears in their prose.
    check(failures,
          _split_aggregator_listings(SENDER, NEWSLETTER_BODY) == [NEWSLETTER_BODY],
          "marketing newsletter was split as if it were a card digest")

    if failures:
        print(f"FAILED {len(failures)} check(s):")
        print("\n".join(failures))
        return 1

    print("OK — Intern Insider splitter, chrome stripping, location and season gates all pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
