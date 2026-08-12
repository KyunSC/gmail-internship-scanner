#!/usr/bin/env python3
"""Read-only audit: which already-processed emails does the new season gate rescue?

The scan cache (.last_scan.json) stores only headers — no bodies — so it can't
answer "did this fix matter" on its own: month-name date ranges live in bodies.
This script re-fetches each cached message from Gmail, re-runs
_all_intern_listings_excluded under BOTH the old and the new TARGET_TERM_REGEX,
and prints every email that flips dropped -> kept.

Those are the emails the old regex cost you: with kept_ids empty, every cached
email matching a cleanable aggregator sender was marked read. They still exist
in Gmail — the flipped ones are worth marking unread again by hand.

Strictly read-only: read-only OAuth scope, no batchModify, no cache writes, no
Ollama. Run from cli/:

    venv/bin/python audit_cache_terms.py
    venv/bin/python audit_cache_terms.py --limit 40
"""

import argparse
import re
import sys

import scanner
from scanner import (
    BODY_MAX_CHARS,
    _all_intern_listings_excluded,
    _extract_body,
    _season_checked_chunks,
    get_gmail_service,
    load_scan_cache,
)

# The season gate as it was before the month-name fix, kept verbatim so the
# comparison reflects what actually shipped rather than a paraphrase of it.
OLD_TARGET_TERM_REGEX = re.compile(
    r"\b(summer|[ée]t[ée])\b.{0,20}\b2027\b"
    r"|\b2027\b.{0,20}\b(summer|[ée]t[ée])\b"
    r"|\bmay\s*\.?\s*2027\b"
    r"|\bmai\s*\.?\s*2027\b"
    r"|\b[s][-\s]?2027\b",
    re.IGNORECASE,
)

BOLD = "\033[1m"
GREEN = "\033[92m"
DIM = "\033[2m"
RESET = "\033[0m"


def _excluded_under(regex, sender: str, body: str) -> bool:
    """_all_intern_listings_excluded evaluated with `regex` as the season gate.

    _is_off_target_term reads the module global, so swapping it is the only way
    to get the old verdict out of the current code path. Restored in a finally
    so a fetch error can't leave the module patched."""
    saved = scanner.TARGET_TERM_REGEX
    scanner.TARGET_TERM_REGEX = regex
    try:
        return _all_intern_listings_excluded(sender, body)
    finally:
        scanner.TARGET_TERM_REGEX = saved


def _new_match(sender: str, body: str) -> str:
    """The substring that rescues this email, for eyeballing the verdict."""
    for chunk in _season_checked_chunks(sender, body):
        m = scanner.TARGET_TERM_REGEX.search(chunk)
        if m:
            return m.group(0)
    return ""


def fetch_body(service, msg_id: str) -> tuple[str, str]:
    """(sender, body) for one message, shaped exactly like search_emails does —
    same _extract_body, same BODY_MAX_CHARS truncation — so the filter sees what
    it saw during the original scan."""
    msg = service.users().messages().get(
        userId="me", id=msg_id, format="full"
    ).execute(num_retries=3)
    payload = msg.get("payload", {})
    headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
    body = _extract_body(payload)
    if len(body) > BODY_MAX_CHARS:
        body = body[:BODY_MAX_CHARS] + " …[truncated]"
    return headers.get("From", "unknown"), body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0,
                        help="audit only the N most recent cached emails "
                             "(default: all; 199 sequential fetches is slow)")
    args = parser.parse_args()

    cache = load_scan_cache()
    if not cache:
        print("No scan cache found — nothing to audit.")
        return 1

    cached = cache.get("emails", []) or []
    kept_ids = set(cache.get("kept_ids", []) or [])
    if args.limit > 0:
        cached = cached[-args.limit:]
    if not cached:
        print("Scan cache holds no emails — nothing to audit.")
        return 1

    print(f"{BOLD}Auditing {len(cached)} cached email(s) "
          f"({len(kept_ids)} were kept by the pipeline){RESET}")
    print(f"{DIM}Read-only: fetching bodies, re-running the season gate. "
          f"Nothing is marked read or unread.{RESET}\n")

    service = get_gmail_service(write_access=False)

    flipped, unchanged, errors = [], 0, 0
    for i, rec in enumerate(cached, 1):
        msg_id = rec.get("id")
        if not msg_id:
            continue
        try:
            sender, body = fetch_body(service, msg_id)
        except Exception as e:
            errors += 1
            print(f"  [!] {msg_id}: fetch failed ({e})")
            continue

        old_dropped = _excluded_under(OLD_TARGET_TERM_REGEX, sender, body)
        new_dropped = _excluded_under(scanner.TARGET_TERM_REGEX, sender, body)
        if old_dropped and not new_dropped:
            flipped.append({
                "id": msg_id,
                "subject": rec.get("subject", "(no subject)"),
                "from": rec.get("from", sender),
                "date": rec.get("date", ""),
                "match": _new_match(sender, body),
            })
        else:
            unchanged += 1

        if i % 25 == 0:
            print(f"{DIM}  …{i}/{len(cached)} fetched{RESET}")

    print(f"\n{BOLD}{'='*70}{RESET}")
    print(f"{BOLD}  Flipped dropped -> kept: {len(flipped)}{RESET}")
    print(f"{BOLD}{'='*70}{RESET}")
    for f in flipped:
        print(f"\n  {GREEN}{BOLD}{f['subject']}{RESET}")
        print(f"    from: {f['from']}")
        print(f"    date: {f['date']}")
        print(f"    id:   {f['id']}")
        print(f"    {GREEN}matched: {f['match']!r}{RESET}")

    print(f"\n{DIM}unchanged: {unchanged}   fetch errors: {errors}{RESET}")
    if flipped:
        print(f"\n{BOLD}These were marked read by the old gate. "
              f"Search each id in Gmail to mark it unread again.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
