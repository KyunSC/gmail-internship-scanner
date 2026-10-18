"""
Plan the Chrome pass over unread job-alert digests, read-only.

Splits each unread aggregator email into its postings, marks each posting
"skip" (with the reason) or "check", and attaches the posting's own link from
the email's HTML. The same posting repeated across digests is checked once.
Never changes Gmail: emails are marked read later by
.claude/skills/verify-postings/mark_read.py, and only once Chrome has found
that none of an email's postings work.

    venv/bin/python plan_postings.py                # unread digests, last 30 days
    venv/bin/python plan_postings.py --days 60      # widen the window
    venv/bin/python plan_postings.py --all          # include read emails
"""

import argparse
import base64
import json
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from scanner import (
    AGGREGATOR_SENDERS,
    BOLD, DIM, RESET,
    TARGET_TERM_REGEX,
    _body_mentions_internship,
    _body_mentions_software,
    _extract_body,
    _is_aggregator,
    _is_indeed_noise_sender,
    _is_linkedin_noise_sender,
    _is_off_target_term,
    _mentions_excluded_role,
    _mentions_location,
    _normalize_body,
    _split_aggregator_listings,
    _subject_mentions_internship,
    aggregator_query,
    get_gmail_service,
)

PLAN_PATH = Path(__file__).parent / ".postings_plan.json"
SNIPPET_CHARS = 240

# Footer and account links, never a posting. Matched against the whole anchor
# text so a real title that merely starts with "Help" ("Help Desk Intern")
# survives.
_CHROME_LINK_TEXT_RE = re.compile(
    r"(?:unsubscribe|privacy(?: policy)?|help(?: cent(?:re|er))?|settings|preferences"
    r"|terms\b.*|view in browser|manage\b.*|see (?:all|more)\b.*|view more jobs"
    r"|find jobs|sign in|pause these emails|edit\b.*|create|your job alert.*"
    r"|jobs similar to.*|this is a (?:bad|good) match|yes|no|app store|google play"
    r"|add widget|learn why.*|glassdoor|indeed|linkedin)",
    re.IGNORECASE,
)
_LINKEDIN_JOB_RE = re.compile(r"https://www\.linkedin\.com/(?:comm/)?jobs/view/(\d+)")
# Prefix length used when a posting's link text and its body text differ in
# what they include (Intern Insider's link wraps the whole card; the card text
# drops its posting age).
_MATCH_PREFIX = 40
_MIN_MATCH = 12


class _LinkExtractor(HTMLParser):
    """Collect (anchor text, href) for every <a> with an http(s) href, in
    document order."""

    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            text = " ".join(" ".join(self._text).split())
            if self._href.startswith("http"):
                self.links.append((text, self._href))
            self._href = None


