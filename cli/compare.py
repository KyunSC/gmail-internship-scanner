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
    clean_inbox,
    clear_scan_cache,
    get_gmail_service,
    load_scan_cache,
    run_gmail_search,
    save_scan_cache,
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
            dropped.append({**e, "_drop_reason": "all listings off-target (not Fall 2026 / Winter 2027)"})
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
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Mark aggregator emails as read when BOTH pipelines agree they're "
             "not internships. Dry-run unless this flag is given.",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Delete the scan cache (.last_scan.json) and exported results "
             "(.last_results.json), then exit. The next run re-analyzes every "
             "email from scratch.",
    )
    args = parser.parse_args()

    if args.clear_cache:
        removed = clear_scan_cache()
        if removed:
            print(f"Cleared cache: {', '.join(removed)}")
        else:
            print("Cache already empty — nothing to clear.")
        return

    print(f"\n{BOLD}Scanner Comparison: LLM vs rule-based{RESET}\n")

    service = get_gmail_service(write_access=args.apply)
    print(f"Fetching emails (last {args.days} days, {'all' if args.all else 'unread only'})…")
    emails = run_gmail_search(
        service, days=args.days, unread_only=not args.all, max_results=args.max_emails
    )
    print(f"  Fetched {len(emails)} emails total\n")

    if not emails:
        print("No emails found.")
        return

    # ── Cache: skip already-analyzed emails ─────────────────────────────────
    cached = load_scan_cache() or {}
    seen_ids = {e.get("id") for e in cached.get("emails", []) if e.get("id")}
    if seen_ids:
        fresh = [e for e in emails if e.get("id") not in seen_ids]
        skipped = len(emails) - len(fresh)
        if skipped:
            print(f"  Skipping {skipped} email(s) already in cache; {len(fresh)} new to analyze\n")
        emails = fresh

    if not emails:
        print("  No new emails since the last scan.\n")
        # Nothing new to analyze, but still run cleanup against the accumulated
        # cache so the keep-set logic (and newly-cleanable senders) still apply.
        email_cache = {e["id"]: e for e in cached.get("emails", []) if e.get("id")}
        marked = clean_inbox(
            service, days=args.days, apply=args.apply,
            email_cache=email_cache, keep_ids=set(cached.get("kept_ids", [])),
        )
        if marked:
            save_scan_cache(marked, [])
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

    # ── Save cache ───────────────────────────────────────────────────────────
    # Merge this run's emails + LLM-surfaced ids into the accumulated cache so
    # the next compare run (and scanner.py) skips these emails.
    cache = save_scan_cache(emails, llm_results)

    # ── Cleanup ─────────────────────────────────────────────────────────────
    # Mark aggregator emails as read when BOTH pipelines agreed they're not
    # internships. Keep set = union of what either pipeline surfaced (any
    # disagreement is treated as "keep unread" — safety bias). Emails outside
    # the fetched set still pass through clean_inbox's normal subject safety net.
    keep_ids = rule_ids | llm_ids | set(cache.get("kept_ids", []))
    email_cache = {e["id"]: e for e in cache.get("emails", []) if e.get("id")}
    marked = clean_inbox(
        service, days=args.days, apply=args.apply,
        email_cache=email_cache, keep_ids=keep_ids,
    )
    # Fold the emails we just marked read into the seen-set so they're tracked
    # like analyzed emails (and skipped by future runs). Not added to kept_ids —
    # they were the noise we cleared, not internships.
    if marked:
        save_scan_cache(marked, [])



if __name__ == "__main__":
    main()
