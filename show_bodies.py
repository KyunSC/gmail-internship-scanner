"""Print the full extracted body of emails matching a set of subjects."""

from scanner import get_gmail_service, run_gmail_search, INTERNSHIP_KEYWORDS

SUBJECTS_TO_INSPECT = {
    "Q1 Technologies just posted a 86% match Automation Tester(Banking background) role 9 minutes ago",
    'Your job matches for "Junior Software Quality Engineer" — curated for you from Jobright - 05/15/2026',
    "UI Developer at Capgemini and 8 more jobs in Montreal, QC for you. Apply Now.",
    "Specialist Software Development at CN and 5 more jobs in Montreal, QC for you. Apply Now.",
    "Software Developer at Geo-Plus Inc. and 4 more jobs in Laval, QC for you. Apply Now.",
    "Specialist Software Development at CN and 8 more jobs in Montreal, QC for you. Apply Now.",
}

service = get_gmail_service()
emails = run_gmail_search(service, days=30, unread_only=False, max_results=200)

found = 0
for e in emails:
    if e.get("subject", "").strip() not in SUBJECTS_TO_INSPECT:
        continue
    found += 1
    body = e.get("body", "")
    hits = [kw for kw in INTERNSHIP_KEYWORDS if kw in body.lower()]
    print(f"\n{'='*70}")
    print(f"SUBJECT : {e['subject']}")
    print(f"FROM    : {e['from']}")
    print(f"KW HITS : {hits}")
    print(f"{'='*70}")
    # Print body with keyword hits highlighted (surround with >><<)
    display = body
    for kw in hits:
        import re
        display = re.sub(f"({re.escape(kw)})", r">>\1<<", display, flags=re.IGNORECASE)
    print(display[:3000])
    if len(body) > 3000:
        print(f"\n... [{len(body)-3000} more chars truncated]")

print(f"\nShowed {found}/{len(SUBJECTS_TO_INSPECT)} target emails.")
