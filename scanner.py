"""
Internship Scanner - Local Gmail + Ollama
Scans your Gmail for internship/co-op opportunities and analyzes them locally.
No email data leaves your machine (except to/from Google's servers, where it already lives).
"""

import base64
import json
import os
import re
import sys
import argparse
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Gmail API scope (read-only)
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

TOKEN_PATH = Path(__file__).parent / "token.json"
CREDS_PATH = Path(__file__).parent / "credentials.json"

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
DEBUG = False

# ── Gmail Auth ──────────────────────────────────────────────────────────────

def get_gmail_service():
    """Authenticate and return a Gmail API service instance."""
    creds = None

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDS_PATH.exists():
                print("\n[ERROR] credentials.json not found.")
                print("Follow the setup instructions in README.md to create it.")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)

        TOKEN_PATH.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# ── Gmail Search ────────────────────────────────────────────────────────────

class _TextExtractor(HTMLParser):
    """Strip HTML tags and collect text content, skipping script/style blocks."""

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            self.parts.append(data)


def _html_to_text(html: str) -> str:
    p = _TextExtractor()
    try:
        p.feed(html)
    except Exception:
        return html
    return " ".join(p.parts)


def _extract_body(payload: dict) -> str:
    """Walk MIME parts and return the cleanest text body we can find."""
    plain_parts: list[str] = []
    html_parts: list[str] = []

    def walk(part: dict):
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if data:
            try:
                decoded = base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace")
            except Exception:
                decoded = ""
            if mime == "text/plain":
                plain_parts.append(decoded)
            elif mime == "text/html":
                html_parts.append(decoded)
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)

    if plain_parts:
        text = "\n".join(plain_parts)
    elif html_parts:
        text = _html_to_text("\n".join(html_parts))
    else:
        return ""

    text = re.sub(r"\s+", " ", text)
    # Collapse runs of any single non-alphanumeric char (decorative separators like ===, ---, ***)
    text = re.sub(r"([^\w\s])\1{3,}", r"\1\1\1", text)
    return text.strip()


BODY_MAX_CHARS = 800  # truncate to keep prompts manageable and skip footers


def search_emails(service, query: str, max_results: int = 30) -> list[dict]:
    """Search Gmail and return a list of simplified email dicts (with full body)."""
    try:
        result = service.users().messages().list(
            userId="me", q=query, maxResults=max_results
        ).execute()
    except Exception as e:
        print(f"  [!] Search failed: {e}")
        return []

    messages = result.get("messages", [])
    if not messages:
        return []

    emails = []
    for msg_meta in messages:
        try:
            msg = service.users().messages().get(
                userId="me", id=msg_meta["id"], format="full"
            ).execute()

            payload = msg.get("payload", {})
            headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
            snippet = msg.get("snippet", "")
            body = _extract_body(payload)
            if len(body) > BODY_MAX_CHARS:
                body = body[:BODY_MAX_CHARS] + " …[truncated]"

            emails.append({
                "id": msg_meta["id"],
                "subject": headers.get("Subject", "(no subject)"),
                "from": headers.get("From", "unknown"),
                "date": headers.get("Date", ""),
                "snippet": snippet,
                "body": body,
            })
        except Exception:
            continue

    return emails


def run_gmail_search(service, keyword: str = "", days: int = 30, unread_only: bool = True) -> list[dict]:
    """Run multiple targeted searches and deduplicate results."""
    if keyword.strip():
        queries = [
            f"{keyword} (internship OR co-op OR stage OR student OR stagiaire)",
        ]
    else:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y/%m/%d")
        queries = [
            f"(internship OR co-op OR stage OR stagiaire) after:{cutoff}",
            f"subject:(application OR applied OR interview OR offer) after:{cutoff}",
            f"from:(recruiter OR talent OR hiring OR careers OR hr) after:{cutoff}",
        ]

    if unread_only:
        queries = [f"is:unread {q}" for q in queries]

    all_emails = {}
    for q in queries:
        print(f"  Searching: {q}")
        for email in search_emails(service, q):
            all_emails[email["id"]] = email  # dedupe by ID

    return list(all_emails.values())


