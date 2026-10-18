"""
Print the links in Gmail messages, read-only through the Gmail API, so a kept
digest can be verified without opening it in Gmail (which would mark it read).

    cd cli
    venv/bin/python .claude/skills/verify-postings/email_links.py            # every kept_id in .last_scan.json
    venv/bin/python .claude/skills/verify-postings/email_links.py <id> ...   # specific message ids
"""

import base64
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(CLI_DIR))

from scanner import CACHE_PATH, get_gmail_service  # noqa: E402

# Footer / chrome links, never a job listing.
_SKIP_TEXT = ("unsubscribe", "privacy", "help", "settings", "preferences",
              "terms", "view in browser", "manage", "learn more", "app store",
              "google play", "see all jobs", "add widget", "learn why",
              "your job alert")
_LINKEDIN_JOB_RE = re.compile(r"https://www\.linkedin\.com/(?:comm/)?jobs/view/(\d+)")


class _LinkExtractor(HTMLParser):
    """Collect (anchor text, href) for every <a> with an http(s) href."""

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


def message_links(service, msg_id: str) -> tuple[dict, list[tuple[str, str]]]:
    # format="full" does not change the UNREAD label.
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute(num_retries=3)
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    parser = _LinkExtractor()
    for part in _html_parts(msg.get("payload", {})):
        parser.feed(part)
    seen, links = set(), []
    for text, href in parser.links:
        # Drop LinkedIn's tracking params: the bare job URL opens the same posting.
        m = _LINKEDIN_JOB_RE.match(href)
        if m:
            href = f"https://www.linkedin.com/jobs/view/{m.group(1)}/"
        if len(text) < 4 or href in seen or any(s in text.lower() for s in _SKIP_TEXT):
            continue
        seen.add(href)
        links.append((text, href))
    return headers, links


def main() -> None:
    ids = sys.argv[1:]
    if not ids:
        cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}
        ids = cache.get("kept_ids", [])
    if not ids:
        print("No kept emails to inspect.")
        return
    service = get_gmail_service()
    for msg_id in ids:
        headers, links = message_links(service, msg_id)
        print(f"\n=== {msg_id}")
        print(f"Subject: {headers.get('Subject', '')}")
        print(f"From: {headers.get('From', '')}  ·  {headers.get('Date', '')}")
        for text, href in links:
            print(f"  - {text[:120]}\n    {href}")


if __name__ == "__main__":
    main()
