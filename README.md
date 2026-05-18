# Internship Scanner (Local)

Scans your Gmail for internship/co-op opportunities and analyzes them **locally** using Ollama. Your email content never leaves your machine for your privacy (aside from the Gmail API fetch from Google, where it already lives).

## Architecture

```
Gmail (Google servers) --API--> Your machine --Ollama--> Results (local only)
```

- **Gmail API** fetches email metadata + full bodies (read-only, OAuth2)
- **Ollama** runs an LLM locally to classify and summarize
- Nothing is sent to any third-party AI service

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

Pull a model (qwen2.5:14b is the recommended default — larger models produce fewer JSON-format errors):

```bash
ollama pull qwen2.5:14b
```

Start the server (if not already running):

```bash
ollama serve
```

### 2. Set up Gmail API credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or use an existing one)
3. Enable the **Gmail API**:
   - Go to APIs & Services > Library
   - Search "Gmail API" and click Enable
4. Create OAuth credentials:
   - Go to APIs & Services > Credentials
   - Click **Create Credentials** > **OAuth client ID**
   - If prompted, configure the consent screen (External is fine for personal use; add your email as a test user)
   - Application type: **Desktop app**
   - Download the JSON file
5. Rename the downloaded file to [credentials.json](credentials.json) and place it in this folder

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
```

---

## Usage

### Scan recent unread emails (last 30 days)

```bash
python scanner.py
```

By default, only **unread** emails are scanned. Use `--all` to include read emails too.

### Search by keyword

```bash
python scanner.py -k "software intern Montreal"
python scanner.py -k "EXFO"
```

### Options

| Flag | Description | Default |
|------|-------------|---------|
| `-k`, `--keyword` | Search keyword | (none, scans recent) |
| `-d`, `--days` | Days to look back | 30 |
| `-m`, `--model` | Ollama model name | `llama3.1:8b` |
| `-n`, `--max-emails` | Max emails to fetch per query | 30 |
| `-o`, `--output` | Export results to JSON | (none) |
| `--all` | Scan all emails, not just unread | unread only |
| `--debug` | Print the raw LLM response for each batch | off |

### Examples

```bash
# Scan last 60 days
python scanner.py -d 60

# Scan everything (read + unread)
python scanner.py --all

# Use the recommended 14b model
python scanner.py -m qwen2.5:14b

# Search and export to file
python scanner.py -k "internship" -o results.json

# Debug a flaky batch
python scanner.py --debug
```

### Environment variables

```bash
OLLAMA_URL=http://localhost:11434   # Ollama server URL
OLLAMA_MODEL=llama3.1:8b            # Default model (override with -m)
```

---

## How it works

1. **Fetch** — runs several targeted Gmail queries (internship/co-op/stage keywords, application/interview subjects, recruiter senders) and deduplicates by message ID.
2. **Normalize** — extracts plain-text bodies from MIME parts, strips HTML/URLs/zero-width characters, and truncates to 5000 chars. Aggregator emails (Glassdoor, Jobright) are rewritten so the LLM sees the headline job rather than the "more jobs you might like" recommendations block.
3. **Classify** — emails are sent to Ollama in batches of 5 with a strict prompt that requires the subject to mention an internship keyword.
4. **Filter** — results are validated against the originals: aggregator emails without internship keywords in the subject are dropped, Fall 2026 / Automne 2026 listings are excluded, hallucinated companies are nulled out, and duplicate subjects are removed.
5. **Display** — results are sorted by priority and color-coded by category (internship / recruiter / confirmation / reply / status).

---

## First run

On first run, a browser window will open asking you to authorize Gmail read access. After authorizing, a [token.json](token.json) file is saved locally so you don't need to re-auth each time.

**The app only requests read-only access** -- it cannot send, delete, or modify your emails.

---

## Recommended models

| Model | RAM needed | Speed | Quality |
|-------|-----------|-------|---------|
| `qwen2.5:14b` | ~10 GB | Medium | Very good (recommended) |
| `qwen2.5:7b` | ~5 GB | Fast | Good |
| `llama3.1:8b` | ~6 GB | Fast | Good |
| `mistral:7b` | ~5 GB | Fast | Good |
| `gemma2:9b` | ~7 GB | Fast | Good |
| `llama3.1:70b` | ~40 GB | Slow | Excellent |

Smaller models (7b/8b) occasionally return malformed JSON or rename the expected `results` key — the scanner tolerates this and skips bad batches, but you'll get more usable hits per run on a 14b+ model.

---

## Privacy

- Email content is fetched from Gmail via Google's official API (HTTPS, OAuth2)
- All analysis runs locally through Ollama -- no data is sent to OpenAI, Anthropic, or any other AI provider
- [credentials.json](credentials.json) and [token.json](token.json) stay on your machine
- Both are already in [.gitignore](.gitignore)
