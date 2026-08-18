"""Checks for LinkedIn job-alert handling.

No pytest in requirements.txt — same plain-script pattern as test_wellfound.py:
import the real symbols, print failures, exit non-zero.

    venv/bin/python test_linkedin.py

fixtures/linkedin_digest.txt is the real body of Gmail message 1a005ec92ab0d8dd
("QC - Intern Forensic - 2027 at KPMG Canada"), the digest that motivated the
splitter: before it existed the whole body was ONE chunk, and the email was
surfaced on four conditions met by four DIFFERENT listings — "engineering
intern" from a baking-ingredients company, "engineering student" from a
construction firm, "May 2027" from an accounting co-op, and "Montreal" from all
six. No listing in it is a Summer 2027 software internship.
"""

import sys
from pathlib import Path

from scanner import (
    _all_intern_listings_excluded,
    _body_mentions_software,
    _has_internship_signal,
    _is_aggregator,
    _is_off_target_term,
    _is_qualifying_listing,
    _split_aggregator_listings,
    _strip_footer,
)

LINKEDIN_SENDER = "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>"
WELLFOUND_SENDER = "Wellfound <team@hi.wellfound.com>"
FIXTURE = Path(__file__).parent / "fixtures" / "linkedin_digest.txt"
FIXTURE_SUBJECT = "QC - Intern Forensic - 2027 at KPMG Canada"
FIXTURE_LISTING_COUNT = 6

# Cards ending in a SINGLE marker. "Easy Apply" and a bare connection count are
# both complete card endings on real digests, so a separator that demanded a
# multi-badge run would fuse these three into one chunk.
SINGLE_BADGE_BODY = (
    "Jobs at Alpha Corp and more Your job alert for Software Engineer "
    "New jobs in Montreal match your preferences. "
    "Software Engineer Intern Alpha Corp · Montreal, QC (Hybrid) Easy Apply "
    "Data Engineer Intern Beta Inc · Montreal, QC 12 connections "
    "Backend Intern Gamma Ltd · Canada (Remote) Fast growing "
    "See all jobs Install LinkedIn Widgets Stay updated at a glance Add widget"
)

# "Jobs that match your profile" format: no badge on the first two cards, so
# only the location field marks where one card ends. A real digest in this shape
# fused three cards and buried an undated Autodesk software internship inside a
# Fall 2026 chunk.
NO_BADGE_BODY = (
    "View jobs picked for you Jobs that match your profile "
    "Based on your title and location. Update "
    "Intern Software Developer Autodesk · Montreal, QC (Hybrid) "
    "Stagiaire en développement logiciel - Automne 2026 McKesson · Montreal, QC "
    "Intern - DevOps (Fall 2026) Tecsys Inc. · Montreal, QC (Hybrid) Easy Apply "
    "See all jobs Get the new LinkedIn desktop app Also available on mobile"
)

# "Your saved job is still available" format. The referral block quotes real
# people's headlines — "Solution Associate", "Business Analyst" — which are not
# listings at all, and used to fuse onto the saved job that follows.
SAVED_JOBS_BODY = (
    "Apply to your saved jobs. Your saved job at Deloitte is still available. "
    "Analyste - Audit et Assurance TI - Stage 2027 - Montréal Deloitte · "
    "Montreal, Quebec, Canada 2 connections Apply now You have connections at "
    "Deloitte Ask them about the job Tin Tran Solution Associate Message "
    "Jerry Luo Business Analyst Message Your other saved jobs "
    "Software Developer Intern DRW · Montreal, Quebec, Canada 2 connections "
    "See all saved jobs"
)

# Company names that collide with the location vocabulary. "Air Canada ·
# Montreal" must not be cut at "Canada" (the "·" that follows proves it is a
# company, not a location), and "Canadian Tire" must not be cut mid-word.
COMPANY_NAME_COLLISION_BODY = (
    "Your job alert for Software Engineer "
    "Software Developer Intern Canadian Tire · Montreal, QC (Hybrid) Easy Apply "
    "Backend Intern Air Canada · Montreal, QC Actively recruiting "
    "ML Intern KPMG Canada · Montreal, QC (On-site) 1 company alum See all jobs"
)

# Non-digest LinkedIn mail: no badge run, so the splitter must degrade to a
# single whole-body chunk rather than shattering on something else.
TRANSACTIONAL_BODY = (
    "Sunny, your Position is added! Take these next steps for more success. "
    "Add skills to your profile so recruiters can find you."
)

