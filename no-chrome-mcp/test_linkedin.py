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
    _is_linkedin_noise_sender,
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

INVITATION_SENDER = "Angelo El Hajj <invitations@linkedin.com>"

# A real connection invitation (message 60d sample). Unlike TRANSACTIONAL_BODY
# this one DOES carry a badge run — "Accept View profile 38 connections" — so
# the splitter treats it as a digest and the first chunk becomes the sender's
# profile headline. That headline has an intern keyword ("Co-op Student"), a
# software keyword ("Computer Engineering") and a location ("Laval, QC"), so it
# qualified as a listing. The term gate would now reject it as undated, but the
# sender check is what actually belongs here: an invitation is not job mail
# whatever term a profile headline happens to mention.
INVITATION_BODY = (
    "Angelo is waiting for your response "
    "Angelo El Hajj Computer Engineering Co-op Student | Concordia University "
    "Laval, QC Accept View profile 38 connections in common "
    "More people you may know Nicole Wang CS & Stats @ McGill | "
    "Events Director @ McWiCS View profile View profile"
)

# Term strings under the single strict policy that now applies to every sender,
# LinkedIn included: a listing is on-target only if it explicitly names Summer
# 2027. Silence is off-target, and so is a bare year with no season word.
TERM_CASES = [
    # (chunk text, expected _is_off_target_term)
    ("Intern, AI Solutions (May - August 2027) PSP · Montreal, QC", False),
    ("Software Engineer Intern (Summer 2027) Acme · Montreal, QC", False),
    ("Stagiaire en développement logiciel - Été 2027 Flare · Montreal", False),
    # Undated cards — the shape LinkedIn's exemption used to let through, and
    # exactly the shape of the KPMG card that motivated dropping it.
    ("QC - Stagiaire Développeur Frontend (Angular) KPMG Canada · Montreal, QC", True),
    ("Software Engineer Intern LevelOps · Montreal, QC (Hybrid)", True),
    ("Machine Learning Intern Epic Games · Greater Montreal Area", True),
    # A bare year names no season, so it is not an explicit Summer 2027 term.
    ("QC - Risk Services - Intern Cyber Security 2027 KPMG · Montreal", True),
    # Listings naming a different term were already rejected, and still are.
    ("Machine Learning Intern/Co-op (Winter 2027) Cohere · Canada", True),
    ("Stagiaire DevOps - Automne 2026 Tecsys Inc. · Montreal, QC", True),
    ("Consultant, Internship (Jan-April '27) KPMG Canada · Montreal", True),
    ("Intern - DevOps (Fall 2026) Tecsys Inc. · Montreal, QC", True),
    ("Engineering Student - Software - Fall 2026 MDA Space · QC", True),
    ("Analyst Intern (September 2026) Foo Inc · Montreal, QC", True),
    ("Stagiaire, Génie industriel - Méthodes (Automne 2026) · Montreal", True),
]

