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
    fetch_aggregator_emails,
    get_gmail_service,
    load_scan_cache,
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
        elif _all_intern_listings_excluded(sender, body, subject):
            dropped.append({**e, "_drop_reason": "no listing targets Summer 2027"})
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
    parser.add_argument("-n", "--max-emails", type=int, default=500)
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
    # Fetch exactly the aggregator senders cleanup may touch — never personal
    # mail. A keyword search would miss digests without an intern term (e.g.
    # "X is hiring"), and cleanup only marks emails that were evaluated, so
    # those would stay unread forever.
    print(f"Fetching aggregator emails (last {args.days} days, {'all' if args.all else 'unread only'})…")
    emails = fetch_aggregator_emails(
        service, days=args.days, unread_only=not args.all, max_results=args.max_emails
    )
    print(f"  Fetched {len(emails)} emails total\n")

    if not emails:
        print("No emails found.")
        return

    # ── Cache: skip already-analyzed emails ─────────────────────────────────
    cached = load_scan_cache() or {}
    seen_ids = {e.get("id") for e in cached.get("emails", []) if e.get("id")} - set(cached.get("failed_ids", []))
    if seen_ids:
        fresh = [e for e in emails if e.get("id") not in seen_ids]
        skipped = len(emails) - len(fresh)
        if skipped:
            print(f"  Skipping {skipped} email(s) already in cache; {len(fresh)} new to analyze\n")
        emails = fresh

    if not emails:
        # Nothing new to analyze, but cached decisions are still successful
        # evaluations — clean against them so a dry run followed by --apply
        # actually marks what the dry run listed.
        print("  No new emails since the last scan.\n")
        clean_inbox(
            service, days=args.days, apply=args.apply,
            email_cache={e["id"]: e for e in cached.get("emails", []) if e.get("id")},
            keep_ids=set(cached.get("kept_ids", [])),
            evaluated_ids=seen_ids, failed_ids=set(cached.get("failed_ids", [])),
            listing_keys=cached.get("listing_keys", {}),
        )
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
    # prefilter=False is what makes this a real comparison. scanner.py runs the
    # same keyword/term rules before the LLM to save model calls, which would make
    # the LLM arm a strict subset of the rule arm here — "LLM caught, rule did NOT"
    # could never be non-zero, and the keep-set union below would collapse to
    # rule_ids. Sending every email to the LLM costs more but lets the two arms
    # actually disagree in both directions.
    print(f"\n{BOLD}Running LLM scanner pipeline…{RESET}")
    llm_results = analyze_with_ollama(emails, prefilter=False)
    llm_ids = {r.get("id") for r in llm_results if r.get("id")}

    print_email_list(llm_results, "LLM-BASED (scanner pipeline)", color="\033[32m")

    # ── Diff ────────────────────────────────────────────────────────────────
    only_rule = [e for e in rule_kept    if e["id"] in llm_results.successful_ids and e["id"] not in llm_ids]
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

    # Persist only completed evaluations. Failed reanalyses retain prior decisions.
    evaluated = [e for e in emails if e["id"] in llm_results.successful_ids]
    cache = save_scan_cache(evaluated, [*llm_results, *rule_kept], failed_ids=llm_results.failed_ids)

    # Both pipelines must successfully agree before cleanup. Emails skipped as
    # cached were successfully evaluated on an earlier run; their keep decision
    # lives in the cache's kept_ids, so they're cleanable too.
    keep_ids = rule_ids | llm_ids | set(cache.get("kept_ids", []))
    email_cache = {e["id"]: e for e in cache.get("emails", []) if e.get("id")}
    clean_inbox(
        service, days=args.days, apply=args.apply,
        email_cache=email_cache, keep_ids=keep_ids,
        evaluated_ids=llm_results.successful_ids | seen_ids,
        failed_ids=set(cache.get("failed_ids", [])),
        listing_keys=cache.get("listing_keys", {}),
    )


if __name__ == "__main__":
    main()
