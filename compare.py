"""
Compare the LLM-based scanner against a pure rule-based full-body parser.
Fetches emails once, runs both pipelines on the same set, and prints a
side-by-side diff. Use it to tune the prompt, adjust keyword rules, or verify
that a code change doesn't cause regressions.

Do not run this alongside `python scanner.py` — both share Ollama and will
contend for VRAM. Run them sequentially.
"""

import argparse

from scanner import (
    BOLD, DIM, RESET,
    _all_intern_listings_excluded,
    _has_internship_signal,
    analyze_with_ollama,
    get_gmail_service,
    run_gmail_search,
)


def rule_based_filter(emails: list[dict]) -> tuple[list[dict], list[dict]]:
    """Pure keyword/rule-based filtering on full email bodies — no LLM involved.
    Returns (kept, dropped)."""
    kept, dropped = [], []
    for e in emails:
        subject = e.get("subject", "")
        body = e.get("body", "")
        sender = e.get("from", "")
        if not _has_internship_signal(subject, body, sender):
            dropped.append({**e, "_drop_reason": "no internship signal"})
        elif _all_intern_listings_excluded(sender, body):
            dropped.append({**e, "_drop_reason": "all listings are Fall 2026 / Sept 2026"})
        else:
            kept.append(e)
    return kept, dropped


def print_email_list(emails: list[dict], label: str, color: str = ""):
    print(f"\n{color}{BOLD}{'='*70}{RESET}")
    print(f"{color}{BOLD}  {label}  ({len(emails)} email(s)){RESET}")
    print(f"{color}{BOLD}{'='*70}{RESET}")
    for e in emails:
        print(f"  {BOLD}{e.get('subject','(no subject)')}{RESET}")
        print(f"  {DIM}From: {e.get('from','')}{RESET}")
        print(f"  {DIM}Date: {e.get('date','')}{RESET}")
        print()


def main():
    parser = argparse.ArgumentParser(description="Compare LLM scanner vs rule-based parser")
    parser.add_argument("-d", "--days", type=int, default=30)
    parser.add_argument("-n", "--max-emails", type=int, default=100)
    parser.add_argument("--all", action="store_true", help="Include read emails")
    args = parser.parse_args()

    print(f"\n{BOLD}Scanner Comparison: LLM vs rule-based{RESET}\n")

    service = get_gmail_service()
    print(f"Fetching emails (last {args.days} days, {'all' if args.all else 'unread only'})…")
    emails = run_gmail_search(
        service, days=args.days, unread_only=not args.all, max_results=args.max_emails
    )
    print(f"  Fetched {len(emails)} emails total\n")

    if not emails:
        print("No emails found.")
        return

    # ── Rule-based (full body, no LLM) ──────────────────────────────────────
    rule_kept, dropped = rule_based_filter(emails)
    rule_ids = {e["id"] for e in rule_kept}

    print_email_list(rule_kept, "RULE-BASED (full-body keyword filter, no LLM)", color="\033[36m")

    # ── Dropped by rule-based ───────────────────────────────────────────────
    print(f"\n\033[31m{BOLD}{'='*70}{RESET}")
    print(f"\033[31m{BOLD}  DROPPED by rule-based  ({len(dropped)} email(s)){RESET}")
    print(f"\033[31m{BOLD}{'='*70}{RESET}")
    for e in dropped:
        print(f"  {BOLD}{e.get('subject','(no subject)')}{RESET}")
        print(f"  {DIM}From: {e.get('from','')}{RESET}")
        print(f"  {DIM}Date: {e.get('date','')}{RESET}")
        print(f"  \033[31mReason: {e.get('_drop_reason','')}{RESET}")
        print()

    # ── LLM-based (the actual scanner pipeline) ─────────────────────────────
    print(f"\n{BOLD}Running LLM scanner pipeline…{RESET}")
    llm_results = analyze_with_ollama(emails)
    llm_ids = {r.get("id") for r in llm_results if r.get("id")}

    print_email_list(llm_results, "LLM-BASED (scanner pipeline)", color="\033[32m")

    # ── Diff ────────────────────────────────────────────────────────────────
    only_rule = [e for e in rule_kept    if e["id"] not in llm_ids]
    only_llm  = [r for r in llm_results  if r.get("id") and r["id"] not in rule_ids]
    in_both   = [e for e in rule_kept    if e["id"] in llm_ids]

    print(f"\n{BOLD}{'='*70}{RESET}")
    print(f"{BOLD}  DIFF SUMMARY{RESET}")
    print(f"{BOLD}{'='*70}{RESET}")
    print(f"  Both agreed on  : {len(in_both)} email(s)")
    print(f"  Rule-only (LLM missed/dropped): {len(only_rule)}")
    print(f"  LLM-only  (rule didn't catch) : {len(only_llm)}")

    if only_rule:
        print(f"\n{BOLD}\033[36m  -- Rule caught, LLM did NOT --{RESET}")
        for e in only_rule:
            print(f"    • {e.get('subject','')}")
            print(f"      {DIM}{e.get('from','')}{RESET}")

    if only_llm:
        print(f"\n{BOLD}\033[32m  -- LLM caught, rule did NOT --{RESET}")
        for r in only_llm:
            print(f"    • {r.get('subject','')}")
            print(f"      {DIM}{r.get('from','')} | {r.get('category','')} | {r.get('summary','')[:80]}{RESET}")

    print(f"\n{BOLD}{'='*70}{RESET}\n")


if __name__ == "__main__":
    main()
