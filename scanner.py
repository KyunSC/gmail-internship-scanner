"""
Internship Scanner - Local Gmail + Ollama
Scans your Gmail for internship/co-op opportunities and analyzes them locally.
No email data leaves your machine (except to/from Google's servers, where it already lives).
"""

import base64
import html
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
SCOPE_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_MODIFY = "https://www.googleapis.com/auth/gmail.modify"

TOKEN_PATH = Path(__file__).parent / "token.json"
CREDS_PATH = Path(__file__).parent / "credentials.json"
# Persisted snapshot of the most recent scan, used by --from-cache so cleanup can
# skip the multi-minute LLM analysis when nothing material has changed.
CACHE_PATH = Path(__file__).parent / ".last_scan.json"

# Job-aggregator senders that --clean-inbox may mark as read. LinkedIn entries are
# restricted to job-alert addresses (so newsletters / personal LinkedIn messages
# are not touched).
CLEAN_INBOX_SENDERS = (
    "jobalerts-noreply@linkedin.com",
    "jobs-listings@linkedin.com",
    "jobs-noreply@linkedin.com",
    "noreply@glassdoor.com",
    "noreply@jobright.ai",
    "alert@indeed.com",
    "noreply@indeed.com",
    # Indeed's job-match subdomain — uses donotreply@match.indeed.com and similar.
    # Substring matches `from:` so any address under match.indeed.com is caught.
    "match.indeed.com",
    # ZipRecruiter uses many automated personas (alerts@, noreply@, phil@, etc.).
    # Domain match catches all of them, including future ones. Real recruiter
    # outreach via seekerteam@ziprecruiter.com is also caught — the subject
    # safety net keeps any internship-mentioning ones unread regardless.
    "ziprecruiter.com",
)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5:9b")
DEBUG = False

# ── Gmail Auth ──────────────────────────────────────────────────────────────

def get_gmail_service(write_access: bool = False):
    """Authenticate and return a Gmail API service instance.

    write_access=True requests the gmail.modify scope so we can mark emails as
    read. If the cached token only has readonly, we force a re-auth.
    """
    needed = [SCOPE_MODIFY] if write_access else [SCOPE_READONLY]
    creds = None

    if TOKEN_PATH.exists():
        # Read the actual granted scopes from the file; from_authorized_user_file
        # overrides creds.scopes with whatever we pass, so we can't rely on it.
        granted = set(json.loads(TOKEN_PATH.read_text()).get("scopes", []))
        if write_access and SCOPE_MODIFY not in granted:
            # Token was issued under readonly only — force a fresh auth flow.
            creds = None
        else:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), needed)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
        if not creds or not creds.valid:
            if not CREDS_PATH.exists():
                print("\n[ERROR] credentials.json not found.")
                print("Follow the setup instructions in README.md to create it.")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), needed)
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


def _normalize_body(text: str) -> str:
    text = html.unescape(text)
    text = re.sub("[​-‏‪-‮⁠﻿]", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text)
    # Collapse runs of any single non-alphanumeric char (decorative separators like ===, ---, ***)
    text = re.sub(r"([^\w\s])\1{3,}", r"\1\1\1", text)
    return text.strip()


def _extract_body(payload: dict) -> str:
    """Walk MIME parts and return the cleanest text body we can find.

    Some senders (notably LinkedIn) ship a text/plain part that, after URL
    stripping, is just footer boilerplate while the actual job listings live
    only in text/html. So we extract both and return whichever yields more
    substantive text after cleanup."""
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

    plain_text = _normalize_body("\n".join(plain_parts)) if plain_parts else ""
    html_text = _normalize_body(_html_to_text("\n".join(html_parts))) if html_parts else ""

    return html_text if len(html_text) > len(plain_text) else plain_text


BODY_MAX_CHARS = 5000  # truncate to keep prompts manageable and skip footers


def search_emails(service, query: str, max_results: int = 100) -> list[dict]:
    """Search Gmail and return a list of simplified email dicts (with full body)."""
    try:
        result = service.users().messages().list(
            userId="me", q=query, maxResults=max_results
        ).execute(num_retries=3)
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
            ).execute(num_retries=3)

            payload = msg.get("payload", {})
            headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
            subject = headers.get("Subject", "(no subject)")
            sender = headers.get("From", "unknown")
            body = _extract_body(payload)
            if len(body) > BODY_MAX_CHARS:
                body = body[:BODY_MAX_CHARS] + " …[truncated]"

            emails.append({
                "id": msg_meta["id"],
                "subject": subject,
                "from": sender,
                "date": headers.get("Date", ""),
                "body": body,
            })
        except Exception:
            continue

    return emails