# ── Ollama Analysis ─────────────────────────────────────────────────────────

def check_ollama():
    """Verify Ollama is running and the model is available."""
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
        # Check if model (with or without tag) is available
        model_base = OLLAMA_MODEL.split(":")[0]
        available = any(model_base in m for m in models)
        if not available:
            print(f"\n[!] Model '{OLLAMA_MODEL}' not found. Available models:")
            for m in models:
                print(f"    - {m}")
            print(f"\n    Run: ollama pull {OLLAMA_MODEL}")
            sys.exit(1)
        return True
    except requests.ConnectionError:
        print(f"\n[ERROR] Cannot connect to Ollama at {OLLAMA_URL}")
        print("Make sure Ollama is running: ollama serve")
        sys.exit(1)


def _analyze_batch(emails: list[dict], offset: int) -> list[dict]:
    """Analyze a single batch of emails. offset is the 1-based index of emails[0]."""
    email_text = ""
    for i, e in enumerate(emails):
        email_text += f"\n--- Email {offset + i} ---\n"
        email_text += f"Subject: {e['subject']}\n"
        email_text += f"From: {e['from']}\n"
        email_text += f"Date: {e['date']}\n"
        body = e.get("body") or e.get("snippet", "")
        email_text += f"Body: {body}\n"

    prompt = f"""You are analyzing a student's inbox for internship and job-related emails.

CLASSIFICATION PRIORITY:
- The SUBJECT line is the PRIMARY signal — decide relevance based on the subject first.
- The body is for ADDITIONAL CONTEXT only (e.g. application term dates, company name,
  status of application). Do NOT use the body to second-guess the subject.
- Job-alert emails (LinkedIn, Glassdoor, Jobright) typically feature ONE headline job in
  the subject and list OTHER suggested jobs in the body — IGNORE the "More jobs you might
  like / Other recommendations / Related jobs" sections in the body. Only the headline
  job in the subject matters.

For each email, determine if it is one of:
- Internship / co-op / stage / stagiaire job postings (STUDENT positions only)
- Recruiter outreach or hiring manager contact about an internship
- Application confirmations or status updates for an internship application
- Interview invitations or follow-ups for an internship

STRICT RULES — these MUST be followed:
1. ONLY include actual internship/co-op/stage/stagiaire positions. The SUBJECT
   MUST explicitly mention one of: "intern", "internship", "stage", "stagiaire", "co-op",
   "coop", or "student". If none of these words appear in the subject, EXCLUDE the email.
2. EXCLUDE all full-time roles, including "junior", "senior", "mid-level", "lead",
   "associate", "engineer", "developer", "analyst", or "specialist" positions when they
   are NOT explicitly labeled as an internship.
3. Application confirmations, recruiter messages, and status updates from companies
   directly (e.g. PCL, WSP, Staples, Indeed Apply confirmations) about YOUR internship
   applications ARE relevant and should be included.
4. EXCLUDE any internship explicitly for "Fall 2026" / "Automne 2026" / "F2026"
   (the student is not looking for fall positions). If the subject or body mentions
   Fall 2026 as the term, EXCLUDE the email even if it's an internship.
5. IGNORE newsletters, promotional emails, Quora digests, and unrelated content.

OUTPUT FORMAT — return a JSON object with EXACTLY this shape:
{{
  "results": [
    {{
      "email_index": 1,
      "subject": "...",
      "from": "...",
      "date": "...",
      "company": "Flare",
      "category": "internship",
      "summary": "...",
      "action_items": ["Apply to the internship"],
      "priority": "high"
    }}
  ]
}}

Use EXACTLY these field names — do NOT rename "results" to "jobs"/"emails", and do NOT
rename "subject" to "job_title". Keep the field names verbatim.

Allowed values for "category": "internship", "recruiter", "confirmation", "reply", "status".
Allowed values for "priority": "high", "medium", "low".

If NO emails in this batch are relevant, return: {{"results": []}}

Emails:
{email_text}"""

    r = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.1, "num_predict": 4096},
        },
        timeout=180,
    )
    r.raise_for_status()
    raw = r.json().get("response", "").strip()
    if DEBUG:
        print(f"\n  ----- RAW LLM RESPONSE (emails {offset}-{offset + len(emails) - 1}) -----")
        print(f"  {raw}")
        print(f"  ----- END RAW -----\n")
    parsed = json.loads(raw)
    items = _extract_results_array(parsed)
    # Normalize field names that the model sometimes invents
    return [_normalize_result(item) for item in items]


