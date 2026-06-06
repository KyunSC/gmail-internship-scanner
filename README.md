# Internship Scanner (Local)

Scans your Gmail for internship/co-op opportunities and analyzes them **locally** using Ollama. Your email content never leaves your machine for analysis (aside from the Gmail API fetch from Google, where it already lives).

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
├── scanner.py         # Main scanner — Gmail fetch + Ollama LLM analysis
├── compare.py         # Dev tool: runs both LLM and rule-based filter, diffs results
├── show_bodies.py     # Dev tool: prints extracted body text for specific emails
├── credentials.json   # Google OAuth client secret (not committed)
├── token.json         # Cached OAuth token (not committed)
├── .last_scan.json    # Accumulated scan snapshot — seen emails for incremental scans + --from-cache (not committed)
└── requirements.txt
```

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
   - Download the JSON and rename it to `credentials.json`, placed in this folder
5. On first run a browser window opens for authorization. A `token.json` is saved for future runs.

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
```

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
| `--from-cache` | Skip the LLM scan and run `--clean-inbox` against the previous run's cached results (`~1 sec` instead of ~5 min) | off |
| `--fast` | Skip the LLM and use the full-body rule-based keyword filter only (~50× faster, no per-email summary) | off |
| `--rescan` | Re-analyze emails already in the cache instead of skipping them | skip seen emails |

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

The seen-set accumulates across runs (capped at 5000 records, oldest dropped first), so emails stay skipped even if they're still unread. If nothing new has arrived, the scan prints `No new emails since the last scan.` and exits (still running `--clean-inbox` if requested, using the accumulated surfaced set).

To re-analyze everything the queries return — ignoring the cache — pass `--rescan`:

```bash
python scanner.py --rescan
```

> The cache stores only message IDs, subjects, senders, and dates — no bodies. Delete `.last_scan.json` to reset the seen-set and start fresh.

---

## Inbox cleanup (`--clean-inbox`)

`--clean-inbox` finds unread emails from job aggregators (LinkedIn, Glassdoor, Jobright, ZipRecruiter, Indeed) that the scanner did **not** surface as internship-relevant and marks them as read, clearing inbox noise.

- Without `--apply`: dry run — lists what would be marked, touches nothing
- With `--apply`: actually marks them as read
- Emails the scanner surfaced **always stay unread**
- Emails whose subject directly mentions an internship keyword are kept as a safety net even if the scanner missed them

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
- Emails where every intern listing in the body is a Fall 2026 / Automne 2026 / September 2026 position are excluded

**Note on `stage`:** The word "stage" in English matches startup funding rounds ("Late Stage", "Early Stage"). The scanner only treats `stage` as an internship signal in the body when French words appear directly before or after it (e.g. `"un stage"`, `"bénévole stage"`, `"stage développeur"`). Subject-line matching is unaffected.

### 3. LLM classify
Pre-filtered emails are sorted by Gmail message ID (stable ordering — a newly arrived email no longer reshuffles existing positions) and sent to Ollama **one email per request, twice each** (`temperature=0` then `temperature=0.5`). The two passes are unioned by email index; results only one pass surfaced are kept. This buys recall on borderline cases — buried internships in aggregator digests, in particular — at the cost of more LLM calls. Sending one email at a time also keeps the body high in the model's attention, which matters for catching listings buried mid-digest.

The Ollama request passes `think: false` so Qwen3-family models route their content into the actual response instead of an empty `<think>` block. The flag is silently ignored by non-Qwen3 models.

The prompt instructs the model to return only genuine internship/co-op/stage/student positions and to scan aggregator digest bodies (LinkedIn, Glassdoor, Jobright) for buried listings. Results include category, summary, action items, and priority.

### 4. Post-filter
LLM results are validated:
- Fields are mapped back to the original email (prevents hallucination of sender/date)
- Companies not found in the email body are nulled out
- Aggregator emails without any internship signal are dropped
- Fall 2026 / September 2026 listings are excluded per-listing (a digest with one Summer co-op and one Fall co-op still passes)
- Duplicates from LLM hallucination are removed

### 5. Display
Results are sorted by priority and color-coded by category: `INTERNSHIP` (green), `RECRUITER` (magenta), `APP CONFIRM` (blue), `REPLY` (yellow), `STATUS` (white).

---

## Dev tools

### `compare.py` — LLM vs rule-based comparison

Fetches emails once, runs both the pure rule-based full-body parser and the full LLM scanner pipeline on the same set, then diffs the results.

```bash
python compare.py              # last 30 days, unread only
python compare.py --days 60    # extend the window
python compare.py --all        # include read emails
```

Output sections:
- **RULE-BASED** — emails kept by pure keyword matching on the full body (no LLM)
- **DROPPED** — emails the rule filter excluded, with the reason (e.g. "all listings are Fall 2026")
- **LLM-BASED** — emails the LLM scanner surfaced after full analysis
- **DIFF SUMMARY** — which emails each approach caught that the other missed

Use this to tune the prompt, adjust keyword rules, or verify that a code change doesn't cause regressions.

> **Don't run this alongside `python scanner.py`.** Both invoke Ollama, and the two processes will contend for VRAM (each spawned generate request gets its own KV cache slot reserved server-side). Run them sequentially.

**Key finding from initial comparison:** The LLM correctly drops Glassdoor digest emails where `intern` appears only in the footer boilerplate (`"Create job alerts for related roles: software intern"`), while the rule-based filter cannot distinguish these from real listings. The LLM is the meaningful filter for aggregator digest noise.

### `show_bodies.py` — inspect extracted email bodies

Prints the full extracted body text for a set of email subjects, with internship keyword hits highlighted. Useful for debugging why an email is or isn't being caught.

Edit the `SUBJECTS_TO_INSPECT` set at the top of the file, then run:

```bash
python show_bodies.py
```

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

Smaller models (7b/8b) occasionally return malformed JSON or rename expected keys — the scanner tolerates this and skips bad batches, but a 14b+ model gives more consistent results per run.

---

## Privacy

- Email content is fetched from Gmail via Google's official API (HTTPS, OAuth2)
- All analysis runs locally through Ollama — no data is sent to OpenAI, Anthropic, or any other AI provider
- `credentials.json` and `token.json` stay on your machine and are already in `.gitignore`
- `.last_scan.json` (the `--from-cache` snapshot) stores only message IDs, subjects, senders, and dates — no email bodies — and is also gitignored