def run_gmail_search(service, keyword: str = "", days: int = 30, unread_only: bool = True, max_results: int = 100) -> list[dict]:
    """Run multiple targeted searches and deduplicate results."""
    intern_terms = "intern OR internship OR coop OR co-op OR stage OR stagiaire OR student"
    if keyword.strip():
        queries = [
            f"{keyword} ({intern_terms})",
        ]
    else:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y/%m/%d")
        queries = [
            f"({intern_terms}) after:{cutoff}",
            f"subject:(application OR applied OR interview OR offer) after:{cutoff}",
            f"from:(recruiter OR talent OR hiring OR careers OR hr) after:{cutoff}",
        ]

    if unread_only:
        queries = [f"is:unread {q}" for q in queries]

    all_emails = {}
    for q in queries:
        print(f"  Searching: {q}")
        for email in search_emails(service, q, max_results=max_results):
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


def _estimate_ctx_size(prompt: str, num_predict: int) -> int:
    """Pick the smallest power-of-2 num_ctx that fits this request, with a floor
    of 2048. Token count is estimated at ~3 chars/token (conservative for BPE).
    Bucketing to powers of 2 keeps Ollama from reloading the model between
    similar-sized requests — a reload only happens when the bucket changes.
    KV cache scales linearly with num_ctx, so a 4096-bucket request uses half
    the VRAM of an 8192-bucket request."""
    needed = len(prompt) // 3 + num_predict + 256  # 256 = safety margin
    bucket = 2048
    while bucket < needed:
        bucket *= 2
    return bucket


_PROMPT_QUOTE_FIXES = str.maketrans({
    "“": '"', "”": '"', "‘": "'", "’": "'",
})


def _sanitize_for_prompt(s: str) -> str:
    # Qwen3.5 emits ASCII " in place of curly " inside JSON strings, which
    # closes the string early and breaks the response. Normalize the lookalikes
    # before they reach the model.
    return s.translate(_PROMPT_QUOTE_FIXES) if s else s


PROMPT_DEFAULT = """You are analyzing a student's inbox for internship and job-related emails.

CLASSIFICATION PRIORITY:
- Combine signals from the SUBJECT, BODY, and SENDER. No single field is decisive on
  its own. Strong signals: subject keywords (intern/co-op/stage/student), body content
  describing a student role, sender being a recruiter or career address.
- For job-alert digest emails (LinkedIn, Glassdoor, Jobright), scan the ENTIRE body for
  any internship/co-op/stage/student listing — not just the headline. Surface the email
  if ANY listing in it is an internship, even if it appears in a recommendations section.

For each email, determine if it is one of:
- Internship / co-op / stage / stagiaire job postings (STUDENT positions only)
- Recruiter outreach or hiring manager contact about an internship
- Application confirmations or status updates for an internship application
- Interview invitations or follow-ups for an internship

STRICT RULES — these MUST be followed:
1. ONLY include actual internship/co-op/stage/stagiaire/student positions. At least ONE
   of the following signals must apply:
   (a) Subject mentions "intern", "internship", "stage", "stagiaire", "co-op", "coop",
       or "student"
   (b) Body clearly describes a student/intern/co-op position
   (c) The email is recruiter outreach or application status for an internship from
       a company or career address
   If NONE of these signals apply, EXCLUDE.
2. EXCLUDE all full-time roles, including "junior", "senior", "mid-level", "lead",
   "associate", "engineer", "developer", "analyst", or "specialist" positions when they
   are NOT explicitly labeled as an internship.
3. Application confirmations, recruiter messages, and status updates from companies
   directly (e.g. PCL, WSP, Staples, Indeed Apply confirmations) about YOUR internship
   applications ARE relevant and should be included.
4. IGNORE newsletters, promotional emails, Quora digests, and unrelated content.

OUTPUT FORMAT — return a JSON object with EXACTLY this shape:
{{
  "results": [
    {{
      "email_index": <integer index from the email block>,
      "subject": "<copy the subject verbatim>",
      "from": "<copy the from verbatim>",
      "date": "<copy the date verbatim>",
      "company": "<extract the hiring company name from THIS email's body — do not invent>",
      "category": "internship",
      "summary": "<short summary of THIS email only>",
      "action_items": ["..."],
      "priority": "high"
    }}
  ]
}}

CRITICAL: Each result must reflect the corresponding email's content. Do NOT mix
information across emails in the batch. Do NOT use placeholder/example values
verbatim. If you cannot determine the company from the email body, set company to null.

Use EXACTLY these field names — do NOT rename "results" to "jobs"/"emails", and do NOT
rename "subject" to "job_title". Keep the field names verbatim.

Allowed values for "category": "internship", "recruiter", "confirmation", "reply", "status".
Allowed values for "priority": "high", "medium", "low".

If NO emails in this batch are relevant, return: {{"results": []}}

Emails:
{email_text}"""


