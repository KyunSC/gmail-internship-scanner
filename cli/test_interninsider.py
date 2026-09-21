"""Checks for Intern Insider digest handling.

No pytest in requirements.txt — same plain-script pattern as test_wellfound.py:
import the real symbols, print failures, exit non-zero.

    venv/bin/python test_interninsider.py

Saved-filter names are stripped from cards but may supply the target term to
independently qualifying Intern Insider listings. Another stated term vetoes
inheritance; explicit target terms win. Posting ages must separate cards so
neighbours cannot lend each other software or location keywords.
"""

import sys

from scanner import (
    _all_intern_listings_excluded,
    _inherited_target_term,
    _OTHER_TERM_REGEX,
    _clean_interninsider_body,
    _has_internship_signal,
    _is_aggregator,
    _is_off_target_term,
    _is_qualifying_listing,
    _split_aggregator_listings,
)

SENDER = "Intern Insider <alerts@interninsider.me>"

HEADER = (
    '{n} new match{es} for "{alert}" Hourly Hi Sunny, {n} new internship{s} '
    'matching your saved filter Here are the newest roles matching your '
    '"{alert}" filter. '
)
FOOTER = (
    "Applying early is one of the biggest levers for landing an interview. "
    "These listings tend to fill fast. You're receiving this email because you "
    'created a hourly email alert for "{alert}" in Intern Insider. '
    "Edit your preferences or unsubscribe here ."
)


def digest(*cards: str, truncated: int = 0, alert: str = "Summer 2027",
           ages: list[str] | None = None) -> str:
    """Assemble a real-shaped digest body: chrome, cards each closed by their
    posting age, optional "+ N more" trailer, footer."""
    n = len(cards)
    if ages is None:
        ages = ["just now"] * n
    if len(ages) != n:
        raise ValueError("ages must contain one posting age per card")
    head = HEADER.format(alert=alert, n=n, es="es" if n > 1 else "", s="s" if n > 1 else "")
    body = head + " ".join(f"{c} {age}" for c, age in zip(cards, ages))
    if truncated:
        body += f" + {truncated} more, open in Intern Insider to view "
    return body + " " + FOOTER.format(alert=alert)


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
# An explicit-term Montreal software fixture also survives independently of
# the saved-filter name; qualifying termless cards can now inherit the term.
MONTREAL_SWE = ("Software Developer Intern (Summer 2027) Nimbus Labs "
                "Montreal, Quebec, Canada Build backend services in Python")

LYFT = ("Software Engineer Intern, Backend Lyft Montreal, Quebec, Canada "
        "Own your project")
PWC_SERVICE = ("Cyber as a Service - Summer Intern PwC Montreal, Quebec, Canada "
               "Learn cybersecurity concepts such as detection, response and attack patterns")
PWC_PRIVACY = ("Cyber and Privacy - Summer Intern PwC Montreal, Quebec, Canada "
               "Learn cybersecurity concepts like detection and response")
TECHNOLOGY_STRATEGY = ("Technology Strategy - Summer Intern PwC Montreal, Quebec, Canada "
                       "Work with practitioners to help clients address complex business challenges")
