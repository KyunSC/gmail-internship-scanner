"""
Mark read the job-alert emails in which no posting works, per the plan from
plan_postings.py. The only step of the /internships flow that changes Gmail.

    cd cli
    venv/bin/python .claude/skills/verify-postings/mark_read.py --failed p3 p7 [--dry-run]

An email is marked read only if every one of its postings was skipped by the
planner or is listed in --failed (❌ in Chrome). Matches, almost-matches,
unsure and unchecked postings keep their emails unread, and so does an email
held on its subject.
"""

import argparse
import json
import sys
import time
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(CLI_DIR))

from plan_postings import PLAN_PATH  # noqa: E402
from scanner import CLEAN_INBOX_SENDERS, get_gmail_service  # noqa: E402

# A plan describes the inbox at one moment; older than this, re-plan.
PLAN_MAX_AGE_HOURS = 12


def emails_to_mark(plan: dict, failed: set[str]) -> tuple[list[dict], list[tuple[dict, str]]]:
    """(emails to mark read, [(email kept unread, why)])."""
    known = {p["id"] for p in plan["postings"]}
    unknown = failed - known
    if unknown:
        raise ValueError(f"not in the plan: {', '.join(sorted(unknown))}")
    to_mark, kept = [], []
    for e in plan["emails"]:
        if e.get("hold"):
            kept.append((e, e["hold"]))
            continue
        if not any(s in (e.get("from") or "").lower() for s in CLEAN_INBOX_SENDERS):
            kept.append((e, "sender is not a job-alert address"))
            continue
        working = [p for p in e["check"] if p not in failed]
        if working:
            kept.append((e, f"posting(s) {', '.join(working)} not failed"))
        else:
            to_mark.append(e)
    return to_mark, kept


def main() -> None:
    parser = argparse.ArgumentParser(description="Mark read the emails whose postings all failed")
    parser.add_argument("--failed", nargs="*", default=[], help="Posting ids that failed in Chrome (❌)")
    parser.add_argument("--plan", default=str(PLAN_PATH), help="Plan file from plan_postings.py")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be marked; change nothing")
    args = parser.parse_args()

    plan = json.loads(Path(args.plan).read_text())
    age_h = (time.time() - plan["generated_at"]) / 3600
    if age_h > PLAN_MAX_AGE_HOURS:
        sys.exit(f"Plan is {age_h:.0f}h old — re-run plan_postings.py first.")
    try:
        to_mark, kept = emails_to_mark(plan, set(args.failed))
    except ValueError as exc:
        sys.exit(f"Refusing: {exc}")

    print(f"\n  {len(to_mark)} email(s) to mark read · {len(kept)} kept unread\n")
    for e, why in kept:
        print(f"  keep  {e['subject'][:70]}\n        ({why})")
    for e in to_mark:
        print(f"  read  {e['subject'][:70]}")
    if args.dry_run or not to_mark:
        print("\n  Gmail not changed." + (" (dry run)" if args.dry_run else ""))
        return

    service = get_gmail_service(write_access=True)
    ids = [e["id"] for e in to_mark]
    for i in range(0, len(ids), 1000):
        service.users().messages().batchModify(
            userId="me", body={"ids": ids[i:i + 1000], "removeLabelIds": ["UNREAD"]},
        ).execute(num_retries=3)
    print(f"\n  Marked {len(ids)} email(s) read.")


if __name__ == "__main__":
    main()
