# Internship Scanner (Local)

Scans your Gmail for internship/co-op opportunities and analyzes them **locally** using Ollama. Your email content never leaves your machine for analysis (aside from the Gmail API fetch from Google, where it already lives). It also checks the popular community internship repos on GitHub (SimplifyJobs, vanshb03, and two Canada-focused lists) for new postings.

## Why I built this

I'm looking for internships, and my inbox turned into a wall of job-alert digests from LinkedIn, Glassdoor, Jobright, Wellfound, Indeed and others. Most of them were noise: full-time roles, US-only postings, Fall or Winter terms, non-software "student" jobs, and the same listing re-sent across a dozen emails. The few internships I actually wanted were often buried halfway down a digest whose subject line never said "intern", so skimming subjects wasn't enough and opening every email took too long.

I wanted something that would:

- **Read the whole email, not just the subject**, and pull out only Summer 2027 software internships located in Canada
- **Clear out the noise** by marking the irrelevant digests as read, while never touching anything that might matter
- **Keep my email private**: the classification runs on a local model through Ollama, so my inbox isn't sent to a third-party AI service
- **Go through the popular internship GitHub repos too**: lists like [SimplifyJobs](https://github.com/SimplifyJobs/Summer2027-Internships), [vanshb03](https://github.com/vanshb03/Summer2027-Internships), [michelleokolie](https://github.com/michelleokolie/canada-tech-internships-summer-2027) and [negarprh](https://github.com/negarprh/Canadian-Tech-Internships-2027) often post new internships hours before any email digest arrives, but they're long and hard to keep track of. `github_watcher.py` applies the same filters to them and shows only the postings I haven't seen yet, with the apply link

## Architecture

```
Gmail (Google servers) --API--> Your machine --Ollama--> Results (local only)
```

- **Gmail API** fetches email metadata + full bodies (read-only or modify, OAuth2)
- **Ollama** runs an LLM locally to classify and summarize
- Nothing is sent to any third-party AI service

---

## Project structure

```
gmail-internship-scanner/
├── cli/                   # Local command-line scanner (Python) — this README
│   ├── scanner.py         # Main scanner — Gmail fetch + Ollama LLM analysis + inbox cleanup
│   ├── github_watcher.py  # New Summer 2027 postings from GitHub listing repos (SimplifyJobs, vanshb03, michelleokolie, negarprh)
│   ├── compare.py         # Dev tool: runs both LLM and rule-based filter on aggregator emails, diffs results
│   ├── compare_prompts.py # Dev tool: A/B two prompt templates on the same email set
│   ├── compare_models.py  # Dev tool: opt-in, read-only baseline-vs-candidate model trial
│   ├── dump_sender.py     # Dev tool: prints extracted bodies for a sender, to learn a new digest format
│   ├── test_*.py          # Offline regression tests (no Gmail, no Ollama)
│   ├── fixtures/          # Sample digest bodies used by the tests
│   ├── credentials.json   # Google OAuth client secret (not committed)
│   ├── token.json         # Cached OAuth token (not committed)
│   ├── .last_scan.json    # Accumulated scan snapshot — seen emails for incremental scans + --from-cache (not committed)
│   ├── .github_seen.json  # github_watcher.py's seen postings + saved ETags (not committed)
│   ├── .model_trials/     # compare_models.py output (not committed)
│   └── requirements.txt
└── web/                   # Browser dashboard (fully client-side; runs Gmail + LLM in the browser) — see web/README.md
```

> **The commands in this README are run from the `cli/` directory** — `cd cli` first (or prefix paths with `cli/`). The browser app under `web/` is separate and shares nothing with the CLI at runtime; its setup lives in [web/README.md](web/README.md).

---

## Setup

### 1. Install Ollama

```bash
# macOS
brew install ollama

# Linux
curl -fsSL https://ollama.com/install.sh | sh

# Windows: download from https://ollama.com/download
```

Pull a model (`qwen3.5:9b` is the recommended default — newer generation, fits comfortably on 16 GB+ Macs):

```bash
ollama pull qwen3.5:9b
```

Start the server (if not already running):

```bash
ollama serve
```

### 2. Set up Gmail API credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or use an existing one)
3. Enable the **Gmail API**: APIs & Services > Library > search "Gmail API" > Enable
4. Create OAuth credentials:
   - APIs & Services > Credentials > Create Credentials > OAuth client ID
   - Configure the consent screen if prompted (External is fine; add your email as a test user)
   - Application type: **Desktop app**
   - Download the JSON and rename it to `credentials.json`, placed in the `cli/` folder
5. On first run a browser window opens for authorization. A `token.json` is saved for future runs.

### 3. Install Python dependencies

```bash
cd cli
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
```

> **Run the Python commands below from the `cli/` virtual environment.** Activate it
> with `source venv/bin/activate` each time you open a new terminal, then use the
> documented `python ...` commands. Alternatively, without activating it, prefix a
> command with the virtual-environment interpreter—for example,
> `venv/bin/python scanner.py` or `venv/bin/python compare.py --apply`.

---

## Usage

### Run the scanner (LLM analysis)

```bash
python scanner.py
```

Scans unread emails from the last 30 days, classifies them with Ollama, and prints results color-coded by category.

### Scan options

| Flag | Description | Default |
|------|-------------|---------|
| `-k`, `--keyword` | Search keyword (e.g. `"EXFO"` or `"software intern Montreal"`) | (none) |
| `-d`, `--days` | Days to look back | `30` |
| `-m`, `--model` | Ollama model name | `qwen3.5:9b` |
| `-n`, `--max-emails` | Max emails to fetch per query | `100` |
| `-o`, `--output` | Export results to a JSON file | (none) |
| `--all` | Include read emails, not just unread | unread only |
| `--debug` | Print raw LLM response for each batch | off |
| `--clean-inbox` | After scanning, mark non-internship aggregator emails as read (dry run unless `--apply` is also passed) | off |
| `--apply` | Actually apply the `--clean-inbox` changes | dry run |
| `--from-cache` | Skip the LLM scan and run `--clean-inbox` against the previous run's cached results (`~1 sec` instead of ~5 min). Requires `--clean-inbox` | off |
| `--fast` | Skip the LLM and use the full-body rule-based keyword filter only (~50× faster, no per-email summary) | off |
| `--rescan` | Re-analyze emails already in the cache instead of skipping them | skip seen emails |
| `--clear-cache` | Delete `.last_scan.json` and `.last_results.json`, then exit | — |

### Examples

```bash
# Scan last 60 days
python scanner.py -d 60

# Scan everything (read + unread)
python scanner.py --all

# Use a smaller/faster model
python scanner.py -m qwen2.5:7b

# Search by keyword and export to file
python scanner.py -k "internship" -o results.json

# Re-analyze everything, including emails seen in a previous scan
python scanner.py --rescan

# Scan and clean inbox in one step (dry run first)
python scanner.py --clean-inbox

# Scan and actually mark non-internship emails as read
python scanner.py --clean-inbox --apply

# Fast cleanup using the previous scan's cached results (~1 sec)
python scanner.py --clean-inbox --apply --from-cache

# No-LLM scan using full-body keyword filtering (~5 sec end-to-end)
python scanner.py --fast
python scanner.py --fast --clean-inbox --apply

# Debug raw LLM responses
python scanner.py --debug
```

### Environment variables

```bash
OLLAMA_URL=http://localhost:11434   # Ollama server URL
OLLAMA_MODEL=qwen3.5:9b             # Default model (override with -m)
```

---

## Incremental scans (skip already-seen emails)

Every scan records the emails it analyzed in `.last_scan.json`. On the next run the scanner skips anything already in that cache and only analyzes **new arrivals** — so re-running is fast and each run's output is just what's landed since last time.

```bash
python scanner.py        # first run: analyzes everything in the window
python scanner.py        # later run: "Skipping N email(s) already scanned; M new to analyze"
```

The seen-set accumulates across runs (capped at 5000 records, oldest dropped first), so emails stay skipped even if they're still unread. If nothing new has arrived, the scan prints `No new emails since the last scan.` and exits (normal cleanup has nothing to act on; use the explicit `--from-cache` mode for cached cleanup).

To re-analyze everything the queries return — ignoring the cache — pass `--rescan`:

```bash
python scanner.py --rescan
```

> The cache stores only message IDs, subjects, senders, dates, and company/title keys for listing dedupe — no bodies. Run `python scanner.py --clear-cache` (or delete `.last_scan.json`) to reset the seen-set and start fresh. `compare.py` shares the same cache.

---

## Inbox cleanup (`--clean-inbox`)

`--clean-inbox` acts only on successfully evaluated emails from the current scan. Messages omitted by fetch caps, keyword searches, fetch failures, or analysis failures are untouched. It finds unread emails from job aggregators (LinkedIn job alerts, Glassdoor, Jobright, Wellfound, ZipRecruiter, Indeed, Intern Insider) that the scanner did **not** surface as internship-relevant and marks them as read, clearing inbox noise.

- Without `--apply`: dry run — lists what would be marked, touches nothing
- With `--apply`: actually marks them as read
- Emails the scanner surfaced **always stay unread**
- Emails whose subject directly mentions an internship keyword are kept as a safety net even if the scanner missed them

Aggregators repeat listings across digests (Glassdoor re-sends a listing in "recently viewed" and recommendation sections), so one internship could otherwise keep a dozen emails unread. Cleanup keeps only the **newest unread copy** of each qualifying listing: a surfaced email whose qualifying listings all appear in a newer surfaced email is marked read, and an email with any listing no newer email has stays unread. Listings are matched per sender (never across senders) by company and title. An email whose listings can't be identified reliably, or that was surfaced only by the LLM, is never deduped.

This requires the Gmail **modify** scope. On first use with `--clean-inbox`, a new browser authorization prompt will appear.

### Fast cleanup (`--from-cache`)

Every successful scan saves its surfaced email IDs to `.last_scan.json`. Pass `--from-cache` to re-run cleanup against that snapshot without redoing the multi-minute LLM analysis:

```bash
python scanner.py                                        # full scan, saves cache (slow)
python scanner.py --clean-inbox --apply --from-cache     # uses cache (~1 second)
```

Emails that arrived **after** the cached scan are evaluated against the standard rules (subject safety net + scanner-surfaced set). They get marked read if their subject doesn't mention an internship keyword and they weren't in the cached scan's surfaced set. The risk this leaves on the table: a buried internship in a new digest whose subject doesn't mention `intern`/`co-op`/`stage`. Run a fresh `python scanner.py` periodically (e.g. once a day) to catch those.

### Speed modes at a glance

| Goal | Command | Time |
|---|---|---|
| Triage with rich per-listing summaries | `python scanner.py` | ~5 min |
| Cleanup inbox using yesterday's scan | `python scanner.py --clean-inbox --apply --from-cache` | ~1 sec |
| One-shot scan + cleanup, no LLM | `python scanner.py --fast --clean-inbox --apply` | ~5 sec |

`--fast` is appropriate when you trust the rule-based filter for cleanup decisions but don't need the LLM's listing-by-listing breakdown of each digest. In practice the two modes agree on which emails are internships when every body containing an `intern`/`co-op`/`stage` keyword genuinely is one — see `compare.py` to verify for your own inbox.

---

## How it works

### 1. Fetch
Runs several targeted Gmail queries (internship/co-op/stage keywords, application/interview subjects, recruiter senders) and deduplicates by message ID. Full bodies are extracted from MIME parts and truncated to 5000 chars. Both `text/plain` and `text/html` versions are normalized (HTML stripped, URLs removed, whitespace collapsed) and the longer of the two is kept — LinkedIn job alerts in particular ship a `text/plain` part that's just footer boilerplate, with the actual listings only in `text/html`.

### 2. Pre-filter (rule-based)
Before hitting the LLM, emails with no internship signal are dropped:
- Subject, body, or sender must contain an internship keyword (`intern`, `internship`, `co-op`, `coop`, `student`, `stagiaire`, or `stage` in French context only — see note below)
- Only qualifying listings that explicitly say Summer 2027 are kept; undated and other-term listings are excluded
- Digest listings must be in a software/tech field (software, developer, data, ML/AI, research, embedded, etc.) — a bare "student" or "engineering intern" hit isn't enough
- QA/testing-focused roles (QA Engineer, QA Analyst, Software Tester, …) are excluded; Automation Engineer roles are kept unless explicitly QA
- Digest listings must be located in Canada — any province or major city, or remote roles based in Canada ("Canada (Remote)", "Remote in Canada"). A bare "remote" doesn't count, and US-only listings are dropped. Your own application confirmations, recruiter outreach, and interview invites are kept regardless of location

**Note on `stage`:** The word "stage" in English matches startup funding rounds ("Late Stage", "Early Stage"). The scanner only treats `stage` as an internship signal in the body when French words appear directly before or after it (e.g. `"un stage"`, `"bénévole stage"`, `"stage développeur"`). Subject-line matching is unaffected.

### 3. LLM classify
Pre-filtered emails are sorted by Gmail message ID (stable ordering — a newly arrived email no longer reshuffles existing positions) and sent to Ollama **one email per request, twice each** (`temperature=0` then `temperature=0.5`, set by `LLM_PASS_TEMPERATURES`; each request times out after 180 s). The two passes are unioned by email index; results only one pass surfaced are kept. This buys recall on borderline cases — buried internships in aggregator digests, in particular — at the cost of more LLM calls. Sending one email at a time also keeps the body high in the model's attention, which matters for catching listings buried mid-digest.

The Ollama request passes `think: false` so Qwen3-family models route their content into the actual response instead of an empty `<think>` block. The flag is silently ignored by non-Qwen3 models.

The prompt instructs the model to return only genuine software internship/co-op/stage/student positions (excluding QA/testing roles) and to scan aggregator digest bodies (LinkedIn, Glassdoor, Jobright, Wellfound, Intern Insider) for buried listings. Results include category, summary, action items, and priority.

### 4. Post-filter
LLM results are validated:
- Fields are mapped back to the original email (prevents hallucination of sender/date)
- Companies not found in the email body are nulled out
- Aggregator emails without any internship signal are dropped
- Listings without an explicit Summer 2027 term are excluded per-listing (a digest with one Summer 2027 co-op and one Fall co-op still passes)
- Duplicates from LLM hallucination are removed

### 5. Display
Results are sorted by priority and color-coded by category: `INTERNSHIP` (green), `RECRUITER` (magenta), `APP CONFIRM` (blue), `REPLY/FOLLOW-UP` (yellow), `STATUS UPDATE` (white).

### Failure handling
An email counts as evaluated only once **both** LLM passes complete for it. Failed classification passes are reported separately, recorded in the cache as failed, and retried on the next run. A successful rescan replaces an email's old keep/drop decision; a failed one preserves it. Cleanup never touches failed emails. The explicit `--from-cache` mode keeps its broader, potentially stale cleanup behaviour, but still excludes known failed analyses and never records cleanup as analysis.

---

## GitHub listings watcher (`github_watcher.py`)

Community repos list new internships within hours of opening — often before any email digest arrives. `github_watcher.py` reads four of them:

| Source | Repo | Format |
|---|---|---|
| `simplify` | [SimplifyJobs/Summer2027-Internships](https://github.com/SimplifyJobs/Summer2027-Internships) | `listings.json` |
| `vansh` | [vanshb03/Summer2027-Internships](https://github.com/vanshb03/Summer2027-Internships) | `listings.json` |
| `michelle` | [michelleokolie/canada-tech-internships-summer-2027](https://github.com/michelleokolie/canada-tech-internships-summer-2027) | `listings.json` (Canada only) |
| `negar` | [negarprh/Canadian-Tech-Internships-2027](https://github.com/negarprh/Canadian-Tech-Internships-2027) | README table (Canada only) |

It applies the same rules as the scanner (Summer 2027, software, no QA roles, located in Canada), and prints only postings you haven't seen on a previous run, newest first, with the apply link. No Gmail, no Ollama.

```bash
venv/bin/python github_watcher.py              # new postings since last run (marks them seen)
venv/bin/python github_watcher.py --no-mark    # preview without marking anything seen
venv/bin/python github_watcher.py --show-all   # every current match, seen or not
venv/bin/python github_watcher.py --anywhere   # skip the Canada location filter
venv/bin/python github_watcher.py --include-termless  # also postings that name no term, labelled [term not stated]
venv/bin/python github_watcher.py --days 60    # widen the posting-age window (default 30)
venv/bin/python github_watcher.py -o gh.json   # also export shown postings as JSON
venv/bin/python github_watcher.py --reset      # forget seen postings
```

The first run shows every current match. After that, a repo that hasn't changed answers with HTTP 304 via its saved ETag, so a quiet re-run downloads nothing. A role listed in several repos is shown once — matched by apply link, or by company + title allowing small spelling differences ("TRC" vs "TRC Companies"). Sources live in `SOURCES` at the top of the file; add another repo there if it publishes `listings.json` or a README table with Company / Role / Location / Apply / Date Posted columns.

Most negarprh postings don't say which term they're for, so by default they only show when the title says Summer 2027. `--include-termless` shows them too, but many turn out to be Fall or Winter roles — check the posting before applying.

---

## Dev tools

> **Don't run any of these alongside `python scanner.py`** (except `dump_sender.py`, which doesn't use Ollama). They all invoke Ollama, and concurrent processes contend for VRAM (each spawned generate request gets its own KV cache slot reserved server-side). Run them sequentially.

### `compare.py` — LLM vs rule-based comparison

Fetches unread aggregator emails (the same sender set `--clean-inbox` acts on) once, runs both the pure rule-based full-body parser and the full LLM scanner pipeline on the same set, then diffs the results. It shares `.last_scan.json` with `scanner.py`, so emails already analyzed are skipped.

```bash
python compare.py              # last 30 days, unread only
python compare.py --days 60    # extend the window
python compare.py --all        # include read emails
python compare.py -n 200       # cap emails fetched (default 500)
python compare.py --apply      # mark emails read when BOTH pipelines agree they're not internships (default is dry run)
python compare.py --clear-cache
```

Output sections:
- **RULE-BASED** — emails kept by pure keyword matching on the full body (no LLM)
- **DROPPED** — emails the rule filter excluded, with the reason (e.g. "all listings are Fall 2027")
- **LLM-BASED** — emails the LLM scanner surfaced after full analysis
- **DIFF SUMMARY** — which emails each approach caught that the other missed

Use this to tune the prompt, adjust keyword rules, or verify that a code change doesn't cause regressions. `--apply` only marks emails that both pipelines evaluated successfully and both agreed to drop.

**Key finding from initial comparison:** The LLM correctly drops Glassdoor digest emails where `intern` appears only in the footer boilerplate (`"Create job alerts for related roles: software intern"`), while the rule-based filter cannot distinguish these from real listings. The LLM is the meaningful filter for aggregator digest noise.

### `compare_prompts.py` — prompt A/B test

Fetches emails once and runs the LLM twice, with the default prompt (`PROMPT_DEFAULT`) and the shorter one (`PROMPT_TIGHT`), then prints a side-by-side diff. Use it to check that a prompt change doesn't regress classification before swapping it in.

```bash
python compare_prompts.py            # -d/--days, -n/--max-emails, --all as in scanner.py
```

### `dump_sender.py` — inspect a sender's digest format

Prints the extracted bodies of every email from a sender substring, to reverse-engineer a new digest format (separators, header/footer chrome, body length) before teaching `scanner.py` about it. Uses a raw `from:` query so digests that never say "intern" still show up. Read-only: no Ollama, no writes.

```bash
python dump_sender.py wellfound --days 180 --all   # also: --max N (default 50), --chars N
```

### `compare_models.py` — opt-in model trial (Qwen3.8 27B IQ4_XS)

From `cli/`, with Ollama running:

```bash
ollama pull qwen3.5:9b
ollama pull hf.co/unsloth/Qwen3.8-27B-GGUF:UD-IQ4_XS
venv/bin/python compare_models.py
# Optional overrides (output must stay inside .model_trials/):
venv/bin/python compare_models.py --baseline qwen3.5:9b \
  --candidate hf.co/unsloth/Qwen3.8-27B-GGUF:UD-IQ4_XS \
  --days 30 --max-emails 20 --output .model_trials/trial.json
```

The exact model tag is required; a different quantization will not satisfy the
availability check. The runner checks both models on a synthetic email before
accessing Gmail, then fetches up to 20 unread aggregator emails **once**. It uses
read-only Gmail operations (an existing broader OAuth token may be reused), never
marks messages read, and never reads or writes normal scanner caches.

Both models receive identical content sequentially, with keyword prefiltering
disabled. Existing prompts, two-pass temperatures, context sizing, 180-second
request timeout, body limit, and post-analysis safety filters are preserved.
Models are unloaded between runs. No memory limits or content limits are changed
to make the candidate fit.

Ignored `cli/.model_trials/` JSON artifacts include the private email sample,
model names, timings, results, successful/failed IDs, keep/drop disagreements,
and macOS memory pressure and swap snapshots every ten seconds. Failures are
excluded from disagreement counts, never treated as negative classifications.
The command exits nonzero on failed evaluations or blocked preflight. Review all
disagreements and several agreements against the stored bodies; this small sample
is not an accuracy benchmark. Success also requires no sustained memory pressure,
which must be assessed from the snapshots; completion alone does not certify fit.

The default remains `qwen3.5:9b`. To explicitly opt into a normal scan with the
candidate after reviewing the trial:

```bash
venv/bin/python scanner.py --model hf.co/unsloth/Qwen3.8-27B-GGUF:UD-IQ4_XS
```

Unlike the isolated comparison, that normal scanner invocation uses its usual
scan caches.

---

## Offline regression checks

No Gmail or Ollama needed. From `cli/`, using the project virtualenv:

```bash
venv/bin/python -m unittest discover -v   # test_scan_safety, test_github_watcher, test_compare_models, test_interninsider_cleanup
venv/bin/python test_linkedin.py
venv/bin/python test_wellfound.py
venv/bin/python test_interninsider.py
venv/bin/python test_target_term.py
venv/bin/python test_location.py
```

The last five are standalone scripts (not `unittest` cases), so `discover` doesn't pick them up — run them directly.

---

## Recommended models

| Model | RAM needed | Speed | Quality |
|-------|-----------|-------|---------|
| `qwen3.5:9b` | ~6 GB | Fast | Very good (recommended) |
| `qwen2.5:14b` | ~10 GB | Medium | Very good |
| `qwen2.5:7b` | ~5 GB | Fast | Good |
| `llama3.1:8b` | ~6 GB | Fast | Good |
| `mistral:7b` | ~5 GB | Fast | Good |
| `gemma2:9b` | ~7 GB | Fast | Good |
| `qwen3:30b-a3b` | ~18 GB | Slow on ≤24 GB Macs (paging) | Excellent — but needs `iogpu.wired_limit_mb` bump or 32 GB+ RAM to be usable |
| `llama3.1:70b` | ~40 GB | Slow | Excellent |

Smaller models (7b/8b) occasionally return malformed JSON or rename expected keys — the scanner tolerates this, records the email as failed, and retries it next run, but a 14b+ model gives more consistent results per run.

---

## Privacy

- Email content is fetched from Gmail via Google's official API (HTTPS, OAuth2)
- All analysis runs locally through Ollama — no data is sent to OpenAI, Anthropic, or any other AI provider
- `credentials.json` and `token.json` stay on your machine and are already in `.gitignore`
- `.last_scan.json` (the `--from-cache` snapshot) stores only message IDs, subjects, senders, dates, and company/title listing keys — no email bodies — and is also gitignored
- `github_watcher.py` only makes anonymous GET requests to `raw.githubusercontent.com`; nothing about you or your inbox is sent. Its `.github_seen.json` is gitignored
- `compare_models.py` writes the sampled email bodies into `.model_trials/`, which is gitignored — delete it when you're done reviewing a trial
