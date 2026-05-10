# Internship Scanner (Local)

Scans your Gmail for internship/co-op opportunities and analyzes them **locally** using Ollama. Your email content never leaves your machine (aside from the Gmail API fetch from Google, where it already lives).

## Architecture

```
Gmail (Google servers) --API--> Your machine --Ollama--> Results (local only)
```

- **Gmail API** fetches email metadata + snippets (read-only, OAuth2)
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

Pull a model (llama3.1:8b is a good balance of speed and quality):

```bash
ollama pull llama3.1:8b
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
5. Rename the downloaded file to `credentials.json` and place it in this folder

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
```

---

## Usage

### Scan recent emails (last 30 days)

```bash
python scanner.py
```

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
| `-m`, `--model` | Ollama model name | llama3.1:8b |
| `-n`, `--max-emails` | Max emails per query | 30 |
| `-o`, `--output` | Export results to JSON | (none) |

### Examples

```bash
# Scan last 60 days
python scanner.py -d 60

# Use a different model
python scanner.py -m mistral:7b

# Search and export to file
python scanner.py -k "internship" -o results.json

# Use a larger model for better analysis
python scanner.py -m llama3.1:70b
```

### Environment variables

```bash
OLLAMA_URL=http://localhost:11434   # Ollama server URL
OLLAMA_MODEL=llama3.1:8b           # Default model
```

---

## First run

On first run, a browser window will open asking you to authorize Gmail read access. After authorizing, a `token.json` file is saved locally so you don't need to re-auth each time.

**The app only requests read-only access** -- it cannot send, delete, or modify your emails.

---

## Recommended models

| Model | RAM needed | Speed | Quality |
|-------|-----------|-------|---------|
| `llama3.1:8b` | ~6 GB | Fast | Good |
| `mistral:7b` | ~5 GB | Fast | Good |
| `llama3.1:70b` | ~40 GB | Slow | Excellent |
| `gemma2:9b` | ~7 GB | Fast | Good |
| `qwen2.5:7b` | ~5 GB | Fast | Good |

---

## Privacy

- Email content is fetched from Gmail via Google's official API (HTTPS, OAuth2)
- All analysis runs locally through Ollama -- no data is sent to OpenAI, Anthropic, or any other AI provider
- `credentials.json` and `token.json` stay on your machine
- Add both to `.gitignore` if you version-control this folder
