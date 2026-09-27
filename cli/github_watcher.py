"""
Watch community GitHub internship repos and print Summer 2027 postings you
haven't seen yet, newest first, with the apply link.

The repos publish their listings as structured JSON (.github/scripts/listings.json),
so nothing here parses README tables. The same season, software, QA and
location rules as the Gmail scanner decide what counts. Unchanged repos are
skipped via ETag (HTTP 304), so a quiet re-run downloads nothing.

    venv/bin/python github_watcher.py              # new matches since last run
    venv/bin/python github_watcher.py --no-mark    # preview, remember nothing
    venv/bin/python github_watcher.py --show-all   # every current match
"""

import argparse
import json
import os
import re
import time
from pathlib import Path

import requests

from scanner import (
    BOLD, DIM, RESET,
    SEEN_CACHE_MAX,
    TARGET_TERM_REGEX,
    _body_mentions_software,
    _mentions_excluded_role,
    _mentions_location,
    _normalize_key_part,
    export_json,
)

STATE_PATH = Path(__file__).parent / ".github_seen.json"

# default_year fills in repos that give a bare season ("Summer") per listing.
SOURCES = [
    {
        "name": "simplify",
        "url": "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships"
               "/dev/.github/scripts/listings.json",
        "default_year": "2027",
    },
    {
        "name": "vansh",
        "url": "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships"
               "/dev/.github/scripts/listings.json",
        "default_year": "2027",
    },
]

# Simplify's categories, old and new spellings. Software is trusted outright.
# AI/ML/Data also holds reporting-analyst and GIS roles, so its titles must
# still read as software or AI work. Hardware/Quant/Product rows are kept only
# when the title itself says software, firmware or developer.
SOFTWARE_CATEGORIES = {"Software", "Software Engineering"}
DATA_CATEGORIES = {"AI/ML/Data", "Data Science, AI & Machine Learning"}
_AI_TITLE_RE = re.compile(r"\b(?:AI|ML)\b")
_SOFTWARE_TITLE_RE = re.compile(r"\b(?:software|firmware|developer)\b", re.IGNORECASE)

GREEN = "\033[32m"
YELLOW = "\033[33m"


# ── Fetch & normalize ───────────────────────────────────────────────────────

def fetch_source(src: dict, etag: str | None) -> tuple[list, str | None] | None:
    """Download a source's listings. None means unchanged since `etag` (304).
    Raises requests.RequestException / ValueError on failure."""
    headers = {"If-None-Match": etag} if etag else {}
    resp = requests.get(src["url"], headers=headers, timeout=60)
    if resp.status_code == 304:
        return None
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError(f"expected a JSON array, got {type(data).__name__}")
    return data, resp.headers.get("ETag")


def normalize_listing(raw: dict, src: dict) -> dict:
    """Map either repo schema onto one shape. Simplify gives `terms` as a list;
    vanshb03 gives a yearless `season` string instead."""
    terms = raw.get("terms")
    if not terms:
        season = raw.get("season")
        terms = [f"{season} {src['default_year']}"] if season else []
    return {
        "id": raw.get("id"),
        "company": (raw.get("company_name") or "").strip(),
        "title": (raw.get("title") or "").strip(),
        "locations": list(raw.get("locations") or []),
        "url": raw.get("url") or "",
        "terms": list(terms),
        "category": raw.get("category"),
        "date_posted": int(raw.get("date_posted") or 0),
        "active": bool(raw.get("active")),
        "is_visible": raw.get("is_visible", True) is not False,
        "source": src["name"],
    }


# ── Filter ──────────────────────────────────────────────────────────────────

def _is_software(listing: dict) -> bool:
    category, title = listing["category"], listing["title"]
    if category in SOFTWARE_CATEGORIES:
        return True
    if category in DATA_CATEGORIES:
        return _body_mentions_software(title) or bool(_AI_TITLE_RE.search(title))
    if not category:
        return _body_mentions_software(title)
    return bool(_SOFTWARE_TITLE_RE.search(title))


def is_wanted(listing: dict, days: int, anywhere: bool = False,
              now: float | None = None) -> bool:
    """Open, Summer 2027, software, not QA, in Canada (unless `anywhere`), and
    posted within `days`."""
    now = time.time() if now is None else now
    return (listing["active"] and listing["is_visible"]
            and bool(TARGET_TERM_REGEX.search(" / ".join(listing["terms"])))
            and _is_software(listing)
            and not _mentions_excluded_role(listing["title"])
            and (anywhere or _mentions_location("; ".join(listing["locations"])))
            and now - listing["date_posted"] <= days * 86400)