# Term strings, judged under the lenient (LinkedIn) policy. Silence means
# "undated", not "off-target"; only a DIFFERENT term is disqualifying.
LENIENT_TERM_CASES = [
    # (chunk text, expected _is_off_target_term under lenient)
    ("Software Engineer Intern LevelOps · Montreal, QC (Hybrid)", False),
    ("Machine Learning Intern Epic Games · Greater Montreal Area", False),
    ("Intern, AI Solutions (May - August 2027) PSP · Montreal, QC", False),
    ("QC - Risk Services - Intern Cyber Security 2027 KPMG · Montreal", False),
    ("Machine Learning Intern/Co-op (Winter 2027) Cohere · Canada", True),
    ("Stagiaire DevOps - Automne 2026 Tecsys Inc. · Montreal, QC", True),
    ("Consultant, Internship (Jan-April '27) KPMG Canada · Montreal", True),
    ("Intern - DevOps (Fall 2026) Tecsys Inc. · Montreal, QC", True),
    ("Engineering Student - Software - Fall 2026 MDA Space · QC", True),
    ("Analyst Intern (September 2026) Foo Inc · Montreal, QC", True),
    ("Stagiaire, Génie industriel - Méthodes (Automne 2026) · Montreal", True),
]

# Bare "engineering" titles that used to sit in SOFTWARE_KEYWORDS, and the
# software-qualified ones that must keep matching.
SOFTWARE_KEYWORD_CASES = [
    # (text, expected _body_mentions_software)
    ("Engineering Intern AB Mauri North America", False),
    ("engineering intern Howmet Aerospace", False),
    ("Co-op Engineering Student Aecon Group Inc.", False),
    ("Co-op Student, Engineering Aecon Group Inc.", False),
    ("Electrical Engineering Intern AeroCardia", False),
    ("Industrial Engineering Intern Signify", False),
    ("Mechanical Engineering Intern Fenplast", False),
    ("Equity Research Intern Wall Street Oasis", False),
    ("Art Research Internship - Historical Paintings Oak Lores", False),
    ("Software Engineering Intern Acme", True),
    ("Computer Engineering Student Acme", True),
    ("Software Engineer Intern LevelOps", True),
    ("Machine Learning Intern Epic Games", True),
    ("Research Scientist Intern Acme AI", True),
    ("Stagiaire en développement logiciel Flare", True),
]


def check(failures: list[str], cond: bool, msg: str) -> None:
    if not cond:
        failures.append(f"  {msg}")


def split(body: str, subject: str = "") -> list[str]:
    return _split_aggregator_listings(LINKEDIN_SENDER, body, subject)


