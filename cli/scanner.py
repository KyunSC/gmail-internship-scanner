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
# Persisted snapshot of scans. Used by --from-cache so cleanup can skip the
# multi-minute LLM analysis, and as the "seen" set so each scan skips emails it
# already analyzed and focuses on new arrivals. Accumulated across runs.
CACHE_PATH = Path(__file__).parent / ".last_scan.json"
# Default export target for analysis results (the web dashboard reads this).
# Cleared alongside CACHE_PATH by --clear-cache.
RESULTS_PATH = Path(__file__).parent / ".last_results.json"

# Cap on how many seen-email records the cache retains, so the file stays bounded
# across many runs. Far above a personal inbox's unread volume within the scan
# window, so IDs only fall out of the seen set long after they've aged out of the
# Gmail queries anyway. Oldest records are dropped first.
SEEN_CACHE_MAX = 5000

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
    "no-reply@indeed.com",
    # Indeed's job-match subdomain — uses donotreply@match.indeed.com and similar.
    # Substring matches `from:` so any address under match.indeed.com is caught.
    "match.indeed.com",
    # ZipRecruiter uses many automated personas (alerts@, noreply@, phil@, etc.).
    # Domain match catches all of them, including future ones. Real recruiter
    # outreach via seekerteam@ziprecruiter.com is also caught — the subject
    # safety net keeps any internship-mentioning ones unread regardless.
    "ziprecruiter.com",
    # Wellfound (ex-AngelList) job digests. Restricted to the hi. subdomain that
    # sends them — team@wellfound.com (bare domain) carries account mail such as
    # email-verification codes, which must stay unread. Wellfound subjects
    # ("New jobs: <first listing> and 1 more jobs") never name a term, so the
    # subject safety net below will not rescue these: every digest the scanner
    # does not surface gets marked read.
    "team@hi.wellfound.com",
)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5:9b")
DEBUG = False

# ── Gmail Auth ──────────────────────────────────────────────────────────────