PROMPT_TIGHT = """Classify emails for a student's internship search. For each email decide whether \
it relates to an internship / co-op / stage / stagiaire / student role — the listing itself, \
recruiter outreach, application confirmations, status updates, or interview messages.

INCLUDE only if at least one applies:
- Subject contains intern, internship, stage, stagiaire, co-op, coop, or student
- Body clearly describes a student / intern / co-op position
- Recruiter or career-address message about an internship application
For digest emails (LinkedIn, Glassdoor, Jobright), scan the FULL body — include if ANY \
listing is an internship, even if buried in recommendations.

EXCLUDE: full-time roles ("junior", "senior", "mid-level", "lead", "associate", "engineer", \
"developer", "analyst", "specialist") when not explicitly labeled as an internship; \
newsletters; promos; Quora digests.

Return ONLY this JSON, fields verbatim:
{{"results": [{{"email_index": <int from the block>, "subject": "<verbatim>", \
"from": "<verbatim>", "date": "<verbatim>", \
"company": "<from THIS email's body, or null>", \
"category": "internship|recruiter|confirmation|reply|status", \
"summary": "<this email only>", "action_items": ["..."], \
"priority": "high|medium|low"}}]}}

Each result must reflect its own email — do not mix content across the batch. \
Do not rename "results" or "subject". If nothing matches, return {{"results": []}}.

Emails:
{email_text}"""


def _analyze_batch(
    emails: list[dict],
    offset: int,
    temperature: float = 0.0,
    prompt_template: str = PROMPT_DEFAULT,
) -> list[dict]:
    """Analyze a single batch of emails. offset is the 1-based index of emails[0].

    temperature=0 is the deterministic baseline. Non-zero gives sampling diversity,
    used by the 2nd LLM pass to surface borderline emails the deterministic pass
    missed (e.g. buried internships in aggregator digests).

    prompt_template must contain the literal "{email_text}" placeholder. Swap it
    to A/B different prompt phrasings against the same email set.
    """
    email_text = ""
    for i, e in enumerate(emails):
        email_text += f"\n--- Email {offset + i} ---\n"
        email_text += f"Subject: {_sanitize_for_prompt(e['subject'])}\n"
        email_text += f"From: {_sanitize_for_prompt(e['from'])}\n"
        email_text += f"Date: {e['date']}\n"
        email_text += f"Body: {_sanitize_for_prompt(e.get('body', ''))}\n"

    prompt = prompt_template.format(email_text=email_text)

    r = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            # Qwen3 family routes content into a <think> block by default, leaving
            # the actual response empty. Disable thinking so the JSON arrives.
            # Non-Qwen3 models that don't support this field ignore it harmlessly.
            "think": False,
            "options": {
                "temperature": temperature,
                "num_predict": 4096,
                "num_ctx": _estimate_ctx_size(prompt, 4096),
            },
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

# Software / tech terms used to require a relevant field on aggregator-digest
# listings. A buried internship chunk must also mention one of these to be
# surfaced — drops Finance/HR/Business student opportunities that otherwise
# slip through on a bare "student" hit. Phrases are intentionally specific
# (e.g. "software engineering", not bare "engineering", which would match
# "Bachelor of Commerce or Engineering degree").
SOFTWARE_KEYWORDS = (
    "software", "developer", "développeur", "developpeur",
    "programmer", "programming", "coding",
    "computer science", "computer engineering", "computer engineer",
    "informatique", "génie logiciel", "genie logiciel",
    "frontend", "front-end",
    "backend", "back-end",
    "full-stack", "fullstack", "full stack",
    "devops", "site reliability",
    "data science", "data scientist", "data analyst", "data engineer",
    "machine learning", "deep learning",
    "cybersecurity", "cyber security",
    "cloud engineer", "cloud developer",
    "embedded systems", "embedded software", "embedded engineer", "firmware",
    "robotics",
    "ios developer", "ios engineer", "android developer", "android engineer",
    "mobile developer", "mobile engineer",
    "python", "javascript", "typescript",
    "automation tester", "automation engineer", "qa engineer", "qa analyst",
    "qa automation", "test engineer", "sdet",
    "tech intern", "technology intern", "it intern",
    "software engineer", "software engineering",
    "systems engineer", "systems engineering",
    "hardware engineer", "fpga",
    "sde intern", "swe intern",
    "web developer", "web engineer",
    # Title phrases — anchored so they only match the listing's role,
    # not a degree requirement like "Bachelor of ... Engineering".
    "engineering intern", "engineering internship",
    "engineering co-op", "engineering coop", "engineering student",
    "stage en génie", "stage en informatique", "stagiaire en informatique",
)

# "stage" alone matches English "Late Stage", "Early Stage", etc.
# Only treat it as an internship signal in the body when French context surrounds it.
_STAGE_FRENCH_RE = re.compile(
    r"(?:(?:un|de|du|en|le|la|au|bénévole|offre|recherche|cherche|propose)\s+stage\b"
    r"|\bstage\s+(?:en|de|du|chez|pour|rémunéré|développeur|developer|informatique|logiciel|étudiant|pratique))",
    re.IGNORECASE,
)