def main() -> int:
    failures: list[str] = []

    if not FIXTURE.exists():
        print(f"FAILED — missing fixture {FIXTURE}")
        print("Regenerate with: venv/bin/python dump_sender.py linkedin --days 120 --all")
        return 1

    body = _strip_footer(FIXTURE.read_text(encoding="utf-8"))

    check(failures, _is_aggregator(LINKEDIN_SENDER),
          "LinkedIn is not in AGGREGATOR_SENDERS — digests would skip the "
          "per-listing location check")

    # 1. The splitter actually splits. A silent fall-through to [body] is the
    #    failure this assertion exists to catch, so the count is exact.
    chunks = split(body, FIXTURE_SUBJECT)
    check(failures, len(chunks) == FIXTURE_LISTING_COUNT,
          f"fixture split into {len(chunks)} chunk(s), "
          f"expected {FIXTURE_LISTING_COUNT}: {chunks}")

    # 2. No cross-contamination. Each card carries exactly one " · " separating
    #    company from location, so more than one means the chunk spans listings.
    for c in chunks:
        check(failures, c.count(" · ") == 1,
              f"chunk spans multiple listings: {c!r}")

    # 3. Header chrome is gone. The alert is named "Summer Intern" and the
    #    banner adds "New jobs in Montreal", so leaving either in hands the
    #    first card an internship keyword and a location it never earned.
    for phrase in ("Your job alert for", "match your preferences", "Jobs at KPMG"):
        check(failures, all(phrase not in c for c in chunks),
              f"header chrome {phrase!r} survived into a chunk")
    # ...and footer chrome likewise.
    for phrase in ("See all jobs", "Install LinkedIn Widgets"):
        check(failures, all(phrase not in c for c in chunks),
              f"footer chrome {phrase!r} survived into a chunk")

    # 4. The actual bug: no single listing is both wanted and on-target. The
    #    email was surfaced on four conditions met by four different listings.
    for c in chunks:
        check(failures, not (_is_qualifying_listing(c) and not _is_off_target_term(c, True)),
              f"listing wrongly both qualifying and on-target: {c!r}")

    check(failures,
          _all_intern_listings_excluded(LINKEDIN_SENDER, body, FIXTURE_SUBJECT) is True,
          "KPMG digest survived the season gate (cross-listing leak)")

    check(failures,
          not (_has_internship_signal(FIXTURE_SUBJECT, body, LINKEDIN_SENDER)
               and not _all_intern_listings_excluded(LINKEDIN_SENDER, body, FIXTURE_SUBJECT)),
          "KPMG digest would still reach the LLM as an internship")

    # 5. Single-marker card endings still split.
    single = split(SINGLE_BADGE_BODY, "Software Engineer Intern at Alpha Corp")
    listings = [c for c in single if " · " in c]
    check(failures, len(listings) == 3,
          f"single-badge digest split into {len(listings)} listing(s), expected 3: {single}")
    for c in listings:
        check(failures, c.count(" · ") == 1,
              f"single-badge chunk spans multiple listings: {c!r}")

    # 6. Cards with NO badge split on their location field instead.
    nobadge = split(NO_BADGE_BODY, "Intern Software Developer at Autodesk")
    listings = [c for c in nobadge if " · " in c]
    check(failures, len(listings) == 3,
          f"no-badge digest split into {len(listings)} listing(s), expected 3: {nobadge}")
    for c in listings:
        check(failures, c.count(" · ") == 1,
              f"no-badge chunk spans multiple listings: {c!r}")
    # The undated Autodesk listing must survive as its own chunk — fused into
    # the Fall 2026 rows it would be judged off-target and the email dropped.
    autodesk = [c for c in nobadge if "Autodesk" in c]
    check(failures,
          len(autodesk) == 1 and _is_qualifying_listing(autodesk[0])
          and not _is_off_target_term(autodesk[0], True),
          f"undated Autodesk listing did not survive as its own chunk: {autodesk}")

    # 7. Saved-jobs referral chrome is stripped, not fused onto the next job.
    saved = split(SAVED_JOBS_BODY, "Analyste - Audit et Assurance TI at Deloitte")
    for phrase in ("Solution Associate", "Business Analyst", "Ask them about the job"):
        check(failures, all(phrase not in c for c in saved),
              f"referral chrome {phrase!r} survived into a chunk")
    qualifying = [c for c in saved if _is_qualifying_listing(c)]
    check(failures, len(qualifying) == 1 and "DRW" in qualifying[0],
          f"expected exactly the DRW listing to qualify, got {qualifying}")

    # 8. Company names that look like locations don't cause stray cuts.
    collide = split(COMPANY_NAME_COLLISION_BODY,
                    "Software Developer Intern at Canadian Tire")
    check(failures, len(collide) == 3,
          f"company/location collision split into {len(collide)}, expected 3: {collide}")
    for name in ("Canadian Tire", "Air Canada", "KPMG Canada"):
        check(failures, any(name in c for c in collide),
              f"{name!r} was cut apart by the location matcher: {collide}")

    # 9. Non-digest LinkedIn mail degrades to one chunk instead of shattering.
    check(failures, split(TRANSACTIONAL_BODY) == [TRANSACTIONAL_BODY],
          "transactional LinkedIn mail was split as a digest")
    check(failures,
          not _has_internship_signal("Sunny, your Position is added!",
                                     TRANSACTIONAL_BODY, LINKEDIN_SENDER),
          "profile nudge was surfaced as an internship")

    # 10. Term policy: LinkedIn cards state a term so rarely that requiring one
    #    mutes the sender, so silence is tolerated and only a DIFFERENT term
    #    disqualifies.
    for text, expected in LENIENT_TERM_CASES:
        got = _is_off_target_term(text, True)
        check(failures, got is expected,
              f"lenient term verdict {got} != {expected} for {text!r}")

    # ...and the strict policy is unchanged, so an undated listing is still
    # off-target for every other sender.
    check(failures,
          _is_off_target_term("Software Engineer Intern LevelOps · Montreal, QC"),
          "strict policy stopped rejecting an undated listing")
    check(failures,
          not _is_off_target_term("Software Engineer Intern (Summer 2027) Acme", True),
          "an explicitly on-target listing was judged off-target")

    # 11. The leniency is scoped to LinkedIn. Wellfound cards carry enough text
    #     to state a term, so an undated Wellfound listing must still be dropped.
    wellfound_body = (
        "< Software Engineer Intern Nimbus Labs / 11-50 Employees | "
        "In office, Montreal | 0 years of exp | Internship Actively Hiring Learn More <"
    )
    check(failures,
          _all_intern_listings_excluded(WELLFOUND_SENDER, wellfound_body),
          "undated Wellfound listing was rescued by LinkedIn's lenient policy")

    # 12. Bare "engineering" no longer reads as software, in either direction.
    for text, expected in SOFTWARE_KEYWORD_CASES:
        got = _body_mentions_software(text)
        check(failures, got is expected,
              f"software verdict {got} != {expected} for {text!r}")

    if failures:
        print(f"FAILED {len(failures)} check(s):")
        print("\n".join(failures))
        return 1

    print("OK — LinkedIn splitter, header/footer chrome, per-listing filtering, "
          "term policy and engineering keywords all pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