def get_gmail_service(write_access: bool = False):
    """Authenticate and return a Gmail API service instance.

    write_access=True requests the gmail.modify scope so we can mark emails as
    read. If the cached token only has readonly, we force a re-auth.

    A token that already carries modify is reused as-is for read-only runs
    rather than being reissued at readonly — see the comment below. That way one
    consent covers every later run of every script, in either direction.
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
            # Load under the scopes actually GRANTED, not the narrower set this
            # run happens to need. Passing `needed` rewrites creds.scopes, and
            # if the token then needs a refresh it gets persisted back below at
            # that narrower scope — silently demoting a modify token to readonly.
            # A single read-only run (compare.py without --apply, show_bodies.py)
            # was therefore enough to force the next --apply through the consent
            # screen again. modify is a superset of readonly, so the granted
            # token serves both; only clean_inbox(apply=True) ever writes.
            creds = Credentials.from_authorized_user_file(
                str(TOKEN_PATH), sorted(granted) or needed
            )

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


# Footer boilerplate markers. Everything from one of these to the end is the
# sender's unsubscribe/identity footer, never a job listing. LinkedIn's footer
# carries the recipient's own profile tagline ("… Software Engineering Co-op @
# McGill …"), whose "Co-op" wrongly trips the internship keyword filter. The
# listings always sit above the marker, so cutting here removes the false
# positive without dropping any real posting. Each marker is sender-specific
# text, so trimming generically is safe — it only fires on that sender's mail.
_FOOTER_MARKERS = (
    "This email was intended for",  # LinkedIn
    # Wellfound — "Not finding what you had in mind? … You're receiving this
    # notification because you're looking for jobs on Wellfound … unsubscribe".
    # Written without the apostrophe so a curly-quote template variant still
    # matches. Every Wellfound mail ends with it, listings always above it.
    "receiving this notification because",
)


def _strip_footer(text: str) -> str:
    cut = len(text)
    for marker in _FOOTER_MARKERS:
        i = text.find(marker)
        if i != -1:
            cut = min(cut, i)
    return text[:cut].strip()


def _normalize_body(text: str) -> str:
    text = html.unescape(text)
    text = re.sub("[​-‏‪-‮⁠﻿]", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text)
    # Collapse runs of any single non-alphanumeric char (decorative separators like ===, ---, ***)
    text = re.sub(r"([^\w\s])\1{3,}", r"\1\1\1", text)
    return _strip_footer(text.strip())


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
  describing a student role, sender being a recruiter or career address. Research,
  R&D, researcher, and research-assistant roles (including French equivalents such
  as recherche, chercheur/chercheuse, and assistant/auxiliaire de recherche) count
  as relevant technical fields when they are explicitly internships/student roles.
- For job-alert digest emails (LinkedIn, Glassdoor, Jobright, Wellfound), scan the ENTIRE body for
  any internship/co-op/stage/student listing, including research/R&D internships, that
  is in the Montreal area for in-person/hybrid work OR explicitly fully remote anywhere
  — not just the headline. Surface the email if ANY listing in it qualifies, even if it
  appears in a recommendations section.

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
5. LOCATION — only include job LISTINGS located in the Montreal area (Montreal,
   Greater Montreal, Laval, Longueuil, Brossard) for in-person/hybrid work, OR
   listings that are explicitly fully REMOTE from anywhere. EXCLUDE hybrid/on-site
   listings clearly located elsewhere (Toronto, Ottawa, Vancouver, Calgary, USA,
   etc.). EXCEPTION: application
   confirmations, recruiter outreach, interview invitations, and status updates about
   the user's OWN applications are always relevant — never drop those for location.
6. EXCLUDE QA/testing-focused roles, even if they are internships or co-ops:
   QA Engineer, QA Analyst, Quality Assurance, Software Tester, Automation Tester,
   Test Engineer, Testing Intern, and SDET. Automation Engineer roles are relevant
   unless the listing is explicitly QA/testing-focused.

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

Research, R&D, researcher, and research-assistant roles count as relevant technical fields \
when explicitly labeled as internships/student roles. This includes French equivalents such \
as recherche, chercheur/chercheuse, and assistant/auxiliaire de recherche.

INCLUDE only if at least one applies:
- Subject contains intern, internship, stage, stagiaire, co-op, coop, or student
- Body clearly describes a student / intern / co-op position
- Recruiter or career-address message about an internship application
For digest emails (LinkedIn, Glassdoor, Jobright, Wellfound), scan the FULL body — include if ANY \
listing is an internship that is in the Montreal area for in-person/hybrid work OR \
explicitly fully remote from anywhere, even if buried in recommendations.

LOCATION: include a listing only if it is in the Montreal area (Montreal, Laval, Longueuil, \
Brossard) for in-person/hybrid work, or explicitly fully remote from anywhere; exclude \
hybrid/on-site listings clearly in other cities (Toronto, Ottawa, Vancouver, USA). \
Application confirmations, recruiter outreach, and interview invites about the user's own \
applications are kept regardless of location.

EXCLUDE: full-time roles ("junior", "senior", "mid-level", "lead", "associate", "engineer", \
"developer", "analyst", "specialist") when not explicitly labeled as an internship; \
QA/testing-focused roles such as QA Engineer, QA Analyst, Quality Assurance, Software \
Tester, Automation Tester, Test Engineer, Testing Intern, or SDET; newsletters; promos; \
Quora digests. Automation Engineer roles are relevant unless explicitly QA/testing-focused.

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
    # Wellfound (ex-AngelList) startup digests, from team@hi.wellfound.com.
    # Strictly a tightening: every card is stamped "| Internship" regardless of
    # the real role (a "Senior (Level 3) Quality Analyst | 12 years of exp" is
    # labelled Internship too), so the intern keyword carries no signal here and
    # the per-listing software + location + season gates do all the work.
    "wellfound.com",
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
    "développement", "developpement",
    "programmer", "programming", "coding",
    "artificial intelligence", "intelligence artificielle",
    "computer science", "computer engineering", "computer engineer",
    "informatique", "génie logiciel", "genie logiciel",
    "frontend", "front-end",
    "backend", "back-end",
    "full-stack", "fullstack", "full stack",
    "devops", "site reliability",
    "data science", "data scientist", "data analyst", "data engineer",
    "machine learning", "deep learning",
    # Research roles are common in AI/software internship alerts. Include both
    # English and French title variants so the shared rule pre-filter, LLM
    # pre-filter, and cleanup subject safety net all recognize them.
    "research", "researcher", "research assistant",
    "research scientist", "research engineering", "research engineer",
    "research and development", "r&d", "r & d",
    "recherche", "chercheur", "chercheuse",
    "assistant de recherche", "assistante de recherche",
    "auxiliaire de recherche", "ingénieur de recherche", "ingenieur de recherche",
    "cybersecurity", "cyber security",
    "cloud engineer", "cloud developer",
    "embedded systems", "embedded software", "embedded engineer", "firmware",
    "robotics",
    "ios developer", "ios engineer", "android developer", "android engineer",
    "mobile developer", "mobile engineer",
    "python", "javascript", "typescript",
    "automation engineer",
    "tech intern", "technology intern", "it intern",
    "software engineer", "software engineering",
    "systems engineer", "systems engineering",
    "hardware engineer", "fpga",
    "sde intern", "swe intern",
    "web developer", "web engineer",
    # NOTE: bare "engineering intern" / "engineering internship" /
    # "engineering co-op" / "engineering student" USED to live here as "title
    # phrases". They were removed: they are effectively bare "engineering",
    # which the comment above this list explicitly rules out, and on real
    # LinkedIn digests they matched a construction firm (Aecon "Co-op
    # Engineering Student"), a baking-ingredients maker (AB Mauri "Engineering
    # Intern"), and an aerospace supplier (Howmet "engineering intern"). None
    # were software. Software-qualified titles still match through the
    # "software engineering" / "computer engineering" entries above, so nothing
    # genuinely wanted is lost — only the unqualified word.
    "stage en génie", "stage en informatique", "stagiaire en informatique",
)

# Phrases that LOOK like a software keyword but name another field. Blanked out
# of the text before keyword matching (see _body_mentions_software) so the
# discipline, not the bare noun, decides.
#
# "<discipline> engineering" covers the qualified half of the problem above —
# "Electrical Engineering Intern" (AeroCardia), "Industrial Engineering Intern"
# (Signify). "test engineering" is also handled by _EXCLUDED_ROLE_RES; it is
# repeated here so this pass stands on its own.
_NON_SOFTWARE_ENGINEERING_RE = re.compile(
    r"\b(?:civil|mechanical|electrical|industrial|chemical|structural|mining|"
    r"production|manufacturing|aerospace|environmental|materials|biomedical|test)"
    r"\s+engineer(?:ing|s)?\b",
    re.IGNORECASE,
)

# "research" is in SOFTWARE_KEYWORDS because AI/ML research roles are a real
# source of hits, but bare it also matches "Equity Research Intern" (Wall
# Street Oasis, surfaced by a real LinkedIn digest). Veto the finance/medical
# senses; "AI research", "research engineer", "research scientist" still match.
_NON_SOFTWARE_RESEARCH_RE = re.compile(
    r"\b(?:equity|market(?:ing)?|investment|economic|econometric|clinical|"
    r"medical|pharmaceutical|legal|policy|consumer|user|art|historical|"
    r"literary|humanities)\s+research\b",
    re.IGNORECASE,
)

# Location filter — the user only wants internships they can actually take:
# in-person/hybrid listings must be in the Montreal area, while fully remote
# listings can be anywhere. Application confirmations, recruiter outreach, and
# interview invites are kept regardless of location (enforced via the LLM prompt,
# and by leaving non-aggregator emails un-location-filtered in the rule path).
_MONTREAL_AREA_REGEXES = (
    re.compile(r"\bmontr[eé]al\b", re.IGNORECASE),
    re.compile(r"\bmtl\b", re.IGNORECASE),
    re.compile(r"\bgreater montreal\b", re.IGNORECASE),
    re.compile(r"\bgrand montr[eé]al\b", re.IGNORECASE),
    re.compile(r"\b(?:laval|longueuil|brossard)\b", re.IGNORECASE),
)

_REMOTE_REGEXES = (
    re.compile(r"\bremote\b", re.IGNORECASE),
    re.compile(r"\bt[eé]l[eé][\s\-]?travail\b", re.IGNORECASE),
    re.compile(r"\bwork[\s\-]?from[\s\-]?home\b", re.IGNORECASE),
    re.compile(r"\bwfh\b", re.IGNORECASE),
)

# "stage" alone matches English "Late Stage", "Early Stage", etc.
# Only treat it as an internship signal in the body when French context surrounds it.
_STAGE_FRENCH_RE = re.compile(
    r"(?:(?:un|de|du|en|le|la|au|bénévole|offre|recherche|cherche|propose)\s+stage\b"
    r"|\bstage\s+(?:en|de|du|chez|pour|rémunéré|développeur|developer|informatique|logiciel|étudiant|pratique))",
    re.IGNORECASE,
)

# Exclude QA/testing-focused titles without dropping normal software roles that
# merely mention writing tests as part of the work.
_EXCLUDED_ROLE_RES = (
    re.compile(
        r"\bq\.?\s*a\.?\s+(?:engineer|analyst|automation|tester|testing|intern|"
        r"internship|co-?op|student|role|position|technician|specialist|"
        r"associate|assistant|lead|coordinator)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:intern|internship|co-?op|student)\s+q\.?\s*a\.?\b", re.IGNORECASE),
    re.compile(r"\bquality\s+assurance\b", re.IGNORECASE),
    re.compile(
        r"\bquality\s+(?:engineer|analyst|intern|internship|co-?op|student|"
        r"technician|specialist|associate|assistant|lead|coordinator|control)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bautomation\s+tester\b", re.IGNORECASE),
    re.compile(r"\bmanual\s+tester\b", re.IGNORECASE),
    re.compile(r"\bsoftware\s+tester\b", re.IGNORECASE),
    re.compile(r"\bsoftware\s+test\s+(?:intern|internship|co-?op|student|role|position)\b", re.IGNORECASE),
    re.compile(r"\btest\s+engineer\b", re.IGNORECASE),
    re.compile(r"\btest\s+automation\b", re.IGNORECASE),
    re.compile(r"\btest\s+(?:intern|internship|co-?op|student|role|position)\b", re.IGNORECASE),
    re.compile(r"\btester\s+(?:intern|internship|co-?op|student|role|position)\b", re.IGNORECASE),
    re.compile(r"\btesting\s+(?:intern|internship|co-?op|student|role|position)\b", re.IGNORECASE),
    re.compile(r"\b(?:intern|internship|co-?op|student)\s+(?:tester|testing)\b", re.IGNORECASE),
    re.compile(r"\bsdet\b", re.IGNORECASE),
)

# The internship term the user IS targeting: Summer <TARGET_YEAR> starts.
# Listings express this term three ways — the season word ("Summer 2027"), a
# season code ("S2027"), or a literal date range ("June 2027 - August 2027") —
# and all three must match or a real internship gets marked read by cleanup.
TARGET_YEAR = "2027"
_YY = TARGET_YEAR[-2:]

# Months a summer term can start in ON THEIR OWN. July onward is absent: a
# July/August start is a Fall term. April is absent too — it would misread
# "Winter 2027 term (Jan 2027 - Apr 2027)" as summer — and only qualifies as
# the head of a range (see _EARLY_START).
_START = r"(?:may|mai|jun(?:e)?|juin)"
# Early starts that count only as the head of a range ending in summer:
# "April 2027 through August 2027" is a 16-week summer term.
_EARLY_START = r"(?:apr(?:il)?|avr(?:il)?|may|mai|jun(?:e)?|juin)"
# Months a summer term can END in. Never qualifying on their own — only as the
# tail of a range whose head already qualified. Matching August alone would let
# "August 2027 - December 2027" through as summer.
_END = r"(?:jul(?:y)?|juil(?:let)?|aug(?:ust)?|ao[uû]t|sep(?:t)?(?:ember|embre)?)"
_SEP = r"(?:\s*(?:[-–—]|to|through|thru|until|till|au|jusqu['’]au|à|/)\s*)"
_DAY = r"(?:\s*\d{1,2}(?:st|nd|rd|th)?\s*,?)?"

TARGET_TERM_REGEX = re.compile(
    # "Summer 2027" / "Summer Internship 2027" / "2027 summer" / "été 2027"
    rf"\b(?:summer|[ée]t[ée])\b.{{0,20}}\b{TARGET_YEAR}\b"
    rf"|\b{TARGET_YEAR}\b.{{0,20}}\b(?:summer|[ée]t[ée])\b"
    # the unspaced form "summer2027", which the \b above rejects
    rf"|(?:summer|[ée]t[ée]){TARGET_YEAR}\b"
    # "Summer '27" / "Summer 27"
    rf"|\b(?:summer|[ée]t[ée])\s*['’]?{_YY}\b"
    # Season codes: S2027, S-2027, SU27
    rf"|\bs[-\s]?{TARGET_YEAR}\b"
    rf"|\bsu[-\s]?['’]?{_YY}\b"
    # Qualifying start month + year: "May 2027", "June 4, 2027", "Jun. 2027"
    rf"|\b{_START}\.?{_DAY}\s*{TARGET_YEAR}\b"
    # Range whose year trails the end month: "June - August 2027", "Apr to Aug 2027"
    rf"|\b{_EARLY_START}\.?{_DAY}{_SEP}{_END}\.?{_DAY}\s*{TARGET_YEAR}\b"
    # ...and the same range with the year on both ends: "May 2027 - Aug 2027"
    rf"|\b{_EARLY_START}\.?{_DAY}\s*{TARGET_YEAR}{_SEP}{_END}"
    # ISO dates: 2027-05, 2027/06/01
    rf"|\b{TARGET_YEAR}[-/]0?[456]\b"
    # Numeric ranges only — a bare "05/2027" is too often a reference number
    rf"|\b0?[456]/{TARGET_YEAR}\s*[-–—]\s*0?\d{{1,2}}/{TARGET_YEAR}\b",
    re.IGNORECASE,
)

_GLASSDOOR_LISTINGS_HEAD_RE = re.compile(
    r"Your job listings for\s+\w+\s+\d+,\s+\d{4}",
    re.IGNORECASE,
)

_GLASSDOOR_LISTINGS_FOOT_RE = re.compile(
    r"See more jobs|Want more listings"
)

# The saved alert's name, as Glassdoor prints it in the header banner
# ("Job alert: Full Stack Engineer"). The same string is echoed right after the
# date line, where it would otherwise read as a job listing.
_GLASSDOOR_ALERT_NAME_RE = re.compile(
    r"Job alert:\s*(.+?)\s+Your job listings for", re.IGNORECASE
)

# The alert's location ("Mississauga, ON" / "Remote"), which trails the echoed
# alert name. Bounded to a few words so it can't swallow the first listing.
_GLASSDOOR_ECHO_LOCATION_RE = re.compile(
    r"^\s*(?:[\w.'’&-]+(?:\s+[\w.'’&-]+){0,3},\s*[A-Z]{2}|remote)\b", re.IGNORECASE
)


def _clean_glassdoor_body(body: str) -> str:
    """Strip Glassdoor digest chrome before chunk analysis. Without this, the
    user's saved alert name (e.g. "Backend Developer Internship") echoes in
    the header banner, the listings subtitle, and the footer "Sent Daily Edit"
    block — injecting "intern" + "developer" keywords into a digest whose
    actual listings are all full-time. The footer also has "Create alert"
    CTAs like "web developer intern Create" that hit the same way.

    Cuts everything through "Your job listings for [date]", then removes just
    the alert-name echo (and its trailing location) that follows. Cutting to
    the first ★ instead — as this used to — also destroyed the FIRST listing,
    because Glassdoor puts each company's rating chip BEFORE its ★ separator,
    so the headline listing sits between the date line and that first ★. That
    listing is the one Glassdoor names in "X is hiring for Y" subjects, which
    carry no location and therefore can't pass on the subject alone — so the
    email's only real listing was being thrown away before it was ever read.

    Falls back to the old star cut when the alert name can't be recovered,
    since leaving an unrecognized echo in place would reintroduce the false
    keywords this function exists to remove. Trims from "See more jobs" /
    "Want more listings" onward either way."""
    m = _GLASSDOOR_LISTINGS_HEAD_RE.search(body)
    if m:
        m_alert = _GLASSDOOR_ALERT_NAME_RE.search(body)
        alert = m_alert.group(1).strip() if m_alert else ""
        body = body[m.end():]
        rest = body.lstrip()
        if alert and rest[:len(alert)].lower() == alert.lower():
            rest = rest[len(alert):]
            loc = _GLASSDOOR_ECHO_LOCATION_RE.match(rest)
            if loc:
                rest = rest[loc.end():]
            body = rest
        else:
            star = body.find("★")
            if star >= 0:
                body = body[star + 1:]
    m2 = _GLASSDOOR_LISTINGS_FOOT_RE.search(body)
    if m2:
        body = body[:m2.start()]
    return body


# LinkedIn job-alert cards read "<title> <company> · <location> (<mode>)
# [<salary>] <badges>", where badges are any run of: Actively recruiting,
# Easy Apply, Fast growing, or a social-proof count. A single marker is NOT
# enough — cards routinely end with only "Easy Apply" or "12 connections" — so
# the separator must consume a RUN of them.
#
# The social-proof count has more shapes than it looks: the live corpus carries
# "1 connection", "95 mutual connections", "49 school alumni", "1 school alum"
# AND "1 company alum". Missing one silently fuses that card into its
# neighbour, which is the exact cross-listing leak this splitter exists to stop
# — so the qualifier and the plural are both optional rather than enumerated.
_LINKEDIN_CARD_SEP_RE = re.compile(
    r"(?:\s*(?:Actively recruiting|Easy Apply|Fast growing"
    r"|\d+\s+(?:(?:mutual|school|company)\s+)?(?:connections?|alum(?:ni)?)))+",
    re.IGNORECASE,
)

# Not every card carries a badge. LinkedIn's "Jobs that match your profile"
# format prints bare "<title> <company> · <location>" rows, so badge-run
# splitting alone fuses them — one real digest ran three cards together and
# swallowed an undated Autodesk software internship into a Fall 2026 chunk.
#
# The structural end of a card is the end of its location field. Matching that
# needs care: "Canada" is also a company-name suffix ("KPMG Canada · Montreal,
# QC"), and a mode parenthetical also appears inside titles ("Java FullStack
# Developer (Hybrid) Morgan Stanley"). Both false cuts are ruled out by the
# trailing (?!\s*·) — a real card end is never immediately followed by the
# separator that introduces a location.
#
# Salary ("$78-$78 / hour") is deliberately NOT consumed: the badge run that
# follows it supplies its own cut, and an orphaned salary fragment carries no
# internship, software, or location keyword, so it cannot qualify anything.
_LINKEDIN_CARD_END_RE = re.compile(
    # Trailing \b on every alternative is load-bearing: without it "Canada"
    # matches the first six letters of "Canadian Tire" and cuts mid-word.
    r"(?:,\s*(?:QC|ON|BC|AB|MB|SK|NS|NB|NL|PE|YT|NT|NU"
    r"|Qu[ée]bec|Ontario|British Columbia|Alberta)\b(?:,\s*Canada\b)?"
    r"|\b(?:Canada|France|Area)\b)"
    r"(?:\s*\((?:On-site|Hybrid|Remote)\))?"
    r"(?:\s*(?:Actively recruiting|Easy Apply|Fast growing"
    r"|\d+\s+(?:(?:mutual|school|company)\s+)?(?:connections?|alum(?:ni)?)))*"
    r"(?!\s*·)",
    re.IGNORECASE,
)

# "Apply now You have connections at <X> Ask them about the job <Person>
# <Headline> Message <Person> <Headline> Message" — the referral block LinkedIn
# puts in "Your saved job at X is still available" mail. Those headlines are
# people's job titles ("Solution Associate", "Business Analyst", "Software
# Engineer @Google"), so left in place they hand the adjacent listing a
# software keyword it never had. Runs to the next section header, or to the end
# of what survives the footer cut.
_LINKEDIN_APPLY_CHROME_RE = re.compile(
    r"Apply now\b.*?(?:Your other saved jobs|$)",
    re.IGNORECASE | re.DOTALL,
)

# Footer chrome after the last card.
_LINKEDIN_FOOT_RE = re.compile(
    r"See all (?:saved )?jobs|Install LinkedIn Widgets", re.IGNORECASE)

# Header chrome. LinkedIn opens with "Jobs at <X> and more Your job alert for
# <ALERT NAME>" — and the alert name is user-chosen ("Software Engineer"), so
# it injects a false software keyword into the first card, exactly the echo
# problem _clean_glassdoor_body solves for Glassdoor. Some bodies continue
# "New jobs in <CITY> match your preferences.", which leaks a location too.
_LINKEDIN_PREFS_RE = re.compile(r"match your preferences\.?", re.IGNORECASE)

# "match your preferences" is only in 6 of 24 bodies, so it can't be the only
# anchor. Reliable fallback: the subject is "<first listing title> at
# <company>" and that title appears verbatim in the body — seek to it and cut.
_LINKEDIN_HEAD_FALLBACK_RE = re.compile(
    r"Your job alert for"
    r"|Based on your title and location\.\s*Update"
    r"|is still available\.",
    re.IGNORECASE,
)


def _split_linkedin_cards(text: str) -> list[str]:
    """Cut a cleaned LinkedIn body into one chunk per card.

    Cut points are the union of two independent markers — the end of a badge
    run and the end of a location field — because neither covers every format
    on its own: alert digests give every card a badge but "Jobs that match your
    profile" mail gives some cards none, while a location field is present on
    every card but only recognisable when it is not immediately followed by the
    "·" that introduces one.

    Unlike a str.split the matched text stays with the card it belongs to,
    which matters for the location field: cutting "Canada (Remote)" out of a
    chunk would take the only evidence that the listing is remote with it."""
    cuts = {0, len(text)}
    for rx in (_LINKEDIN_CARD_END_RE, _LINKEDIN_CARD_SEP_RE):
        for m in rx.finditer(text):
            if m.end() > m.start():
                cuts.add(m.end())
    bounds = sorted(cuts)
    chunks = [text[a:b].strip(" ·-") for a, b in zip(bounds, bounds[1:])]
    return [c for c in chunks if c]


def _clean_linkedin_body(subject: str, body: str) -> str:
    """Strip LinkedIn job-alert chrome before chunk analysis.

    The header names the user's own alert ("Your job alert for Software
    Engineer") and often its city ("New jobs in Montreal match your
    preferences."). Left in place, both glue onto the first card and hand it a
    software keyword and a location it never earned — the same echo problem
    _clean_glassdoor_body exists for.

    Three anchors, most specific first: the preferences sentence (which ends
    the whole banner, alert name and city included), then the subject's listing
    title, then a generic header phrase. The title anchor is preferred over the
    generic one because cutting at "Your job alert for" leaves the alert name
    itself behind."""
    m_prefs = _LINKEDIN_PREFS_RE.search(body)
    if m_prefs:
        body = body[m_prefs.end():]
    else:
        title = subject.rsplit(" at ", 1)[0].strip() if " at " in subject else ""
        idx = body.find(title) if title else -1
        if idx > 0:
            body = body[idx:]
        else:
            m = _LINKEDIN_HEAD_FALLBACK_RE.search(body)
            if m:
                body = body[m.end():]
    m2 = _LINKEDIN_FOOT_RE.search(body)
    if m2:
        body = body[:m2.start()]
    return _LINKEDIN_APPLY_CHROME_RE.sub(" ", body)


def _split_aggregator_listings(sender: str, body: str, subject: str = "") -> list[str]:
    """Split a digest body into per-listing chunks. Glassdoor uses ★ as the
    listing separator (after each company's rating); Jobright closes each
    recommendation with "APPLY NOW"; Indeed match digests close each listing
    with "Easily apply"; Wellfound closes each card with "Learn More".
    Senders without a known digest format return the whole body as a single
    chunk. Glassdoor bodies are pre-cleaned to drop alert chrome that would
    otherwise inject false internship signals."""
    s = (sender or "").lower()
    if "glassdoor.com" in s and "★" in body:
        cleaned = _clean_glassdoor_body(body)
        return [c.strip() for c in cleaned.split("★") if c.strip()]
    if "jobright.ai" in s and "APPLY NOW" in body:
        return [c.strip() for c in body.split("APPLY NOW") if c.strip()]
    if "match.indeed.com" in s and "Easily apply" in body:
        return [c.strip() for c in body.split("Easily apply") if c.strip()]
    # Wellfound cards read "<title> <company> / <size> Employees | <mode>,
    # <city> | <n> years of exp | Internship Actively Hiring Learn More", so the
    # CTA closes each listing. Non-digest Wellfound mail (welcome, application
    # updates, verification codes) has no "Learn More" and falls through to the
    # whole-body chunk, which is why the marker is part of the guard.
    if "wellfound.com" in s and "Learn More" in body:
        return [c.strip() for c in body.split("Learn More") if c.strip()]
    # LinkedIn closes each card with a run of badges rather than a single CTA.
    # Non-digest LinkedIn mail (invitations, profile-view nudges, post
    # notifications) carries no badge run and falls through to the whole-body
    # chunk, which is why the marker is part of the guard.
    if "linkedin.com" in s and _LINKEDIN_CARD_SEP_RE.search(body):
        return _split_linkedin_cards(_clean_linkedin_body(subject, body)) or [body]
    return [body]


# A term that is definitely NOT Summer 2027 — an off-target season word with a
# year ("Fall 2026", "Automne 2026", "Winter 2027"), or a date range starting in
# a month no summer term starts in ("Jan-April '27", "September 2026").
#
# Used only where listings do not state a term at all (see _is_off_target_term):
# there, "says nothing" and "says Fall 2026" have to be told apart, which the
# absence of TARGET_TERM_REGEX cannot do on its own.
_OFF_SEASON = r"(?:fall|autumn|automne|winter|hiver|spring|printemps)"
# Months a summer term never starts in. May/June/April are absent — they are
# TARGET_TERM_REGEX's business — and July/August are absent because a range like
# "August 2026 - December 2026" is already caught by its Fall season word, while
# matching them bare would misread a summer term's END month as off-target.
_OFF_START = (
    r"(?:jan(?:uary)?|janv(?:ier)?|feb(?:ruary)?|f[ée]vr?(?:ier)?"
    r"|sep(?:t)?(?:ember|embre)?|oct(?:ober|obre)?|nov(?:ember|embre)?"
    r"|d[ée]c(?:ember|embre)?)"
)
# Any year, four-digit or apostrophised. \b cannot anchor the left of "'27"
# (apostrophe and space are both non-word), so that branch anchors on the right.
_ANY_YEAR = r"(?:\b20\d{2}\b|['’]\d{2}\b)"

_OFF_TARGET_TERM_REGEX = re.compile(
    # "Fall 2026", "Internship - Automne 2026", "2027 winter"
    rf"\b{_OFF_SEASON}\b.{{0,20}}{_ANY_YEAR}"
    rf"|{_ANY_YEAR}.{{0,20}}\b{_OFF_SEASON}\b"
    # Range headed by an off-target month: "Jan-April '27", "Sept - Dec 2026"
    rf"|\b{_OFF_START}\.?\s*[-–—/]\s*\w+\.?\s*{_ANY_YEAR}"
    # Bare off-target start: "September 2026", "janvier 2027"
    rf"|\b{_OFF_START}\.?\s*{_ANY_YEAR}",
    re.IGNORECASE,
)

# Senders whose listings are title + company + location only and essentially
# never state a term. Requiring an explicit "Summer 2027" from these drops every
# listing they carry, on-target or not, which makes the sender permanently mute
# rather than filtered. Glassdoor and Wellfound cards carry enough text to state
# a term, so they stay on the strict rule.
_UNDATED_CARD_SENDERS = ("linkedin.com",)


def _states_no_term_by_convention(sender: str) -> bool:
    s = (sender or "").lower()
    return any(domain in s for domain in _UNDATED_CARD_SENDERS)


def _is_off_target_term(chunk: str, lenient: bool = False) -> bool:
    """True unless the listing targets the term the user wants (Summer 2027).

    A listing can name that term as a season word ("Summer 2027"), a season code
    ("S2027"), or a date range ("June 2027 - August 2027") — see
    TARGET_TERM_REGEX.

    lenient=True inverts the burden of proof for senders whose cards never state
    a term (see _UNDATED_CARD_SENDERS): a listing is off-target only if it names
    a DIFFERENT term. Silence is not evidence there, so treating it as such
    rejected "Software Engineer Intern · LevelOps · Montreal" for saying nothing
    at all. Off-target listings in the same digest ("Machine Learning Intern/Co-op
    (Winter 2027)") still name their term, so they are still rejected."""
    if TARGET_TERM_REGEX.search(chunk):
        return False
    if lenient:
        return bool(_OFF_TARGET_TERM_REGEX.search(chunk))
    return True


def _season_checked_chunks(sender: str, body: str, subject: str = "") -> list[str]:
    """The listings the off-target-season filter judges an email by.

    For aggregator digests these are exactly the listings that QUALIFY the
    email — the same intern + software + location + not-excluded test
    _has_software_internship_listing surfaces it on. Season-checking every
    chunk with an internship keyword instead let the two filters disagree about
    which listing mattered: a digest could be surfaced on an off-target listing
    (e.g. a Fall 2026 front-end role) and then rescued from this filter by an
    unrelated undated one (a QA technician posting in another province), so
    neither listing was one the user wanted.

    Non-aggregator bodies are deliberately not location-filtered (see
    _mentions_location), so they fall back to a plain internship-keyword test
    over the whole body.

    Either way the test is _body_mentions_internship, never the subject
    variant: that one also matches a bare "stage", which is a company-maturity
    label in these digests ("Cohere · Late Stage", "TechInsights · Growth
    Stage"), not an internship. Counting those made almost every chunk look
    like an intern listing, and one undated impostor was enough to keep an
    all-off-target digest alive."""
    chunks = _split_aggregator_listings(sender, body, subject)
    if _is_aggregator(sender):
        return [c for c in chunks if _is_qualifying_listing(c)]
    return [c for c in chunks if _body_mentions_internship(c)]


def _all_intern_listings_excluded(sender: str, body: str, subject: str = "") -> bool:
    """True if every listing this email qualifies on names an off-target term
    (anything other than Summer 2027, however it is written). False only if at
    least one qualifying listing targets Summer 2027."""
    chunks = _season_checked_chunks(sender, body, subject)
    if not chunks:
        return True
    lenient = _states_no_term_by_convention(sender)
    return all(_is_off_target_term(c, lenient) for c in chunks)


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


# Indeed sends engagement/marketing nudges from no-reply@indeed.com — e.g.
# "Stand out by sending a quick message to {company}" / "Confirm your interest".
# Their bodies reference a role + location, so the normal intern+software+location
# co-occurrence wrongly surfaces them as internships. These are never job
# postings: real job alerts come from match.indeed.com and application
# confirmations from indeedapply@indeed.com, both of which keep normal handling.
INDEED_NOISE_SENDERS = ("no-reply@indeed.com", "noreply@indeed.com")


def _is_indeed_noise_sender(sender: str) -> bool:
    s = (sender or "").lower()
    return any(addr in s for addr in INDEED_NOISE_SENDERS)


# Word-boundary patterns for internship keywords. Plain substring matching wrongly
# fires on superstrings: "intern" inside "international/internal/internet", "coop"
# inside "cooperative", "co-op" inside "co-operative". Anchored alternatives with
# explicit plural handling avoid that while still catching plurals. "stage" is
# excluded here — too noisy in English ("late/early stage") — and handled
# separately (subject: bare word; body: French context only, via _STAGE_FRENCH_RE).
_INTERNSHIP_WORD_RE = re.compile(
    r"\b(?:interns?|internships?|stagiaires?|students?|co-?ops?)\b",
    re.IGNORECASE,
)
_STAGE_WORD_RE = re.compile(r"\bstages?\b", re.IGNORECASE)


def _subject_mentions_internship(subject: str) -> bool:
    s = subject or ""
    return bool(_INTERNSHIP_WORD_RE.search(s) or _STAGE_WORD_RE.search(s))


def _body_mentions_internship(body: str) -> bool:
    b = body or ""
    return bool(_INTERNSHIP_WORD_RE.search(b) or _STAGE_FRENCH_RE.search(b))


def _body_mentions_software(body: str) -> bool:
    """True if the text names a software/tech field.

    Non-software senses of otherwise-matching nouns are blanked first, so
    "Electrical Engineering Intern" and "Equity Research Intern" no longer read
    as software. Substituting a space (not the empty string) keeps the
    surrounding words from fusing into a new false match."""
    b = (body or "").lower()
    b = _NON_SOFTWARE_ENGINEERING_RE.sub(" ", b)
    b = _NON_SOFTWARE_RESEARCH_RE.sub(" ", b)
    return any(kw in b for kw in SOFTWARE_KEYWORDS)


def _mentions_excluded_role(text: str) -> bool:
    return bool(text and any(rx.search(text) for rx in _EXCLUDED_ROLE_RES))


def _mentions_location(text: str) -> bool:
    """True if a listing is Montreal-area or explicitly remote from anywhere."""
    if not text:
        return False
    return (
        any(rx.search(text) for rx in _MONTREAL_AREA_REGEXES)
        or any(rx.search(text) for rx in _REMOTE_REGEXES)
    )


def _is_qualifying_listing(chunk: str) -> bool:
    """True if a single listing is one the user actually wants: an internship,
    in software/tech, at an acceptable location, and not an excluded (QA /
    testing) role. Shared by the surfacing check and the season filter so both
    judge an email by the same listing."""
    return (_body_mentions_internship(chunk) and _body_mentions_software(chunk)
            and _mentions_location(chunk) and not _mentions_excluded_role(chunk))


def _has_software_internship_listing(sender: str, body: str, subject: str = "") -> bool:
    """True iff at least one body chunk contains an internship keyword, a
    software/tech keyword, AND an acceptable listing location. For aggregators
    with a known digest splitter this is per-listing; without one it falls back to
    a whole-body co-occurrence check (still strictly stricter than the previous
    "any intern keyword" test)."""
    return any(_is_qualifying_listing(c)
               for c in _split_aggregator_listings(sender, body, subject))


def _has_internship_signal(subject: str, body: str, sender: str) -> bool:
    """Combined signal check. An email must have BOTH an internship signal
    AND a software/tech signal somewhere to pass. Filters out non-tech
    internships (accounting, marketing, finance) that the subject keyword
    alone would otherwise let through. For aggregator digests, the per-chunk
    AND check (intern + software in the same listing) is the stricter form."""
    # Indeed engagement nudges (no-reply@indeed.com) aren't job postings even
    # though their bodies mention a role + location — never surface them.
    if _is_indeed_noise_sender(sender):
        return False
    if _is_aggregator(sender):
        # Excluded (QA/testing) roles are filtered PER LISTING here, never on the
        # subject. A digest's subject names just one of its listings, so vetoing
        # the whole email on it threw away the others — a digest titled "Sibelius
        # - Software Test Engineer at Avid Technology and 4 more jobs in Montreal"
        # was discarded along with the two Summer 2027 Montreal internships
        # inside it. _is_qualifying_listing still keeps any QA listing from
        # qualifying an email on its own; it just no longer vetoes its neighbours.
        #
        # Subject-only intern keyword is enough only if the subject itself also
        # names a software/tech role AND an acceptable listing location; otherwise rely
        # on the per-chunk body listing check (which also requires location).
        if (_subject_mentions_internship(subject) and _body_mentions_software(subject)
                and _mentions_location(subject) and not _mentions_excluded_role(subject)):
            return True
        return _has_software_internship_listing(sender, body, subject)
    if _mentions_excluded_role(subject):
        return False
    if _mentions_excluded_role(body):
        return False
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
    prefilter: bool = True,
) -> list[dict]:
    """Send email metadata to Ollama for classification and summarization in batches.

    prefilter=True runs the keyword/term rules before the LLM, so only plausible
    emails cost a model call — the right trade-off for a normal scan. Pass
    prefilter=False to send every email to the LLM regardless of what the rules
    think; compare.py needs this so the LLM arm is genuinely independent of the
    rule arm rather than a subset of it.
    """
    if not emails:
        return []

    if prefilter:
        signaled = [
            e for e in emails
            if _has_internship_signal(e.get("subject", ""), e.get("body", ""), e.get("from", ""))
        ]
        dropped_no_signal = len(emails) - len(signaled)
        filtered_in = [
            e for e in signaled
            if not _all_intern_listings_excluded(e.get("from", ""), e.get("body", ""),
                                                 e.get("subject", ""))
        ]
        dropped_term = len(signaled) - len(filtered_in)
        if dropped_no_signal:
            print(f"  Pre-filtered {dropped_no_signal} email(s) with no internship signal")
        if dropped_term:
            print(f"  Pre-filtered {dropped_term} email(s) with no listing targeting Summer 2027")
    else:
        filtered_in = emails
        print(f"  Pre-filter disabled — sending all {len(emails)} email(s) to the LLM")
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

        # Per-listing off-target-term exclusion (body only — subject is
        # intentionally not checked). For digest aggregators the body is split
        # into individual job listings; we drop the email only if EVERY intern
        # listing targets a term other than Summer 2027. A mixed digest with one
        # on-target intern still passes.
        body = original.get("body", "")
        if _all_intern_listings_excluded(sender, body, subject):
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
        print(f"  Filtered out {dropped_term} email(s) with no listing targeting Summer 2027")
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
        if _all_intern_listings_excluded(sender, body, subject):
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
        print(f"  Dropped {dropped_term} email(s) with no listing targeting Summer 2027")
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

def save_scan_cache(emails: list[dict], results: list[dict], merge: bool = True) -> dict:
    """Persist this scan and return the resulting cache payload.

    We only store header-level fields (no bodies) to keep the file small and
    avoid leaking content on disk. The cache serves two purposes: --from-cache
    reads it to re-run cleanup without the LLM, and the next scan reads its
    `emails` ids as the "seen" set so it can skip what it already analyzed.

    merge=True (default) accumulates this run's emails/kept_ids into the existing
    cache so the seen set grows across runs. The email list is capped at
    SEEN_CACHE_MAX, dropping the oldest records first; kept_ids are pruned to ids
    still present in that capped set so they stay bounded too."""
    emails_min = [
        {"id": e.get("id", ""), "subject": e.get("subject", ""),
         "from": e.get("from", ""), "date": e.get("date", "")}
        for e in emails if e.get("id")
    ]
    kept_ids = [r.get("id") for r in results if r.get("id")]

    existing = (load_scan_cache() or {}) if merge else {}
    prev_emails = existing.get("emails", []) or []
    prev_kept = existing.get("kept_ids", []) or []

    # Merge emails by id, preserving order (oldest first) so the cap drops the
    # oldest. A re-seen id keeps its slot but refreshes its stored metadata.
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for e in prev_emails + emails_min:
        eid = e.get("id")
        if not eid:
            continue
        if eid not in by_id:
            order.append(eid)
        by_id[eid] = e
    merged_emails = [by_id[eid] for eid in order]
    if len(merged_emails) > SEEN_CACHE_MAX:
        merged_emails = merged_emails[-SEEN_CACHE_MAX:]

    live_ids = {e["id"] for e in merged_emails}
    merged_kept = [eid for eid in dict.fromkeys(prev_kept + kept_ids) if eid in live_ids]

    payload = {
        "scan_time": datetime.now().isoformat(timespec="seconds"),
        "emails": merged_emails,
        "kept_ids": merged_kept,
    }
    CACHE_PATH.write_text(json.dumps(payload))
    return payload


def load_scan_cache() -> dict | None:
    if not CACHE_PATH.exists():
        return None
    try:
        return json.loads(CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def clear_scan_cache() -> list[str]:
    """Delete the persisted scan cache and exported results so the next run
    re-analyzes every email from scratch. Returns the names of files removed."""
    removed = []
    for p in (CACHE_PATH, RESULTS_PATH):
        if p.exists():
            p.unlink()
            removed.append(p.name)
    return removed


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

    Returns the list of emails actually marked read (empty on dry-run or when
    there's nothing to mark) so callers can fold them into the scan cache.
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
        if (
            _subject_mentions_internship(subject)
            and _body_mentions_software(subject)
            and TARGET_TERM_REGEX.search(subject)
        ):
            kept_subject_safety += 1
            continue
        to_mark.append({"id": meta["id"], "from": sender_raw, "subject": subject, "date": date_raw})

    cache_note = f", {cache_hits} from cache" if cache_hits else ""
    print(f"  Found {len(candidates)} unread aggregator email(s){cache_note}; keeping {kept_count} (scanner-surfaced) "
          f"+ {kept_subject_safety} (subject mentions internship); {len(to_mark)} to mark as read.\n")

    if not to_mark:
        print("  Nothing to mark.\n")
        return []

    for m in to_mark:
        print(f"  • {m['subject'][:70]}")
        print(f"      {DIM}from {m['from']}  ·  {m['date']}{RESET}")

    if not apply:
        print(f"\n  {DIM}(Dry run — re-run with --apply to mark these {len(to_mark)} emails as read){RESET}\n")
        return []

    print(f"\n  Marking {len(to_mark)} email(s) as read…")
    ids = [m["id"] for m in to_mark]
    # batchModify cap is 1000 ids per call
    for i in range(0, len(ids), 1000):
        service.users().messages().batchModify(
            userId="me",
            body={"ids": ids[i:i + 1000], "removeLabelIds": ["UNREAD"]},
        ).execute(num_retries=3)
    print(f"  Marked {len(to_mark)} email(s) as read\n")
    return to_mark


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
    parser.add_argument(
        "--rescan",
        action="store_true",
        help="Re-analyze emails already in the cache instead of skipping them. By "
             "default each run skips emails seen in a previous scan and only analyzes "
             "new arrivals.",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Delete the scan cache (.last_scan.json) and exported results "
             "(.last_results.json), then exit. The next scan re-analyzes every "
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
        marked = clean_inbox(
            service, days=args.days, apply=args.apply, email_cache=email_cache,
            keep_ids=keep_ids,
        )
        # Fold the emails we just marked read into the seen-set so they're
        # tracked like analyzed emails. Not added to kept_ids — they're noise.
        if marked:
            save_scan_cache(marked, [])
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

    # Skip emails already analyzed in a previous run so each scan focuses on new
    # arrivals. The cache accumulates every email it has seen; --rescan forces a
    # full re-analysis of everything the queries returned.
    cached = load_scan_cache() or {}
    seen_ids = {e.get("id") for e in cached.get("emails", []) if e.get("id")}
    if not args.rescan and seen_ids:
        fresh = [e for e in emails if e.get("id") not in seen_ids]
        skipped = len(emails) - len(fresh)
        if skipped:
            print(f"  Skipping {skipped} email(s) already scanned (in cache); {len(fresh)} new to analyze")
        emails = fresh

    if not emails:
        print("\n  No new emails since the last scan.\n")
        # Nothing new to analyze, but still clear inbox noise if asked — use the
        # accumulated surfaced set (same keep logic as --from-cache).
        if args.clean_inbox:
            email_cache = {e["id"]: e for e in cached.get("emails", []) if e.get("id")}
            marked = clean_inbox(
                service, days=args.days, apply=args.apply,
                email_cache=email_cache, keep_ids=set(cached.get("kept_ids", [])),
            )
            if marked:
                save_scan_cache(marked, [])
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

    # Save cache (merging into the accumulated seen-set) so the next scan skips
    # these and --from-cache can run a near-instant cleanup later.
    cache = save_scan_cache(emails, results)

    # Export
    if args.output:
        export_json(results, args.output)

    # Inbox cleanup. Match the keep-set by id against the accumulated surfaced
    # ids so emails surfaced in earlier runs — skipped this time — stay unread.
    if args.clean_inbox:
        email_cache = {e["id"]: e for e in cache.get("emails", []) if e.get("id")}
        marked = clean_inbox(
            service, days=args.days, apply=args.apply,
            email_cache=email_cache, keep_ids=set(cache.get("kept_ids", [])),
        )
        if marked:
            save_scan_cache(marked, [])


if __name__ == "__main__":
    main()
