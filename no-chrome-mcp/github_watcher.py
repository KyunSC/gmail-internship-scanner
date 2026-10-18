"""
Watch community GitHub internship repos and print Summer 2027 postings you
haven't seen yet, newest first, with the apply link.

Most repos publish their listings as structured JSON
(.github/scripts/listings.json); the rest are read from their README table.
The same season, software, QA and location rules as the Gmail scanner decide
what counts. Unchanged repos are skipped via ETag (HTTP 304), so a quiet re-run
downloads nothing.

    venv/bin/python github_watcher.py                    # new matches since last run
    venv/bin/python github_watcher.py --no-mark          # preview, remember nothing
    venv/bin/python github_watcher.py --show-all         # every current match
    venv/bin/python github_watcher.py --include-termless # also postings that name no term
"""

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from scanner import (
    BOLD, DIM, RESET,
    SEEN_CACHE_MAX,
    TARGET_TERM_REGEX,
    _OTHER_TERM_REGEX,
    _body_mentions_software,
    _mentions_excluded_role,
    _mentions_location,
    _normalize_key_part,
    export_json,
)

STATE_PATH = Path(__file__).parent / ".github_seen.json"

# format "json": a listings.json array (Simplify's schema, or vanshb03's with a
# yearless `season` that default_year completes). format "markdown": a README
# table with Company / Role / Location / Apply / Date Posted columns.
SOURCES = [
    {
        "name": "simplify",
        "url": "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships"
               "/dev/.github/scripts/listings.json",
        "format": "json",
        "default_year": "2027",
    },
    {
        "name": "vansh",
        "url": "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships"
               "/dev/.github/scripts/listings.json",
        "format": "json",
        "default_year": "2027",
    },
    {
        "name": "michelle",
        "url": "https://raw.githubusercontent.com/michelleokolie/canada-tech-internships-summer-2027"
               "/main/.github/scripts/listings.json",
        "format": "json",
        "default_year": "2027",
    },
    {
        "name": "negar",
        "url": "https://raw.githubusercontent.com/negarprh/Canadian-Tech-Internships-2027"
               "/main/README.md",
        "format": "markdown",
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
    """Download a source's listings as raw dicts. None means unchanged since
    `etag` (304). Raises requests.RequestException / ValueError on failure."""
    headers = {"If-None-Match": etag} if etag else {}
    resp = requests.get(src["url"], headers=headers, timeout=60)
    if resp.status_code == 304:
        return None
    resp.raise_for_status()
    if src.get("format") == "markdown":
        data = parse_markdown_listings(resp.text)
        if not data:
            raise ValueError("no listings table found in README")
    else:
        data = resp.json()
        if not isinstance(data, list):
            raise ValueError(f"expected a JSON array, got {type(data).__name__}")
    return data, resp.headers.get("ETag")


# Escaped pipes ("Intelcom \| Dragonfly") are part of a cell, not a boundary.
_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")
_MD_LINK_RE = re.compile(r"!?\[([^\[\]]*)\]\([^)\s]*\)")
_MD_URL_RE = re.compile(r"\]\((https?://[^)\s]+)\)")
_MD_EMPHASIS_RE = re.compile(r"[*_`]+")
_LOCATION_SPLIT_RE = re.compile(r"\s*/\s*|<br\s*/?>|;", re.IGNORECASE)


def _md_text(cell: str) -> str:
    """Cell text with links reduced to their label and emphasis removed.
    Innermost first, so a badge inside a link ("[![Apply](img)](url)")
    unwraps to "Apply"."""
    text = cell.replace("\\|", "|")
    while True:
        unwrapped = _MD_LINK_RE.sub(lambda m: m.group(1), text)
        if unwrapped == text:
            break
        text = unwrapped
    return _MD_EMPHASIS_RE.sub("", text).strip()


def _parse_date(text: str) -> int:
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
        return int(dt.replace(tzinfo=timezone.utc).timestamp())
    return 0


def parse_markdown_listings(text: str) -> list[dict]:
    """Read README listing tables into raw dicts shaped like listings.json.

    A table starts at a header row naming Company and Role, and ends at the
    first line that isn't a table row. "↳" repeats the previous company. The
    badge image inside the Apply link is skipped for the real URL; "Closed🔒"
    in place of a link marks a closed posting."""
    rows: list[dict] = []
    header: list[str] | None = None
    last_company = ""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            header = None
            continue
        cells = [c.strip() for c in _CELL_SPLIT_RE.split(line)[1:-1]]
        lowered = [_md_text(c).lower() for c in cells]
        if header is None:
            if "company" in lowered and ("role" in lowered or "title" in lowered):
                header = lowered
            continue
        if all(re.fullmatch(r":?-+:?", c) for c in cells if c):
            continue
        row = dict(zip(header, cells))

        company = _md_text(row.get("company", ""))
        if company.startswith("↳"):
            company = last_company
        else:
            last_company = company
        title = _md_text(row.get("role") or row.get("title") or "")
        apply_cell = row.get("apply", "")
        urls = [u for u in _MD_URL_RE.findall(apply_cell) if "shields.io" not in u]
        location = _md_text(row.get("location", ""))
        rows.append({
            "id": urls[0] if urls else f"{company}|{title}",
            "company_name": company,
            "title": title,
            "locations": [p for p in _LOCATION_SPLIT_RE.split(location) if p.strip()],
            "url": urls[0] if urls else "",
            # A README has no terms column; the title carries it when stated
            # ("Developer Intern (Winter 2027)").
            "terms": [title] if _states_term(title) else [],
            "active": bool(urls) and "🔒" not in apply_cell,
            "is_visible": True,
            "date_posted": _parse_date(_md_text(row.get("date posted", ""))),
        })
    return rows


def normalize_listing(raw: dict, src: dict) -> dict:
    """Map every source schema onto one shape. Simplify gives `terms` as a
    list; vanshb03-style repos give a yearless `season` string instead."""
    terms = raw.get("terms")
    if not terms:
        season = raw.get("season")
        terms = [f"{season} {src['default_year']}"] if season and src.get("default_year") else []
    return {
        "id": raw.get("id"),
        "company": (raw.get("company_name") or "").strip(),
        "title": (raw.get("title") or "").strip(),
        "locations": [str(l).strip() for l in raw.get("locations") or []],
        "url": (raw.get("url") or "").strip(),
        "terms": [str(t) for t in terms],
        "category": raw.get("category"),
        "date_posted": int(raw.get("date_posted") or 0),
        "active": bool(raw.get("active")),
        "is_visible": raw.get("is_visible", True) is not False,
        "source": src["name"],
    }


# ── Filter ──────────────────────────────────────────────────────────────────

def _states_term(text: str) -> bool:
    return bool(TARGET_TERM_REGEX.search(text) or _OTHER_TERM_REGEX.search(text))


def term_stated(listing: dict) -> bool:
    """False when no term names a season and year — Simplify's "N/A", or a
    README title with no "(Summer 2027)"-style suffix."""
    return _states_term(" / ".join(listing["terms"]))


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
              now: float | None = None, include_termless: bool = False) -> bool:
    """Open, Summer 2027 (or no stated term, with `include_termless`),
    software, not QA, in Canada (unless `anywhere`), and posted within `days`.
    An explicit Summer 2027 wins over other terms listed alongside it."""
    now = time.time() if now is None else now
    terms = " / ".join(listing["terms"])
    on_target = bool(TARGET_TERM_REGEX.search(terms)) or (
        include_termless and not term_stated(listing))
    return (listing["active"] and listing["is_visible"]
            and on_target
            and _is_software(listing)
            and not _mentions_excluded_role(listing["title"])
            and (anywhere or _mentions_location("; ".join(listing["locations"])))
            and now - listing["date_posted"] <= days * 86400)


def _normalize_url(url: str) -> str:
    """Scheme/host casefolded, tracking params, fragment and trailing slash
    dropped. Other query params stay — for many job boards they are the job id
    ("?gh_jid=8212179")."""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in ("ref", "source")]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                       parts.path.rstrip("/"), urlencode(query), ""))