def _extract_results_array(parsed) -> list:
    """Find the array of results regardless of which top-level key the model chose."""
    if isinstance(parsed, list):
        return parsed
    if not isinstance(parsed, dict):
        return []
    # Common keys the model might pick
    for key in ("results", "jobs", "emails", "items", "data", "matches"):
        val = parsed.get(key)
        if isinstance(val, list):
            return val
    # Fall back: any list value in the dict
    for val in parsed.values():
        if isinstance(val, list) and val and isinstance(val[0], dict):
            return val
    # Maybe the model returned a single object instead of an array
    if any(k in parsed for k in ("subject", "job_title", "email_index")):
        return [parsed]
    return []


VALID_CATEGORIES = {"internship", "recruiter", "confirmation", "reply", "status"}
VALID_PRIORITIES = {"high", "medium", "low"}


def _normalize_result(item: dict) -> dict:
    """Map common alternative field names to the canonical ones."""
    if not isinstance(item, dict):
        return {}
    aliases = {
        "subject": ("subject", "job_title", "title", "headline"),
        "from": ("from", "sender", "source"),
        "company": ("company", "employer", "organization"),
        "category": ("category", "type", "kind"),
        "summary": ("summary", "description", "details"),
        "action_items": ("action_items", "actions", "next_steps", "todos"),
        "priority": ("priority", "importance"),
        "email_index": ("email_index", "index", "id", "number"),
        "date": ("date", "received"),
    }
    out: dict = {}
    for canonical, alts in aliases.items():
        for a in alts:
            if a in item and item[a] is not None:
                out[canonical] = item[a]
                break
    # Validate category and priority; coerce unknown values to defaults
    cat = str(out.get("category", "")).lower()
    out["category"] = cat if cat in VALID_CATEGORIES else "internship"
    pri = str(out.get("priority", "")).lower()
    out["priority"] = pri if pri in VALID_PRIORITIES else "medium"
    out.setdefault("action_items", [])
    out.setdefault("summary", "")
    out.setdefault("company", None)
    return out


AGGREGATOR_SENDERS = (
    "linkedin.com",
    "glassdoor.com",
    "jobright.ai",
    "match.indeed.com",  # Indeed job alerts (not Indeed Apply confirmations)
)

INTERNSHIP_KEYWORDS = (
    "intern", "internship", "stage", "stagiaire", "co-op", "coop", "student",
)

EXCLUDE_TERM_PATTERNS = (
    "fall 2026", "fall2026", "f2026", "f-2026",
    "automne 2026", "automne2026", "a2026", "a-2026",
)


def _is_aggregator(sender: str) -> bool:
    s = (sender or "").lower()
    return any(domain in s for domain in AGGREGATOR_SENDERS)


def _subject_mentions_internship(subject: str) -> bool:
    s = (subject or "").lower()
    return any(kw in s for kw in INTERNSHIP_KEYWORDS)


def _mentions_excluded_term(*texts: str) -> bool:
    blob = " ".join(t.lower() for t in texts if t)
    return any(pat in blob for pat in EXCLUDE_TERM_PATTERNS)


