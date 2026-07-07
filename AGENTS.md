# AGENTS.md

Guidance for coding agents working in this repo. Humans: see [README.md](README.md).

## Two independent projects

- `cli/` — the Python command-line scanner (Gmail fetch + local Ollama analysis). **All Python commands run from here.**
- `web/` — a separate, fully client-side browser dashboard. Shares nothing with the CLI at runtime.

## Running the CLI (`scanner.py`, `compare.py`, `show_bodies.py`)

These scripts live in `cli/`, not the project root, and they import each other as
sibling modules (`compare.py` does `from scanner import ...`). Two things must be
true or they fail:

1. **Working directory must be `cli/`.** Running from the project root gives
   `No such file or directory: compare.py` — there is no `compare.py` at the root.
2. **Use the project virtualenv `cli/venv`.** The system Python lacks the Google
   API packages, so the bare `python compare.py` gives
   `ModuleNotFoundError: No module named 'google.auth'`. The venv (Python 3.13)
   already has everything from `cli/requirements.txt` installed.

### Correct invocation

```bash
cd cli
venv/bin/python compare.py            # last 30 days, unread only
venv/bin/python compare.py --days 60  # extend the window
venv/bin/python compare.py --all      # include read emails
venv/bin/python compare.py --apply    # actually mark agreed-non-internships read (default is dry-run)
venv/bin/python compare.py --clear-cache
```

The same rule applies to the other scripts:

```bash
cd cli
venv/bin/python scanner.py
venv/bin/python show_bodies.py
```

Equivalent, if you'd rather activate the env first:

```bash
cd cli
source venv/bin/activate
python compare.py
```

### Runtime prerequisites (only if a command actually reaches those stages)

- **Ollama** must be running for the LLM pipeline: `ollama serve`, with a model
  pulled (`ollama pull qwen3.5:9b`). `compare.py` and `scanner.py` both call it.
- **Google OAuth**: `cli/credentials.json` (client secret) must exist; `token.json`
  is created on first auth. Neither is committed.
- **Don't run `compare.py` and `scanner.py` at the same time** — both hit Ollama
  and contend for VRAM. Run them sequentially.

If you only need to reproduce the import/module errors above, you don't need
Ollama or valid Google credentials — just the correct directory and venv.

## Setting up the venv from scratch

If `cli/venv` is missing:

```bash
cd cli
python3.13 -m venv venv
venv/bin/pip install -r requirements.txt
```