# Repos spell the same employer and role slightly differently ("TRC" vs "TRC
# Companies", "Software Engineering Intern" vs "Software Engineer Intern").
# Suffixes after " - " are NOT dropped: "Software Engineer Intern - Backend"
# and "- Fullstack" are separate postings.
_COMPANY_SUFFIX_RE = re.compile(
    r"(?:\s+(?:inc|ltd|llc|corp|corporation|co|company|companies|group"
    r"|technologies|financial|limited))+$")
_ENGINEERING_RE = re.compile(r"\bengineering\b")


def listing_keys(listing: dict) -> list[str]:
    """Every identity a listing is known by. Company + title catches the same
    role listed twice, allowing for small spelling differences; the apply URL
    catches it when the names differ more than that."""
    company = _COMPANY_SUFFIX_RE.sub("", _normalize_key_part(listing["company"]))
    title = _ENGINEERING_RE.sub("engineer", _normalize_key_part(listing["title"]))
    keys = [f"{company}|{title}"]
    if listing["url"]:
        keys.append(f"url:{_normalize_url(listing['url'])}")
    return keys


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
          mark: bool = True, include_termless: bool = False
          ) -> tuple[list[dict], dict[str, str]]:
    """Fetch every source and return (listings to show, status per source).

    An ETag is reused only under the same filters it was saved with: after a
    304 nothing is re-filtered, so widening the filters with a stale ETag would
    silently hide listings the old filters dropped."""
    state = load_state()
    now = time.time()
    filters = {"days": days, "anywhere": anywhere, "include_termless": include_termless}
    matches: list[tuple[list[str], dict]] = []
    matched_keys: set[str] = set()
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
            if not is_wanted(listing, days, anywhere, now, include_termless):
                continue
            keys = listing_keys(listing)
            if matched_keys.isdisjoint(keys):
                matches.append((keys, listing))
            matched_keys.update(keys)
        fetched[src["name"]] = new_etag
        statuses[src["name"]] = "updated"

    shown = [l for keys, l in matches
             if show_all or not any(k in state["seen"] for k in keys)]
    shown.sort(key=lambda l: l["date_posted"], reverse=True)

    if mark:
        for key in matched_keys:
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
        termless = "" if term_stated(l) else f" {YELLOW}[term not stated]{RESET}"
        print(f"  {BOLD}{l['company']}{RESET} — {l['title']}{termless}")
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
    parser.add_argument("--include-termless", action="store_true",
                        help="Also show postings that name no term (may be Fall/Winter roles)")
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

    listings, statuses = watch(args.days, args.anywhere, args.show_all,
                               not args.no_mark, args.include_termless)
    display(listings, statuses, args.show_all)
    if args.output:
        export_json(listings, args.output)


if __name__ == "__main__":
    main()
