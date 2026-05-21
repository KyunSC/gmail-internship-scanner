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
├── scanner.py        # Main scanner — Gmail fetch + Ollama LLM analysis
├── compare.py        # Dev tool: runs both LLM and rule-based filter, diffs results
├── show_bodies.py    # Dev tool: prints extracted body text for specific emails
├── credentials.json  # Google OAuth client secret (not committed)
├── token.json        # Cached OAuth token (not committed)
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
| `-m`, `--model` | Ollama model name | `qwen2.5:14b` |
| `-n`, `--max-emails` | Max emails to fetch per query | `100` |
| `-o`, `--output` | Export results to a JSON file | (none) |
| `--all` | Include read emails, not just unread | unread only |
| `--debug` | Print raw LLM response for each batch | off |
| `--clean-inbox` | After scanning, mark non-internship aggregator emails as read (dry run unless `--apply` is also passed) | off |
| `--apply` | Actually apply the `--clean-inbox` changes | dry run |

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

# Scan and clean inbox in one step (dry run first)
python scanner.py --clean-inbox

# Scan and actually mark non-internship emails as read
python scanner.py --clean-inbox --apply

# Debug raw LLM responses
python scanner.py --debug
```

### Environment variables

```bash
OLLAMA_URL=http://localhost:11434   # Ollama server URL
OLLAMA_MODEL=qwen2.5:14b            # Default model (override with -m)
```

---

## Inbox cleanup (`--clean-inbox`)

`--clean-inbox` finds unread emails from job aggregators (LinkedIn, Glassdoor, Jobright, ZipRecruiter, Indeed) that the scanner did **not** surface as internship-relevant and marks them as read, clearing inbox noise.

- Without `--apply`: dry run — lists what would be marked, touches nothing
- With `--apply`: actually marks them as read
- Emails the scanner surfaced **always stay unread**
- Emails whose subject directly mentions an internship keyword are kept as a safety net even if the scanner missed them

This requires the Gmail **modify** scope. On first use with `--clean-inbox`, a new browser authorization prompt will appear.

---

## How it works

### 1. Fetch
Runs several targeted Gmail queries (internship/co-op/stage keywords, application/interview subjects, recruiter senders) and deduplicates by message ID. Full bodies are extracted from MIME parts, HTML is stripped, URLs are removed, and text is truncated to 5000 chars.

### 2. Pre-filter (rule-based)
Before hitting the LLM, emails with no internship signal are dropped:
- Subject, body, or sender must contain an internship keyword (`intern`, `internship`, `co-op`, `coop`, `student`, `stagiaire`, or `stage` in French context only — see note below)
- Emails where every intern listing in the body is a Fall 2026 / Automne 2026 / September 2026 position are excluded

**Note on `stage`:** The word "stage" in English matches startup funding rounds ("Late Stage", "Early Stage"). The scanner only treats `stage` as an internship signal in the body when French words appear directly before or after it (e.g. `"un stage"`, `"bénévole stage"`, `"stage développeur"`). Subject-line matching is unaffected.

### 3. LLM classify
Pre-filtered emails are sorted by Gmail message ID (stable batching — a new arriving email doesn't reshuffle existing batches) and sent to Ollama one at a time. Each batch is sent **twice** and the per-batch results are unioned by email index — this buys recall on borderline cases (buried internships in aggregator digests, in particular) at the cost of more LLM calls. The small batch size keeps each email's body high in the model's attention, which is important for catching internship listings buried mid-digest. The prompt instructs the model to return only genuine internship/co-op/stage/student positions and to scan aggregator digest bodies (LinkedIn, Glassdoor, Jobright) for buried listings. Results include category, summary, action items, and priority.

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

Fetches emails once, runs both the rule-based keyword filter and the full LLM pipeline on the same set, then diffs the results.

```bash
python compare.py              # last 30 days, unread only
python compare.py --days 60    # extend the window
python compare.py --all        # include read emails
```

Output sections:
- **RULE-BASED** — emails kept by pure keyword matching (no LLM)
- **DROPPED** — emails the rule filter excluded, with the reason (e.g. "all listings are Fall 2026")
- **LLM-BASED** — emails the LLM surfaced after full analysis
- **DIFF SUMMARY** — which emails each approach caught that the other missed

Use this to tune the prompt, adjust keyword rules, or verify that a code change doesn't cause regressions.

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
| `qwen2.5:14b` | ~10 GB | Medium | Very good (recommended) |
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