def analyze_with_ollama(emails: list[dict]) -> list[dict]:
    """Send email metadata to Ollama for classification and summarization in batches."""
    if not emails:
        return []

    BATCH_SIZE = 5
    results: list[dict] = []
    total_batches = (len(emails) + BATCH_SIZE - 1) // BATCH_SIZE

    for b in range(total_batches):
        start = b * BATCH_SIZE
        end = min(start + BATCH_SIZE, len(emails))
        batch = emails[start:end]
        print(f"  Batch {b+1}/{total_batches} (emails {start+1}-{end})...")
        try:
            batch_results = _analyze_batch(batch, offset=start + 1)
            results.extend(batch_results)
        except json.JSONDecodeError as e:
            print(f"  [!] Batch {b+1} parse failed, skipping: {e}")
        except requests.RequestException as e:
            print(f"  [!] Batch {b+1} request failed, skipping: {e}")

    # Build subject lookup for fallback when email_index is missing
    by_subject: dict[str, dict] = {}
    for e in emails:
        key = (e.get("subject") or "").strip().lower()
        if key:
            by_subject[key] = e

    # Safety filters
    filtered = []
    seen_subjects: set[str] = set()
    dropped_aggregator = 0
    dropped_term = 0
    dropped_dup = 0
    dropped_unmatched = 0
    for r in results:
        # Look up the original email so we filter against the REAL sender/body,
        # not whatever the LLM rewrote them as. Try email_index first, fall back to subject.
        idx = r.get("email_index")
        original = None
        if isinstance(idx, int) and 1 <= idx <= len(emails):
            original = emails[idx - 1]
        else:
            key = (r.get("subject") or "").strip().lower()
            original = by_subject.get(key)

        if original is None:
            # The LLM made up an email that doesn't exist in the input — drop it
            dropped_unmatched += 1
            continue

        # Replace LLM-rewritten fields with the originals
        r["from"] = original.get("from", "")
        r["subject"] = original.get("subject", "")
        r["date"] = original.get("date", "")

        sender = r["from"]
        subject = r["subject"]

        if _is_aggregator(sender) and not _subject_mentions_internship(subject):
            dropped_aggregator += 1
            continue

        snippet = original.get("snippet", "")
        body = original.get("body", "")
        if _mentions_excluded_term(subject, r.get("summary", ""), snippet, body):
            dropped_term += 1
            continue

        # Dedupe hallucinated duplicates within the batch
        key = subject.strip().lower()
        if key and key in seen_subjects:
            dropped_dup += 1
            continue
        seen_subjects.add(key)

        filtered.append(r)

    if dropped_aggregator:
        print(f"  Filtered out {dropped_aggregator} aggregator email(s) without internship keywords")
    if dropped_term:
        print(f"  Filtered out {dropped_term} email(s) mentioning excluded term (e.g. Fall 2026)")
    if dropped_dup:
        print(f"  Filtered out {dropped_dup} duplicate email(s) (LLM hallucination)")
    if dropped_unmatched:
        print(f"  Filtered out {dropped_unmatched} unmatched result(s) (LLM hallucination)")

    return filtered


# ── Display ─────────────────────────────────────────────────────────────────

CATEGORY_COLORS = {
    "internship": "\033[32m",    # green
    "recruiter": "\033[35m",     # magenta
    "confirmation": "\033[34m",  # blue
    "reply": "\033[33m",         # yellow
    "status": "\033[37m",        # white
}
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

CATEGORY_LABELS = {
    "internship": "INTERNSHIP",
    "recruiter": "RECRUITER",
    "confirmation": "APP CONFIRM",
    "reply": "REPLY/FOLLOW-UP",
    "status": "STATUS UPDATE",
}