HIVER = ("Stagiaire en développement logiciel - Hiver 2027 Nimbus Labs Montreal, "
         "Quebec, Canada Build backend services in Python")

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

    # 4. Inheritance cannot rescue Calgary/Vancouver cards. Without inheritance,
    #    the individual termless cards still fail the explicit-term check.
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

    # 7. Explicit listing terms still work alongside the inheritance exception:
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

    # Scoped inheritance and mandatory per-listing gates.
    for card in (LYFT, PWC_SERVICE, PWC_PRIVACY):
        check(failures, _has_internship_signal("1 new match", digest(card), SENDER),
              f"qualifying card has no internship signal: {card}")
        check(failures, not _all_intern_listings_excluded(SENDER, digest(card)),
              f"qualifying card failed inheritance: {card}")
    check(failures, _all_intern_listings_excluded(SENDER, digest(LYFT, alert="Montreal SWE")),
          "non-target name rescued termless Lyft")
    check(failures, not _all_intern_listings_excluded(SENDER, digest(MONTREAL_SWE, alert="Montreal SWE")),
          "non-target name blocked explicit target")
    for card in (HIVER, CALGARY_SWE, VANCOUVER_SWE,
                 "Software Developer Nimbus Labs Montreal, Quebec, Canada Build backend services",
                 "Software QA Intern Nimbus Labs Montreal, Quebec, Canada Build backend services"):
        check(failures, _all_intern_listings_excluded(SENDER, digest(card)),
              f"inheritance bypassed a listing gate: {card}")
    check(failures, _inherited_target_term(SENDER.upper(), digest(LYFT).replace('"Summer 2027"', '“Summer 2027”')),
          "curly quotes or case-insensitive sender failed")
    for sender, body in ((SENDER, NEWSLETTER_BODY), ("alerts@example.com", digest(LYFT)),
                         (SENDER, digest(LYFT, alert="Montreal SWE"))):
        check(failures, not _inherited_target_term(sender, body), "unscoped inheritance")
    check(failures, _is_off_target_term(LYFT), "termless card kept without inheritance")
    for inherited in (False, True):
        check(failures, not _is_off_target_term("Winter 2027 and Summer 2027 Software Intern", inherited),
              "explicit target lost precedence")

    chunks = _split_aggregator_listings(SENDER, digest(TECHNOLOGY_STRATEGY, PWC_PRIVACY,
                                                     ages=["1h ago", "just now"]))
    check(failures, chunks == [TECHNOLOGY_STRATEGY, PWC_PRIVACY],
          f"abbreviated age merged cards or leaked text: {chunks}")
    check(failures, not _is_qualifying_listing(TECHNOLOGY_STRATEGY), "Technology Strategy unexpectedly qualifies")
    check(failures, _is_qualifying_listing(PWC_PRIVACY), "Cyber and Privacy unexpectedly fails")
    ages = [f"1{unit} ago" for unit in ("s", "m", "h", "d", "w", "mo")]
    ages += [f"2 {unit}s ago" for unit in ("second", "minute", "hour", "day", "week", "month")]
    ages += [f"1 {unit} ago" for unit in ("second", "minute", "hour", "day", "week", "month")]
    ages += ["just now", "yesterday", "3 h ago", "4 mo ago"]
    for age in ages:
        check(failures, _split_aggregator_listings(SENDER, digest(LYFT, PWC_PRIVACY, ages=[age, age]))
              == [LYFT, PWC_PRIVACY], f"splitter failed {age!r}")
    for prose in ("word1h ago", "1h agog", "unjust now", "yesterdays", "word2 days ago"):
        check(failures, _split_aggregator_listings(SENDER, digest(prose)) == [prose],
              f"splitter matched inside a word: {prose}")
    try:
        digest(LYFT, ages=[])
    except ValueError:
        pass
    else:
        failures.append("ages length was not validated")

    positives = [f"{season} 2027" for season in
                 ("winter", "fall", "autumn", "spring", "hiver", "automne", "printemps")]
    positives += [f"2027 {season}" for season in
                  ("winter", "fall", "autumn", "spring", "hiver", "automne", "printemps")]
    positives += ["Summer 2026", "2026 Summer", "été 2026", "2026 ete", "summer2026",
                  "Summer '26", "Summer ’26", "Summer 26", "Summer internship 2028",
                  "Winter internship 2027"]
    positives += [f"{code}{sep}{year}" for code, years in
                  (("W", ("27", "2027")), ("F", ("27", "2027")),
                   ("S", ("26", "2026")), ("SU", ("26", "2026")))
                  for year in years for sep in ("", " ", "-", " - ", "–")]
    negatives = ["Summer Intern", "Intern Cyber Security 2027", "Summer 2027", "2027 Summer",
                 "summer2027", "Summer '27", "Summer ’27", "Summer 27", "été 2027",
                 "Winter", "Fall", "2026", "midsummer2026", "midsummer 2026",
                 "2026 summerish", "wintergreen 2027", "2027 springboard", "XS26",
                 "SU20260", "Summer 20260", "Summer 126", "Winter 12027",
                 "20260 Summer", "S2027x", "W20270", "W27word"]
    negatives += [f"{code}{sep}{year}" for code in ("S", "SU")
                  for year in ("27", "2027") for sep in ("", " ", "-", " - ", "–")]
    for text in positives:
        check(failures, bool(_OTHER_TERM_REGEX.search(text)), f"other-term regex missed {text!r}")
    for text in negatives:
        check(failures, not _OTHER_TERM_REGEX.search(text), f"other-term regex falsely matched {text!r}")
    for text, excluded in ((HIVER, True), ("Summer 2026", True), ("SU26", True),
                           ("W27", True), (LYFT, False), ("Intern Cyber Security 2027", False),
                           ("Summer Intern", False), ("Summer 2027", False)):
        check(failures, _is_off_target_term(text, inherited=True) == excluded,
              f"inherited decision incorrect for {text!r}")

    if failures:
        print(f"FAILED {len(failures)} check(s):")
        print("\n".join(failures))
        return 1

    print("OK — Intern Insider splitter, chrome stripping, location and season gates all pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