# Terms for internships the user does NOT want surfaced (currently Fall 2026 /
# September 2026 starts). Per-listing filter — we split aggregator digests into
# individual job listings and drop the email only if EVERY intern listing in it
# is Fall/Sept. If at least one Summer/other-term intern listing survives in the
# same email, the email is kept.
EXCLUDE_TERM_REGEX = re.compile(
    r"\b(fall|automne)\b.{0,20}\b2026\b"
    r"|\b2026\b.{0,20}\b(fall|automne)\b"
    r"|\bsept(\.|ember|embre)?\s*\.?\s*2026\b"
    r"|\b[fa][-\s]?2026\b",
    re.IGNORECASE,
)


_GLASSDOOR_LISTINGS_HEAD_RE = re.compile(
    r"Your job listings for\s+\w+\s+\d+,\s+\d{4}",
    re.IGNORECASE,
)

_GLASSDOOR_LISTINGS_FOOT_RE = re.compile(
    r"See more jobs|Want more listings"
)


def _clean_glassdoor_body(body: str) -> str:
    """Strip Glassdoor digest chrome before chunk analysis. Without this, the
    user's saved alert name (e.g. "Backend Developer Internship") echoes in
    the header banner, the listings subtitle, and the footer "Sent Daily Edit"
    block — injecting "intern" + "developer" keywords into a digest whose
    actual listings are all full-time. The footer also has "Create alert"
    CTAs like "web developer intern Create" that hit the same way.

    Removes everything up to and including the first ★ after "Your job
    listings for [date]" (which drops the alert-name echo and the first
    listing's company chip, since Glassdoor puts the company+rating BEFORE
    its ★ separator), and trims from "See more jobs" / "Want more listings"
    onward."""
    m = _GLASSDOOR_LISTINGS_HEAD_RE.search(body)
    if m:
        body = body[m.end():]
        star = body.find("★")
        if star >= 0:
            body = body[star + 1:]
    m2 = _GLASSDOOR_LISTINGS_FOOT_RE.search(body)
    if m2:
        body = body[:m2.start()]
    return body


def _split_aggregator_listings(sender: str, body: str) -> list[str]:
    """Split a digest body into per-listing chunks. Glassdoor uses ★ as the
    listing separator (after each company's rating); Jobright closes each
    recommendation with "APPLY NOW"; Indeed match digests close each listing
    with "Easily apply". Senders without a known digest format return the whole
    body as a single chunk. Glassdoor bodies are pre-cleaned to drop alert
    chrome that would otherwise inject false internship signals."""
    s = (sender or "").lower()
    if "glassdoor.com" in s and "★" in body:
        cleaned = _clean_glassdoor_body(body)
        return [c.strip() for c in cleaned.split("★") if c.strip()]
    if "jobright.ai" in s and "APPLY NOW" in body:
        return [c.strip() for c in body.split("APPLY NOW") if c.strip()]
    if "match.indeed.com" in s and "Easily apply" in body:
        return [c.strip() for c in body.split("Easily apply") if c.strip()]
    return [body]


def _all_intern_listings_excluded(sender: str, body: str) -> bool:
    """True if every chunk containing an internship keyword also matches the
    Fall/Sept exclusion. False if any non-Fall intern listing exists, or if no
    chunk has an intern keyword (in which case the filter shouldn't fire)."""
    intern_chunks = [
        c for c in _split_aggregator_listings(sender, body)
        if _body_mentions_internship(c) or _subject_mentions_internship(c)
    ]
    if not intern_chunks:
        return False
    return all(EXCLUDE_TERM_REGEX.search(c) for c in intern_chunks)


RECRUITER_SENDER_HINTS = (
    "talent@", "careers@", "career@", "hr@", "recruiter@", "recruiting@",
    "hiring@", "jobs@", "noreply@", "no-reply@",
    "lever.co", "greenhouse.io", "workday.com", "myworkday.com",
    "icims.com", "smartrecruiters", "ashbyhq", "bamboohr",
    "successfactors", "taleo.net",
)


def _is_aggregator(sender: str) -> bool:
    s = (sender or "").lower()
    return any(domain in s for domain in AGGREGATOR_SENDERS)


def _is_recruiter_sender(sender: str) -> bool:
    """Recruiter/career-address senders worth keeping even without a subject keyword
    (e.g. application status updates, interview invites)."""
    s = (sender or "").lower()
    return any(h in s for h in RECRUITER_SENDER_HINTS)


def _subject_mentions_internship(subject: str) -> bool:
    s = (subject or "").lower()
    return any(kw in s for kw in INTERNSHIP_KEYWORDS)


