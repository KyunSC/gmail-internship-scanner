"""
Internship Scanner - Local Gmail + Ollama
Scans your Gmail for internship/co-op opportunities and analyzes them locally.
No email data leaves your machine (except to/from Google's servers, where it already lives).
"""

import json
import os
import sys
import argparse
from datetime import datetime, timedelta
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

def search_emails(service, query: str, max_results: int = 30) -> list[dict]:
    """Search Gmail and return a list of simplified email dicts."""
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
                userId="me", id=msg_meta["id"], format="metadata",
                metadataHeaders=["Subject", "From", "Date"]
            ).execute()

            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            snippet = msg.get("snippet", "")

            emails.append({
                "id": msg_meta["id"],
                "subject": headers.get("Subject", "(no subject)"),
                "from": headers.get("From", "unknown"),
                "date": headers.get("Date", ""),
                "snippet": snippet,
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
        email_text += f"Snippet: {e['snippet']}\n"

    prompt = f"""You are analyzing a student's inbox for internship and job-related emails.

For each email, determine if it is one of:
- Internship / co-op / stage / stagiaire job postings (STUDENT positions only)
- Recruiter outreach or hiring manager contact about an internship
- Application confirmations or status updates for an internship application
- Interview invitations or follow-ups for an internship

STRICT RULES — these MUST be followed:
1. ONLY include actual internship/co-op/stage/stagiaire positions. The subject or snippet
   MUST explicitly mention one of: "intern", "internship", "stage", "stagiaire", "co-op",
   "coop", or "student". If none of these words appear, EXCLUDE the email.
2. EXCLUDE all full-time roles, including "junior", "senior", "mid-level", "lead",
   "associate", "engineer", "developer", "analyst", or "specialist" positions when they
   are NOT explicitly labeled as an internship.
3. For job-aggregator emails (LinkedIn, Glassdoor, Jobright, Indeed Job Alerts):
   - The aggregator email is RELEVANT ONLY if the lead/headline job title in the subject
     is explicitly an internship/co-op/stage.
   - Digest emails like "Java Developer at X and 7 more jobs" are EXCLUDED unless that
     lead job itself is explicitly an internship.
   - "Junior Data Engineer", "Senior Full Stack", "Application Engineer" without
     "intern/stage/co-op" qualifier → EXCLUDE.
4. Application confirmations, recruiter messages, and status updates from companies
   directly (e.g. PCL, WSP, Staples, Indeed Apply confirmations) about YOUR internship
   applications ARE relevant and should be included.
5. EXCLUDE any internship explicitly for "Fall 2026" / "Automne 2026" / "F2026"
   (the student is not looking for fall positions). If the subject or snippet
   mentions Fall 2026 as the term, EXCLUDE the email even if it's an internship.
6. IGNORE newsletters, promotional emails, Quora digests, and unrelated content.

Return a JSON object with a single key "results" whose value is an array.
For each RELEVANT email, include an object in the array with these fields:
- "email_index": the email number shown above (integer)
- "subject": the email subject
- "from": sender
- "date": date
- "company": company name if identifiable, or null
- "category": one of "internship", "recruiter", "confirmation", "reply", "status"
- "summary": 1-2 sentence summary
- "action_items": array of action items (strings), or empty array
- "priority": "high", "medium", or "low"

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
    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        return parsed.get("results", []) or []
    if isinstance(parsed, list):
        return parsed
    return []


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

    BATCH_SIZE = 10
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

    # Safety filters
    filtered = []
    dropped_aggregator = 0
    dropped_term = 0
    for r in results:
        if _is_aggregator(r.get("from", "")) and not _subject_mentions_internship(r.get("subject", "")):
            dropped_aggregator += 1
            continue
        # Pull the original snippet so we can also filter on body text
        idx = r.get("email_index")
        snippet = ""
        if isinstance(idx, int) and 1 <= idx <= len(emails):
            snippet = emails[idx - 1].get("snippet", "")
        if _mentions_excluded_term(r.get("subject", ""), r.get("summary", ""), snippet):
            dropped_term += 1
            continue
        filtered.append(r)

    if dropped_aggregator:
        print(f"  Filtered out {dropped_aggregator} aggregator email(s) without internship keywords")
    if dropped_term:
        print(f"  Filtered out {dropped_term} email(s) mentioning excluded term (e.g. Fall 2026)")

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
    global OLLAMA_MODEL
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
    args = parser.parse_args()

    if args.model:
        OLLAMA_MODEL = args.model

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