def listing_key(listing: dict) -> str:
    """Company + title, normalized, so the same role in both repos shows once."""
    return f"{_normalize_key_part(listing['company'])}|{_normalize_key_part(listing['title'])}"


# ── State ───────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        state = json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        state = {}
    state.setdefault("sources", {})
    state.setdefault("seen", {})
    return state


def save_state(state: dict) -> None:
    """Write atomically, keeping the newest SEEN_CACHE_MAX seen keys."""
    seen = sorted(state["seen"].items(), key=lambda kv: kv[1])[-SEEN_CACHE_MAX:]
    state["seen"] = dict(seen)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_PATH)


# ── Watch ───────────────────────────────────────────────────────────────────

def watch(days: int = 30, anywhere: bool = False, show_all: bool = False,
          mark: bool = True) -> tuple[list[dict], dict[str, str]]:
    """Fetch every source and return (listings to show, status per source).

    An ETag is reused only under the same filters it was saved with: after a
    304 nothing is re-filtered, so widening the filters with a stale ETag would
    silently hide listings the old filters dropped."""
    state = load_state()
    now = time.time()
    filters = {"days": days, "anywhere": anywhere}
    matches: dict[str, dict] = {}
    statuses: dict[str, str] = {}
    fetched: dict[str, str | None] = {}

    for src in SOURCES:
        saved = state["sources"].get(src["name"], {})
        etag = None if show_all or saved.get("filters") != filters else saved.get("etag")
        try:
            result = fetch_source(src, etag)
        except (requests.RequestException, ValueError) as exc:
            statuses[src["name"]] = f"error: {exc}"
            continue
        if result is None:
            statuses[src["name"]] = "unchanged"
            continue
        raw_listings, new_etag = result
        for raw in raw_listings:
            if not isinstance(raw, dict):
                continue
            listing = normalize_listing(raw, src)
            if is_wanted(listing, days, anywhere, now):
                matches.setdefault(listing_key(listing), listing)
        fetched[src["name"]] = new_etag
        statuses[src["name"]] = "updated"

    shown = [l for k, l in matches.items() if show_all or k not in state["seen"]]
    shown.sort(key=lambda l: l["date_posted"], reverse=True)

    if mark:
        for key in matches:
            state["seen"].setdefault(key, now)
        for name, etag in fetched.items():
            state["sources"][name] = {"etag": etag, "filters": filters, "fetched_at": now}
        save_state(state)

    return shown, statuses


# ── Display ─────────────────────────────────────────────────────────────────

def _age(ts: int, now: float) -> str:
    secs = max(0, now - ts)
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def display(listings: list[dict], statuses: dict[str, str], show_all: bool) -> None:
    now = time.time()
    label = "matching" if show_all else "new"
    print(f"\n{'='*70}")
    print(f"  {len(listings)} {label} Summer 2027 posting(s) on GitHub")
    print(f"{'='*70}\n")
    for l in listings:
        print(f"  {BOLD}{l['company']}{RESET} — {l['title']}")
        print(f"  {DIM}{'; '.join(l['locations'])} · {_age(l['date_posted'], now)} · {l['source']}{RESET}")
        print(f"  {GREEN}{l['url']}{RESET}\n")
    parts = []
    for name, status in statuses.items():
        color = YELLOW if status.startswith("error") else DIM
        parts.append(f"{color}{name}: {status}{RESET}")
    print(f"  Sources — {' · '.join(parts)}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Show new Summer 2027 internships from GitHub listing repos")
    parser.add_argument("-d", "--days", type=int, default=30,
                        help="Only postings from the last N days (default: 30)")
    parser.add_argument("--anywhere", action="store_true",
                        help="Skip the Canada location filter")
    parser.add_argument("--show-all", action="store_true",
                        help="Show every current match, not just new ones")
    parser.add_argument("--no-mark", action="store_true",
                        help="Preview: don't remember what was shown")
    parser.add_argument("-o", "--output", help="Also export shown listings to a JSON file")
    parser.add_argument("--reset", action="store_true",
                        help="Forget all seen postings and saved ETags")
    args = parser.parse_args()

    if args.reset:
        STATE_PATH.unlink(missing_ok=True)
        print(f"  Removed {STATE_PATH.name}")
        return

    listings, statuses = watch(args.days, args.anywhere, args.show_all, not args.no_mark)
    display(listings, statuses, args.show_all)
    if args.output:
        export_json(listings, args.output)


if __name__ == "__main__":
    main()