def _body_mentions_internship(body: str) -> bool:
    b = (body or "").lower()
    for kw in INTERNSHIP_KEYWORDS:
        if kw == "stage":
            if _STAGE_FRENCH_RE.search(body):
                return True
        elif kw in b:
            return True
    return False


def _body_mentions_software(body: str) -> bool:
    b = (body or "").lower()
    return any(kw in b for kw in SOFTWARE_KEYWORDS)


def _has_software_internship_listing(sender: str, body: str) -> bool:
    """True iff at least one body chunk contains BOTH an internship keyword
    AND a software/tech keyword. For aggregators with a known digest splitter
    this is per-listing; without one it falls back to a whole-body co-occurrence
    check (still strictly more strict than the previous "any intern keyword"
    test)."""
    for c in _split_aggregator_listings(sender, body):
        if _body_mentions_internship(c) and _body_mentions_software(c):
            return True
    return False


def _has_internship_signal(subject: str, body: str, sender: str) -> bool:
    """Combined signal check. An email must have BOTH an internship signal
    AND a software/tech signal somewhere to pass. Filters out non-tech
    internships (accounting, marketing, finance) that the subject keyword
    alone would otherwise let through. For aggregator digests, the per-chunk
    AND check (intern + software in the same listing) is the stricter form."""
    if _is_aggregator(sender):
        # Subject-only intern keyword is enough only if the subject itself
        # also names a software/tech role; otherwise rely on the per-chunk
        # body listing check.
        if _subject_mentions_internship(subject) and _body_mentions_software(subject):
            return True
        return _has_software_internship_listing(sender, body)
    has_software = _body_mentions_software(subject) or _body_mentions_software(body)
    if _subject_mentions_internship(subject) or _body_mentions_internship(body):
        return has_software
    return _is_recruiter_sender(sender) and has_software



BATCH_SIZE = 1
# Each batch is sent to the LLM with these per-pass temperatures and the
# per-batch results are unioned (first-pass wins on conflicts). Pass 1 at
# temperature 0 is the deterministic baseline; later passes at a small
# temperature add sampling diversity to surface borderline emails (buried
# internships in aggregator digests, in particular) the baseline missed.
# Length controls how many passes run per batch.
LLM_PASS_TEMPERATURES = (0.0, 0.5)