# The digest that ended LinkedIn's undated-card exemption (message
# 1a00fcb66bb4519d, 17 Aug 2026). Two cards, neither of them a Summer 2027
# software internship: McKesson's names Fall 2026 AND is a QA role, and KPMG's
# names no term at all. The KPMG card qualified on intern + software + Montreal
# and, being undated, used to count as on-target — so the email was surfaced
# even though the user had checked and found no Summer 2027 role in it.
KPMG_FRONTEND_SUBJECT = "QC - Stagiaire Développeur Frontend (Angular) at KPMG Canada"
KPMG_FRONTEND_BODY = (
    "Jobs at KPMG Canada and more Your job alert for Engineer Intern "
    "New jobs in Montreal match your preferences. "
    "QC - Stagiaire Développeur Frontend (Angular) KPMG Canada · Montreal, QC "
    "(On-site) Actively recruiting "
    "Stagiaire en automatisation de l'assurance qualité (AQ) - Automne 2026 / "
    "QA Automation Engineer Intern - Fall 2026 McKesson · Montreal, QC "
    "Actively recruiting See all jobs Install LinkedIn Widgets "
    "Stay updated at a glance Add widget"
)

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
        check(failures, not (_is_qualifying_listing(c) and not _is_off_target_term(c)),
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
    # The Autodesk listing must survive as its own chunk: per-listing verdicts
    # are only meaningful if one card is one chunk. It is undated, so under the
    # strict term rule it is off-target on its own merits — not because it
    # inherited "Fall 2026" from a neighbour it was fused with.
    autodesk = [c for c in nobadge if "Autodesk" in c]
    check(failures, len(autodesk) == 1 and _is_qualifying_listing(autodesk[0]),
          f"Autodesk listing did not survive as its own qualifying chunk: {autodesk}")
    check(failures, autodesk and "2026" not in autodesk[0],
          f"Autodesk chunk absorbed a neighbouring card's term: {autodesk}")

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

    # 10. Term policy: one strict rule for every sender. A listing is on-target
    #     only if it explicitly names Summer 2027; silence is not evidence of a
    #     summer start, and LinkedIn is no longer exempt from that.
    for text, expected in TERM_CASES:
        got = _is_off_target_term(text)
        check(failures, got is expected,
              f"term verdict {got} != {expected} for {text!r}")

    # 11. The same rule reaches the other aggregators, which never had the
    #     exemption: an undated Wellfound listing is dropped as it always was.
    wellfound_body = (
        "< Software Engineer Intern Nimbus Labs / 11-50 Employees | "
        "In office, Montreal | 0 years of exp | Internship Actively Hiring Learn More <"
    )
    check(failures,
          _all_intern_listings_excluded(WELLFOUND_SENDER, wellfound_body),
          "undated Wellfound listing survived the term gate")

    # 12. Bare "engineering" no longer reads as software, in either direction.
    for text, expected in SOFTWARE_KEYWORD_CASES:
        got = _body_mentions_software(text)
        check(failures, got is expected,
              f"software verdict {got} != {expected} for {text!r}")

    # 13. Connection invitations are not job digests. The body splits like one
    #     and its first chunk qualifies as a listing, so the gate has to reject
    #     it on the sender, before any chunk-level reasoning runs.
    check(failures, _is_linkedin_noise_sender(INVITATION_SENDER),
          "invitations@linkedin.com is not treated as LinkedIn noise")
    check(failures,
          not _has_internship_signal("I want to connect", INVITATION_BODY,
                                     INVITATION_SENDER),
          "connection invitation was surfaced as an internship")
    # The profile headline really does look like a listing — if this stops being
    # true the check above would pass for the wrong reason.
    headline = "Angelo El Hajj Computer Engineering Co-op Student | " \
               "Concordia University Laval, QC"
    check(failures, _is_qualifying_listing(headline),
          "profile headline no longer qualifies — check 13 is now vacuous")
    # ...and the real job senders keep normal handling.
    for addr in ("jobalerts-noreply@linkedin.com", "jobs-noreply@linkedin.com",
                 "jobs-listings@linkedin.com"):
        check(failures, not _is_linkedin_noise_sender(addr),
              f"job sender {addr!r} was wrongly classed as noise")

    # 14. The KPMG frontend digest, end to end. Both cards must be rejected for
    #     their own reason: the McKesson row for being a Fall 2026 QA role, the
    #     KPMG row for naming no term at all.
    kpmg = split(KPMG_FRONTEND_BODY, KPMG_FRONTEND_SUBJECT)
    listings = [c for c in kpmg if " · " in c]
    check(failures, len(listings) == 2,
          f"KPMG frontend digest split into {len(listings)} listing(s), "
          f"expected 2: {kpmg}")
    frontend = [c for c in listings if "KPMG" in c]
    check(failures, len(frontend) == 1 and _is_qualifying_listing(frontend[0]),
          f"KPMG frontend card stopped qualifying — check 14 is now vacuous: "
          f"{frontend}")
    check(failures, frontend and _is_off_target_term(frontend[0]),
          f"undated KPMG frontend card was judged on-target: {frontend}")
    check(failures,
          _all_intern_listings_excluded(LINKEDIN_SENDER, KPMG_FRONTEND_BODY,
                                        KPMG_FRONTEND_SUBJECT),
          "KPMG frontend digest survived the term gate")
    check(failures,
          not (_has_internship_signal(KPMG_FRONTEND_SUBJECT, KPMG_FRONTEND_BODY,
                                      LINKEDIN_SENDER)
               and not _all_intern_listings_excluded(LINKEDIN_SENDER,
                                                     KPMG_FRONTEND_BODY,
                                                     KPMG_FRONTEND_SUBJECT)),
          "KPMG frontend digest would still reach the LLM as an internship")

    if failures:
        print(f"FAILED {len(failures)} check(s):")
        print("\n".join(failures))
        return 1

    print("OK — LinkedIn splitter, header/footer chrome, per-listing filtering, "
          "term policy and engineering keywords all pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
