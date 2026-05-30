"""
A/B two prompt templates against the same email set.

Fetches emails once, runs analyze_with_ollama twice (default vs tight prompt),
and prints a side-by-side diff. Useful for verifying that a shorter prompt
doesn't regress classification quality before swapping it in as the default.

Do not run alongside `python scanner.py` — both share Ollama VRAM.
"""

import argparse
import time

from scanner import (
    BOLD, DIM, RESET,
    PROMPT_DEFAULT, PROMPT_TIGHT,
    analyze_with_ollama,
    get_gmail_service,
    run_gmail_search,
)


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
    parser = argparse.ArgumentParser(description="A/B compare two prompt templates")
    parser.add_argument("-d", "--days", type=int, default=30)
    parser.add_argument("-n", "--max-emails", type=int, default=100)
    parser.add_argument("--all", action="store_true", help="Include read emails")
    args = parser.parse_args()

    print(f"\n{BOLD}Prompt A/B: PROMPT_DEFAULT vs PROMPT_TIGHT{RESET}\n")
    print(f"  Default prompt: {len(PROMPT_DEFAULT):>5} chars")
    print(f"  Tight prompt  : {len(PROMPT_TIGHT):>5} chars "
          f"({100 * (len(PROMPT_DEFAULT) - len(PROMPT_TIGHT)) // len(PROMPT_DEFAULT)}% shorter)\n")

    service = get_gmail_service(write_access=False)
    print(f"Fetching emails (last {args.days} days, {'all' if args.all else 'unread only'})…")
    emails = run_gmail_search(
        service, days=args.days, unread_only=not args.all, max_results=args.max_emails,
    )
    print(f"  Fetched {len(emails)} emails total\n")

    if not emails:
        print("No emails found.")
        return

    print(f"{BOLD}Running pass 1: PROMPT_DEFAULT…{RESET}")
    t0 = time.time()
    default_results = analyze_with_ollama(emails, prompt_template=PROMPT_DEFAULT)
    t_default = time.time() - t0

    print(f"\n{BOLD}Running pass 2: PROMPT_TIGHT…{RESET}")
    t0 = time.time()
    tight_results = analyze_with_ollama(emails, prompt_template=PROMPT_TIGHT)
    t_tight = time.time() - t0

    default_ids = {r.get("id") for r in default_results if r.get("id")}
    tight_ids = {r.get("id") for r in tight_results if r.get("id")}

    print_email_list(default_results, "PROMPT_DEFAULT (current)", color="\033[36m")
    print_email_list(tight_results, "PROMPT_TIGHT (candidate)", color="\033[32m")

    only_default = [r for r in default_results if r.get("id") and r["id"] not in tight_ids]
    only_tight   = [r for r in tight_results   if r.get("id") and r["id"] not in default_ids]
    in_both      = [r for r in default_results if r.get("id") and r["id"] in tight_ids]

    print(f"\n{BOLD}{'='*70}{RESET}")
    print(f"{BOLD}  DIFF SUMMARY{RESET}")
    print(f"{BOLD}{'='*70}{RESET}")
    print(f"  Both agreed on              : {len(in_both)} email(s)")
    print(f"  Default-only (tight missed) : {len(only_default)}")
    print(f"  Tight-only (default missed) : {len(only_tight)}")
    print(f"  Default elapsed             : {t_default:>6.1f}s")
    print(f"  Tight elapsed               : {t_tight:>6.1f}s "
          f"({100 * (t_default - t_tight) / t_default:+.0f}% vs default)" if t_default else "")

    if only_default:
        print(f"\n{BOLD}\033[36m  -- Default caught, Tight did NOT --{RESET}")
        for r in only_default:
            print(f"    • {r.get('subject','')}")
            print(f"      {DIM}{r.get('from','')} | {r.get('category','')} | {r.get('summary','')[:80]}{RESET}")

    if only_tight:
        print(f"\n{BOLD}\033[32m  -- Tight caught, Default did NOT --{RESET}")
        for r in only_tight:
            print(f"    • {r.get('subject','')}")
            print(f"      {DIM}{r.get('from','')} | {r.get('category','')} | {r.get('summary','')[:80]}{RESET}")

    print(f"\n{BOLD}{'='*70}{RESET}\n")


if __name__ == "__main__":
    main()