def analyze_with_ollama(
    emails: list[dict],
    prompt_template: str = PROMPT_DEFAULT,
) -> list[dict]:
    """Send email metadata to Ollama for classification and summarization in batches."""
    if not emails:
        return []

    # Pre-filter: only send emails that have an internship signal in subject, body,
    # or sender before hitting the LLM.
    filtered_in = [
        e for e in emails
        if _has_internship_signal(e.get("subject", ""), e.get("body", ""), e.get("from", ""))
    ]
    dropped_pre = len(emails) - len(filtered_in)
    if dropped_pre:
        print(f"  Pre-filtered {dropped_pre} email(s) with no internship signal")
    # Stable batching: sort by Gmail message ID so batch composition doesn't shift
    # when a new email arrives between runs (Gmail returns most-recent-first, which
    # shifts every existing email down by one when something new lands).
    emails = sorted(filtered_in, key=lambda e: e.get("id", ""))

    if not emails:
        return []

    results: list[dict] = []
    total_batches = (len(emails) + BATCH_SIZE - 1) // BATCH_SIZE
    n_passes = len(LLM_PASS_TEMPERATURES)

    for b in range(total_batches):
        start = b * BATCH_SIZE
        end = min(start + BATCH_SIZE, len(emails))
        batch = emails[start:end]
        # Union LLM_PASS_TEMPERATURES passes of the same batch. Dedupe by
        # email_index inside the batch so we don't double-count emails both passes
        # flagged; results that only one pass produced (the recall win) are kept.
        batch_combined: list[dict] = []
        seen_in_batch: set[int] = set()
        for p, temp in enumerate(LLM_PASS_TEMPERATURES):
            pass_label = f" pass {p+1}/{n_passes} (t={temp})" if n_passes > 1 else ""
            print(f"  Batch {b+1}/{total_batches} (emails {start+1}-{end}){pass_label}...")
            try:
                pass_results = _analyze_batch(
                    batch, offset=start + 1, temperature=temp,
                    prompt_template=prompt_template,
                )
            except json.JSONDecodeError as e:
                print(f"  [!] Batch {b+1}{pass_label} parse failed, skipping: {e}")
                continue
            except requests.RequestException as e:
                print(f"  [!] Batch {b+1}{pass_label} request failed, skipping: {e}")
                continue
            for r in pass_results:
                idx = r.get("email_index")
                if isinstance(idx, int):
                    if idx in seen_in_batch:
                        continue
                    seen_in_batch.add(idx)
                batch_combined.append(r)
        results.extend(batch_combined)

    # Build subject lookup for fallback when email_index is missing
    by_subject: dict[str, dict] = {}
    for e in emails:
        key = (e.get("subject") or "").strip().lower()
        if key:
            by_subject[key] = e

    index_by_id: dict[str, int] = {e["id"]: i + 1 for i, e in enumerate(emails) if e.get("id")}

    # Safety filters
    filtered = []
    seen_indices: set[int] = set()
    dropped_aggregator = 0
    dropped_dup = 0
    dropped_unmatched = 0
    dropped_term = 0
    for r in results:
        # Look up the original email so we filter against the REAL sender/body,
        # not whatever the LLM rewrote them as. Try email_index first, fall back to subject.
        idx = r.get("email_index")
        original = None
        match_idx: int | None = None
        if isinstance(idx, int) and 1 <= idx <= len(emails):
            original = emails[idx - 1]
            match_idx = idx
        else:
            key = (r.get("subject") or "").strip().lower()
            original = by_subject.get(key)
            if original is not None:
                match_idx = index_by_id.get(original.get("id", ""))

        if original is None:
            # The LLM made up an email that doesn't exist in the input — drop it
            dropped_unmatched += 1
            continue

        # Replace LLM-rewritten fields with the originals
        r["from"] = original.get("from", "")
        r["subject"] = original.get("subject", "")
        r["date"] = original.get("date", "")
        r["id"] = original.get("id", "")

        # Validate company against the email — null it out if the LLM hallucinated.
        company = r.get("company")
        if isinstance(company, str) and company.strip():
            haystack = " ".join([
                original.get("body", ""),
                original.get("subject", ""),
                original.get("from", ""),
            ]).lower()
            if company.strip().lower() not in haystack:
                r["company"] = None

        sender = r["from"]
        subject = r["subject"]
        body = original.get("body", "")

        # Drop only if there's no internship signal anywhere — subject, body, or sender.
        # Aggregators (Glassdoor, LinkedIn, Jobright) get the strictest application of
        # this since they spam digests; other senders pass through to the next filters.
        if _is_aggregator(sender) and not _has_internship_signal(subject, body, sender):
            dropped_aggregator += 1
            continue

        # Per-listing Fall 2026 / September 2026 exclusion (body only — subject
        # is intentionally not checked). For digest aggregators the body is split
        # into individual job listings; we drop the email only if EVERY intern
        # listing in it is Fall/Sept. A mixed digest with one Summer intern still
        # passes.
        body = original.get("body", "")
        if _all_intern_listings_excluded(sender, body):
            dropped_term += 1
            continue

        # Dedupe per-email: only drop if the LLM returned the same email twice.
        # Distinct emails that happen to share a subject are kept.
        if match_idx is not None:
            if match_idx in seen_indices:
                dropped_dup += 1
                continue
            seen_indices.add(match_idx)

        filtered.append(r)

    if dropped_aggregator:
        print(f"  Filtered out {dropped_aggregator} aggregator email(s) without internship keywords")
    if dropped_term:
        print(f"  Filtered out {dropped_term} email(s) whose body mentions Fall 2026 / September 2026")
    if dropped_dup:
        print(f"  Filtered out {dropped_dup} duplicate email(s) (LLM hallucination)")
    if dropped_unmatched:
        print(f"  Filtered out {dropped_unmatched} unmatched result(s) (LLM hallucination)")

    return filtered


def rule_based_analyze(emails: list[dict]) -> list[dict]:
    """Fast path: full-body keyword classification without the LLM. Returns
    result dicts shaped to match analyze_with_ollama's output so display, cache,
    and cleanup downstream all work unchanged. Comparable accuracy to the LLM
    when every body containing an internship keyword is genuinely an internship
    listing; loses the LLM's ability to spot footer-boilerplate false positives
    and gives no per-email summaries.
    """
    out: list[dict] = []
    dropped_no_signal = 0
    dropped_term = 0
    for e in emails:
        subject = e.get("subject", "")
        body = e.get("body", "")
        sender = e.get("from", "")
        if not _has_internship_signal(subject, body, sender):
            dropped_no_signal += 1
            continue
        if _all_intern_listings_excluded(sender, body):
            dropped_term += 1
            continue
        out.append({
            "id": e.get("id", ""),
            "subject": subject,
            "from": sender,
            "date": e.get("date", ""),
            "company": None,
            "category": "internship",
            "summary": "(rule-based match — full-body keyword filter)",
            "action_items": [],
            "priority": "medium",
        })
    if dropped_no_signal:
        print(f"  Dropped {dropped_no_signal} email(s) with no internship signal")
    if dropped_term:
        print(f"  Dropped {dropped_term} email(s) whose body mentions Fall 2026 / September 2026")
    return out


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