def _html_parts(payload: dict) -> list[str]:
    out = []
    data = payload.get("body", {}).get("data")
    if data and payload.get("mimeType") == "text/html":
        out.append(base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace"))
    for sub in payload.get("parts", []) or []:
        out.extend(_html_parts(sub))
    return out


def _norm(text: str) -> str:
    return _normalize_body(text).casefold()


# ── Postings and their links ────────────────────────────────────────────────

def skip_reason(chunk: str, aggregator: bool = True) -> str | None:
    """Why a posting is skipped without opening it, or None to check it.
    Non-aggregator mail isn't split into listings, so its one chunk is not
    location-checked (see _season_checked_chunks)."""
    if not _body_mentions_internship(chunk):
        return "not an internship"
    if not _body_mentions_software(chunk):
        return "not software/tech"
    if aggregator and not _mentions_location(chunk):
        return "not in Canada"
    if _mentions_excluded_role(chunk):
        return "QA/testing role"
    if _is_off_target_term(chunk):
        return "Winter/Fall term"
    return None


def job_links(raw_links: list[tuple[str, str]], sender: str) -> list[tuple[str, str]]:
    """The links that can point at a posting: chrome text dropped, LinkedIn
    tracking stripped, and for LinkedIn/Glassdoor only their job-page paths."""
    s = (sender or "").lower()
    out, seen = [], set()
    for text, href in raw_links:
        if len(text) < 4 or _CHROME_LINK_TEXT_RE.fullmatch(text.strip()):
            continue
        m = _LINKEDIN_JOB_RE.match(href)
        if m:
            href = f"https://www.linkedin.com/jobs/view/{m.group(1)}/"
        elif "linkedin.com" in s:
            continue
        if "glassdoor" in s and "jobListing" not in href and "/job-listing/" not in href:
            continue
        if (text, href) in seen:
            continue
        seen.add((text, href))
        out.append((text, href))
    return out


def assign_links(chunks: list[str], links: list[tuple[str, str]]) -> dict[int, list[tuple[str, str]]]:
    """Map each link to the posting chunk it belongs to.

    A link matches a chunk when its text sits in the chunk, or (failing that)
    when the two share a prefix. Glassdoor anchors span its ★ separator
    ("Company 4.1 ★ Title Montreal …"), so the part after ★ is tried first.
    Ties go to the first match at or after the previous link's chunk, since
    links and postings run in the same document order, skipping a chunk that
    already holds a link with the same text: two jobs titled "Machine Learning
    Intern" are two postings."""
    nchunks = [_norm(c) for c in chunks]
    out: dict[int, list[tuple[str, str]]] = {}
    texts: dict[int, set[str]] = {}
    cursor = 0
    for text, href in links:
        for part in reversed([p.strip() for p in _norm(text).split("★")]):
            if len(part) < _MIN_MATCH:
                continue
            hits = [i for i, c in enumerate(nchunks) if part in c]
            if not hits:
                hits = [i for i, c in enumerate(nchunks)
                        if part[:_MATCH_PREFIX] in c
                        or (len(c) >= _MIN_MATCH and c[:_MATCH_PREFIX] in part)]
            if hits:
                key = _norm(text)
                i = next((h for h in hits if h >= cursor and key not in texts.get(h, ())), None)
                if i is not None:
                    out.setdefault(i, []).append((text, href))
                    texts.setdefault(i, set()).add(key)
                    cursor = i
                break
    return out


def posting_key(sender: str, chunk: str, links: list[tuple[str, str]]) -> str:
    """Identity of a posting across digests: the job id when the link has one,
    else the sender family plus the link (or posting) text."""
    for _, href in links:
        m = _LINKEDIN_JOB_RE.match(href)
        if m:
            return f"linkedin:{m.group(1)}"
        jl = parse_qs(urlsplit(href).query).get("jobListingId")
        if jl:
            return f"glassdoor:{jl[0]}"
    family = next((d for d in AGGREGATOR_SENDERS if d in (sender or "").lower()),
                  (sender or "").lower())
    basis = links[0][0] if links else chunk
    return f"{family}|{' '.join(_norm(basis).split())[:80]}"


def _title_says_internship(links: list[tuple[str, str]]) -> bool:
    """False only when the posting's own link text, its title, names no
    internship and no Summer 2027. A chunk can borrow "Intern" from a
    neighbouring card or the alert header ("Summer Intern Manage alerts
    C-GE-900 – Développeur(se) …"); the link text can't. Short CTA links
    ("Learn More") say nothing either way, so with no title link it's True."""
    titles = [t for t, _ in links if len(t) >= _MIN_MATCH]
    return not titles or any(_subject_mentions_internship(t) or TARGET_TERM_REGEX.search(t)
                             for t in titles)


def _subject_hold(subject: str) -> bool:
    """The cleanup safety net: a subject naming a Summer 2027 software
    internship keeps the email unread even if every body posting was skipped."""
    return bool(_subject_mentions_internship(subject) and _body_mentions_software(subject)
                and TARGET_TERM_REGEX.search(subject))


def plan_email(msg_id: str, sender: str, subject: str, date: str, body: str,
               raw_links: list[tuple[str, str]]) -> dict:
    """One email's postings, each with its skip reason (None = check) and links."""
    found = job_links(raw_links, sender)
    if _is_indeed_noise_sender(sender) or _is_linkedin_noise_sender(sender):
        chunks, reasons = [body], ["not a job posting (account nudge)"]
        links = {}
    elif "glassdoor" in (sender or "").lower() and found:
        # Each Glassdoor job link wraps its whole card ("Company 4.1 ★ Title
        # Montreal $40K … 3d"), so the links ARE the postings. The ★ split
        # merges a card that has no rating into the one before it.
        by_text: dict[str, list[tuple[str, str]]] = {}
        for text, href in found:
            by_text.setdefault(" ".join(_norm(text).split()), []).append((text, href))
        groups = list(by_text.values())
        chunks = [g[0][0] for g in groups]
        reasons = [skip_reason(c) for c in chunks]
        links = dict(enumerate(groups))
    else:
        aggregator = _is_aggregator(sender)
        chunks = _split_aggregator_listings(sender, body, subject) if aggregator else [body]
        reasons = [skip_reason(c, aggregator) for c in chunks]
        links = assign_links(chunks, found)
    postings = []
    for i, (chunk, reason) in enumerate(zip(chunks, reasons)):
        if not chunk.strip():
            continue
        own = links.get(i, [])
        if reason is None and not _title_says_internship(own):
            reason = "not an internship (title)"
        postings.append({
            "text": chunk[:SNIPPET_CHARS],
            "skip": reason,
            "links": [{"text": t[:SNIPPET_CHARS], "url": u} for t, u in own],
            "key": posting_key(sender, chunk, own),
        })
    return {"id": msg_id, "from": sender, "subject": subject, "date": date,
            "postings": postings}


def build_plan(planned: list[dict], days: int, unread_only: bool,
               now: float | None = None) -> dict:
    """Fold per-email plans into one: unique postings to check (each listing
    the emails it appears in) and, per email, its checks and skips."""
    now = time.time() if now is None else now
    postings: dict[str, dict] = {}
    emails = []
    for e in planned:
        check_ids, skipped = [], []
        for p in e["postings"]:
            if p["skip"]:
                skipped.append({"text": p["text"][:120], "reason": p["skip"]})
                continue
            entry = postings.get(p["key"])
            if entry is None:
                entry = postings[p["key"]] = {
                    "id": f"p{len(postings) + 1}", "key": p["key"], "text": p["text"],
                    "links": p["links"], "emails": []}
            elif not entry["links"] and p["links"]:
                entry["links"] = p["links"]
            if e["id"] not in entry["emails"]:
                entry["emails"].append(e["id"])
            if entry["id"] not in check_ids:
                check_ids.append(entry["id"])
        hold = not check_ids and _subject_hold(e["subject"])
        emails.append({"id": e["id"], "from": e["from"], "subject": e["subject"],
                       "date": e["date"], "check": check_ids, "skipped": skipped,
                       "all_skipped": not check_ids,
                       "hold": "subject names a Summer 2027 software internship" if hold else None})
    return {"generated_at": now, "days": days, "unread_only": unread_only,
            "postings": list(postings.values()), "emails": emails}


# ── Gmail (read-only) ───────────────────────────────────────────────────────

def fetch_messages(service, days: int, unread_only: bool) -> list[dict]:
    """Every aggregator email cleanup could touch, full payload. format=full
    does not change the UNREAD label."""
    query = aggregator_query(days, unread_only)
    ids, page = [], None
    while True:
        resp = service.users().messages().list(
            userId="me", q=query, maxResults=500, pageToken=page).execute(num_retries=8)
        ids += [m["id"] for m in resp.get("messages", []) or []]
        page = resp.get("nextPageToken")
        if not page:
            break
    return [service.users().messages().get(userId="me", id=i, format="full").execute(num_retries=8)
            for i in ids]


def plan_message(msg: dict) -> dict:
    payload = msg.get("payload", {})
    headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
    parser = _LinkExtractor()
    for part in _html_parts(payload):
        parser.feed(part)
    return plan_email(msg["id"], headers.get("From", ""), headers.get("Subject", "(no subject)"),
                      headers.get("Date", ""), _extract_body(payload), parser.links)


def write_plan(plan: dict, path: Path = PLAN_PATH) -> None:
    path.write_text(json.dumps(plan, indent=2, ensure_ascii=False))


def display(plan: dict, path: Path = PLAN_PATH) -> None:
    emails, postings = plan["emails"], plan["postings"]
    skipped_only = [e for e in emails if e["all_skipped"] and not e["hold"]]
    held = [e for e in emails if e["hold"]]
    print(f"\n{'='*70}")
    print(f"  {len(emails)} email(s) · {len(postings)} unique posting(s) to check in Chrome")
    print(f"  {len(skipped_only)} email(s) where every posting was skipped · {len(held)} held on subject")
    print(f"{'='*70}\n")
    for p in postings:
        link = p["links"][0]["url"] if p["links"] else "(no link — will be ❓)"
        title = p["links"][0]["text"] if p["links"] else p["text"]
        print(f"  {BOLD}{p['id']}{RESET} {title[:90]}")
        print(f"     {DIM}{link[:110]} · in {len(p['emails'])} email(s){RESET}")
    print(f"\n  {DIM}Gmail was not changed. Plan written to {path.name}.{RESET}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Plan the Chrome pass over unread job-alert digests (read-only)")
    parser.add_argument("-d", "--days", type=int, default=30, help="Look back N days (default: 30)")
    parser.add_argument("--all", action="store_true", help="Include read emails")
    parser.add_argument("-o", "--output", default=str(PLAN_PATH), help="Plan file (default: .postings_plan.json)")
    args = parser.parse_args()

    service = get_gmail_service()
    print(f"Fetching aggregator emails (last {args.days} days, {'all' if args.all else 'unread only'})…")
    messages = fetch_messages(service, args.days, not args.all)
    plan = build_plan([plan_message(m) for m in messages], args.days, not args.all)
    write_plan(plan, Path(args.output))
    display(plan, Path(args.output))


if __name__ == "__main__":
    main()
