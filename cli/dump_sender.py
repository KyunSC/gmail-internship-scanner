"""Print the extracted bodies of emails from a given sender.

Selects by sender substring, so a whole digest sender can be dumped at once.
Used to reverse-engineer a new sender's format (separator marker, header/footer
chrome, body length) before teaching scanner.py about it.

Deliberately uses a raw `from:` query rather than run_gmail_search: the latter's
intern-keyword queries would hide exactly the digests that never say "intern",
which are the ones a new splitter has to be judged against.

    venv/bin/python dump_sender.py wellfound --days 180 --all

Read-only: no batchModify, no Ollama, no writes.
"""

import argparse
import sys

from scanner import (
    BODY_MAX_CHARS,
    INTERNSHIP_KEYWORDS,
    get_gmail_service,
    search_emails,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("sender", help="sender substring, e.g. wellfound or angel.co")
    ap.add_argument("--days", type=int, default=180, help="lookback window (default 180)")
    ap.add_argument("--all", action="store_true", help="include read emails")
    ap.add_argument("--max", type=int, default=50, help="max emails to fetch")
    ap.add_argument("--chars", type=int, default=BODY_MAX_CHARS,
                    help="how much of each body to print (default: the full extracted body)")
    args = ap.parse_args()

    query = f"from:{args.sender} newer_than:{args.days}d"
    if not args.all:
        query += " is:unread"

    service = get_gmail_service()
    print(f"QUERY   : {query}")
    emails = search_emails(service, query, max_results=args.max)
    print(f"MATCHED : {len(emails)} email(s)\n")

    for e in emails:
        body = e.get("body", "")
        hits = [kw for kw in INTERNSHIP_KEYWORDS if kw in body.lower()]
        print("=" * 70)
        print(f"FROM    : {e['from']}")
        print(f"SUBJECT : {e['subject']}")
        print(f"DATE    : {e['date']}")
        print(f"LEN     : {len(body)}"
              f"{'  [AT BODY_MAX_CHARS CAP]' if body.endswith('…[truncated]') else ''}")
        print(f"KW HITS : {hits}")
        print("=" * 70)
        print(body[:args.chars])
        if len(body) > args.chars:
            print(f"\n... [{len(body) - args.chars} more chars not shown]")
        print()

    return 0 if emails else 1


if __name__ == "__main__":
    sys.exit(main())