def display_results(results: list[dict]):
    """Pretty-print results to terminal."""
    if not results:
        print("\n  No internship-related emails found.\n")
        return

    # Sort by priority
    priority_order = {"high": 0, "medium": 1, "low": 2}
    results.sort(key=lambda x: priority_order.get(x.get("priority", "low"), 2))

    print(f"\n{'='*70}")
    print(f"  Found {len(results)} relevant email(s)")
    print(f"{'='*70}\n")

    for r in results:
        cat = r.get("category", "status")
        color = CATEGORY_COLORS.get(cat, "")
        label = CATEGORY_LABELS.get(cat, cat.upper())
        priority = r.get("priority", "")
        pri_marker = " !!" if priority == "high" else " !" if priority == "medium" else ""

        print(f"  {color}[{label}]{RESET}{BOLD}{pri_marker}{RESET}")
        print(f"  {BOLD}{r.get('subject', 'No subject')}{RESET}")
        print(f"  {DIM}From: {r.get('from', 'unknown')}{RESET}")
        print(f"  {DIM}Date: {r.get('date', '')}{RESET}")
        if r.get("company"):
            print(f"  {DIM}Company: {r['company']}{RESET}")
        print(f"  {r.get('summary', '')}")
        if r.get("action_items"):
            print(f"  {color}Action items:{RESET}")
            for item in r["action_items"]:
                print(f"    -> {item}")
        print()

    # Summary by category
    print(f"{'='*70}")
    print("  Summary:")
    from collections import Counter
    cats = Counter(r.get("category") for r in results)
    for cat, count in cats.most_common():
        color = CATEGORY_COLORS.get(cat, "")
        label = CATEGORY_LABELS.get(cat, cat)
        print(f"    {color}{label}: {count}{RESET}")
    print(f"{'='*70}\n")


# ── Export ──────────────────────────────────────────────────────────────────

def export_json(results: list[dict], path: str):
    """Export results to a JSON file."""
    with open(path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"  Exported {len(results)} results to {path}")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    global OLLAMA_MODEL, DEBUG
    parser = argparse.ArgumentParser(
        description="Scan Gmail for internship opportunities using a local LLM."
    )
    parser.add_argument(
        "-k", "--keyword",
        type=str, default="",
        help="Search keyword (e.g. 'EXFO' or 'software intern Montreal')"
    )
    parser.add_argument(
        "-d", "--days",
        type=int, default=30,
        help="How many days back to scan (default: 30)"
    )
    parser.add_argument(
        "-m", "--model",
        type=str, default=None,
        help=f"Ollama model to use (default: {OLLAMA_MODEL})"
    )
    parser.add_argument(
        "-o", "--output",
        type=str, default=None,
        help="Export results to JSON file"
    )
    parser.add_argument(
        "-n", "--max-emails",
        type=int, default=30,
        help="Max emails to fetch per query (default: 30)"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Scan all emails (default: unread only)"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print raw LLM response for each batch"
    )
    args = parser.parse_args()

    if args.model:
        OLLAMA_MODEL = args.model
    DEBUG = args.debug

    print(f"\n{BOLD}Internship Scanner{RESET}")
    print(f"{DIM}Local analysis with {OLLAMA_MODEL} via Ollama{RESET}\n")

    # Check Ollama
    print("[1/3] Checking Ollama...")
    check_ollama()
    print(f"  Using model: {OLLAMA_MODEL}")

    # Gmail auth
    print("[2/3] Connecting to Gmail...")
    service = get_gmail_service()

    # Search
    scope = "all emails" if args.all else "unread only"
    if args.keyword:
        print(f"  Keyword search: '{args.keyword}' ({scope})")
    else:
        print(f"  Scanning last {args.days} days ({scope})")

    emails = run_gmail_search(service, keyword=args.keyword, days=args.days, unread_only=not args.all)
    print(f"  Found {len(emails)} emails to analyze")

    if not emails:
        print("\n  No emails matched the search queries.\n")
        return

    # Analyze locally
    print(f"[3/3] Analyzing with {OLLAMA_MODEL} (this may take a moment)...")
    results = analyze_with_ollama(emails)

    # Display
    display_results(results)

    # Export
    if args.output:
        export_json(results, args.output)


if __name__ == "__main__":
    main()