# ── Scan cache ──────────────────────────────────────────────────────────────

def save_scan_cache(emails: list[dict], results: list[dict]):
    """Persist enough of the most recent scan that --from-cache can re-run the
    cleanup without redoing the LLM analysis. We only store header-level fields
    (no bodies) to keep the file small and avoid leaking content on disk."""
    emails_min = [
        {"id": e.get("id", ""), "subject": e.get("subject", ""),
         "from": e.get("from", ""), "date": e.get("date", "")}
        for e in emails if e.get("id")
    ]
    kept_ids = [r.get("id") for r in results if r.get("id")]
    payload = {
        "scan_time": datetime.now().isoformat(timespec="seconds"),
        "emails": emails_min,
        "kept_ids": kept_ids,
    }
    CACHE_PATH.write_text(json.dumps(payload))


def load_scan_cache() -> dict | None:
    if not CACHE_PATH.exists():
        return None
    try:
        return json.loads(CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


# ── Inbox cleanup ───────────────────────────────────────────────────────────

def clean_inbox(service, results: list[dict] | None = None, days: int = 30,
                apply: bool = False, email_cache: dict | None = None,
                keep_ids: set[str] | None = None):
    """Mark unread emails from job-aggregator senders as read IF they're not in
    the scanner's results. Aggregator emails the scanner surfaced stay unread.

    email_cache: optional dict mapping message-id -> email dict (from run_gmail_search),
    used to skip individual metadata API calls for already-fetched emails.

    keep_ids: when provided (--from-cache path), match the keep set by message ID
    rather than the (sender, date) tuple derived from `results`. New arrivals
    not in the cache are still processed by the standard rules (surfaced /
    subject safety net) because the query already restricts to known aggregator
    senders.
    """
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y/%m/%d")
    sender_clauses = " OR ".join(f"from:{s}" for s in CLEAN_INBOX_SENDERS)
    query = f"is:unread ({sender_clauses}) after:{cutoff}"

    print(f"\n{BOLD}Inbox cleanup{RESET}")
    print(f"  Querying unread aggregator emails: {query[:80]}…")

    cache = email_cache or {}

    candidates: list[dict] = []
    page_token = None
    while True:
        resp = service.users().messages().list(
            userId="me", q=query, maxResults=500, pageToken=page_token
        ).execute(num_retries=3)
        candidates.extend(resp.get("messages", []) or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    using_ids = keep_ids is not None
    # Keep-set: scanner's surfaced results. From-cache path matches by ID;
    # in-process path falls back to (sender, date) because the result dicts
    # passed through display logic may not carry IDs.
    keep_keys: set[tuple[str, str]] = set()
    if not using_ids:
        for r in results or []:
            sender = (r.get("from") or "").strip().lower()
            date = (r.get("date") or "").strip()
            if sender and date:
                keep_keys.add((sender, date))

    to_mark: list[dict] = []
    kept_count = 0
    kept_subject_safety = 0
    cache_hits = 0
    for meta in candidates:
        msg_id = meta["id"]
        # All candidates here are from known aggregator senders (the query
        # restricts to CLEAN_INBOX_SENDERS), so we trust the standard "surfaced
        # + subject safety net" rules even for new arrivals that weren't in the
        # cached scan. The risk — a buried internship in a new digest with no
        # intern keyword in the subject — only matters when --from-cache is
        # used, and that path explicitly trades freshness for speed.
        if msg_id in cache:
            cached = cache[msg_id]
            sender_raw = cached.get("from", "")
            date_raw = cached.get("date", "")
            subject = cached.get("subject", "(no subject)")
            cache_hits += 1
        else:
            msg = service.users().messages().get(
                userId="me", id=msg_id, format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute(num_retries=3)
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            sender_raw = headers.get("From", "")
            date_raw = headers.get("Date", "")
            subject = headers.get("Subject", "(no subject)")

        if using_ids:
            in_keep = msg_id in keep_ids
        else:
            in_keep = (sender_raw.strip().lower(), date_raw.strip()) in keep_keys
        if in_keep:
            kept_count += 1
            continue
        # Safety net: keep unread if subject mentions BOTH an internship keyword
        # AND a software/tech keyword. Covers emails the scanner missed because
        # its per-query result cap (30) cut them off, while still dropping
        # non-tech internships (accounting, marketing) that the scanner now
        # filters out. Subject-only check — we don't fetch the body here.
        if _subject_mentions_internship(subject) and _body_mentions_software(subject):
            kept_subject_safety += 1
            continue
        to_mark.append({"id": meta["id"], "from": sender_raw, "subject": subject, "date": date_raw})

    cache_note = f", {cache_hits} from cache" if cache_hits else ""
    print(f"  Found {len(candidates)} unread aggregator email(s){cache_note}; keeping {kept_count} (scanner-surfaced) "
          f"+ {kept_subject_safety} (subject mentions internship); {len(to_mark)} to mark as read.\n")

    if not to_mark:
        print("  Nothing to mark.\n")
        return

    for m in to_mark:
        print(f"  • {m['subject'][:70]}")
        print(f"      {DIM}from {m['from']}  ·  {m['date']}{RESET}")

    if not apply:
        print(f"\n  {DIM}(Dry run — re-run with --apply to mark these {len(to_mark)} emails as read){RESET}\n")
        return

    print(f"\n  Marking {len(to_mark)} email(s) as read…")
    ids = [m["id"] for m in to_mark]
    # batchModify cap is 1000 ids per call
    for i in range(0, len(ids), 1000):
        service.users().messages().batchModify(
            userId="me",
            body={"ids": ids[i:i + 1000], "removeLabelIds": ["UNREAD"]},
        ).execute(num_retries=3)
    print(f"  Marked {len(to_mark)} email(s) as read\n")


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
        type=int, default=100,
        help="Max emails to fetch per query (default: 100)"
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
    parser.add_argument(
        "--clean-inbox",
        action="store_true",
        help="After analysis, list unread aggregator emails (Glassdoor/LinkedIn/Jobright/ZipRecruiter/Indeed) "
             "without internship content as read. Dry-run unless --apply is also given.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="With --clean-inbox, actually mark the emails as read (otherwise dry-run only).",
    )
    parser.add_argument(
        "--from-cache",
        action="store_true",
        help="Skip the LLM scan and run --clean-inbox against the cached scan results "
             "from the previous run. ~1 second vs ~5 minutes. Requires --clean-inbox.",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Use rule-based full-body keyword filter instead of the LLM. ~100x faster "
             "but no per-email summary. Useful for cleanup runs.",
    )
    args = parser.parse_args()

    if args.model:
        OLLAMA_MODEL = args.model
    DEBUG = args.debug

    print(f"\n{BOLD}Internship Scanner{RESET}")
    if args.fast:
        print(f"{DIM}Rule-based analysis (no LLM){RESET}\n")
    else:
        print(f"{DIM}Local analysis with {OLLAMA_MODEL} via Ollama{RESET}\n")

    # --from-cache short-circuit: skip Ollama + Gmail search entirely and just
    # run the cleanup against the persisted scan from the previous run.
    if args.from_cache:
        if not args.clean_inbox:
            print("[!] --from-cache requires --clean-inbox.")
            sys.exit(1)
        cached = load_scan_cache()
        if cached is None:
            print(f"[!] No scan cache at {CACHE_PATH.name}. Run scanner.py without --from-cache first.")
            sys.exit(1)
        print(f"Using cached scan from {cached.get('scan_time', '?')}")
        print("Connecting to Gmail...")
        service = get_gmail_service(write_access=True)
        email_cache = {e["id"]: e for e in cached.get("emails", []) if e.get("id")}
        keep_ids = set(cached.get("kept_ids", []))
        clean_inbox(
            service, days=args.days, apply=args.apply, email_cache=email_cache,
            keep_ids=keep_ids,
        )
        return

    # Check Ollama (unless --fast, which skips the LLM entirely)
    if not args.fast:
        print("[1/3] Checking Ollama...")
        check_ollama()
        print(f"  Using model: {OLLAMA_MODEL}")

    # Gmail auth (request modify scope only when needed)
    step = "[1/2]" if args.fast else "[2/3]"
    print(f"{step} Connecting to Gmail...")
    service = get_gmail_service(write_access=args.clean_inbox)

    # Search
    scope = "all emails" if args.all else "unread only"
    if args.keyword:
        print(f"  Keyword search: '{args.keyword}' ({scope})")
    else:
        print(f"  Scanning last {args.days} days ({scope})")

    emails = run_gmail_search(service, keyword=args.keyword, days=args.days, unread_only=not args.all, max_results=args.max_emails)
    print(f"  Found {len(emails)} emails to analyze")

    if not emails:
        print("\n  No emails matched the search queries.\n")
        return

    # Analyze
    if args.fast:
        print("[2/2] Rule-based analysis (no LLM)...")
        results = rule_based_analyze(emails)
    else:
        print(f"[3/3] Analyzing with {OLLAMA_MODEL} (this may take a moment)...")
        results = analyze_with_ollama(emails)

    # Display
    display_results(results)

    # Save cache so --from-cache can run a near-instant cleanup later without
    # redoing the LLM analysis.
    save_scan_cache(emails, results)

    # Export
    if args.output:
        export_json(results, args.output)

    # Inbox cleanup
    if args.clean_inbox:
        email_cache = {e["id"]: e for e in emails if e.get("id")}
        clean_inbox(service, results, days=args.days, apply=args.apply, email_cache=email_cache)


if __name__ == "__main__":
    main()
