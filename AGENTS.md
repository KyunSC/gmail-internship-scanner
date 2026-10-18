# AGENTS.md

Guidance for coding agents working in this repo. Humans: see [README.md](README.md).

## Two independent projects

- `cli/` — the Python command-line scanner (Gmail fetch + local Ollama analysis). **All Python commands run from here.**
- `web/` — a separate, fully client-side browser dashboard. Shares nothing with the CLI at runtime.
- `no-chrome-mcp/` — a frozen copy of `cli/` from before the Chrome flow
  (Ollama + `compare.py --apply`, strict Summer 2027 filter). Same rules as
  `cli/`: run from inside it with `venv/bin/python` (its `venv`,
  `credentials.json` and `token.json` are symlinks to `../cli/`). Don't port
  `cli/` changes into it unless asked. See `no-chrome-mcp/README.md`.

## Running the CLI (`scanner.py`, `compare.py`, `plan_postings.py`, `dump_sender.py`, `github_watcher.py`)

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
venv/bin/python plan_postings.py      # read-only: which digest postings to check/skip (see /internships)
venv/bin/python dump_sender.py wellfound --days 180 --all
venv/bin/python scanner.py --rescan   # re-check already-seen emails (e.g. after a filter change)
```

Equivalent, if you'd rather activate the env first:

```bash
cd cli
source venv/bin/activate
python compare.py
```

### GitHub listings watcher (`github_watcher.py`)

Shows new software internships in Canada from the GitHub listing
repos (SimplifyJobs, vanshb03, michelleokolie, negarprh), with apply links.
Needs network only — no Gmail or Ollama.

```bash
cd cli
venv/bin/python github_watcher.py            # the user's command: new postings since last run
venv/bin/python github_watcher.py --no-mark  # preview without marking anything seen
```

Use `--no-mark` when trying `github_watcher.py` yourself: a plain run records
every shown posting in `cli/.github_seen.json`, so the user would never see
those postings as new.

### One command: emails + GitHub, checked in Chrome (`/internships`)

From a Claude Code session started at the project root:

```text
/internships              # last 30 days, unread only
/internships --days 60    # extend the Gmail window
/internships --all        # include read emails
```

This runs the `verify-postings` flow below:

1. `plan_postings.py` reads the unread job-alert digests (read-only — the
   scanner never marks anything read) and lists, per email, which postings
   to skip and why and which to check, each with its own link.
2. `github_watcher.py` lists new GitHub postings (marks them seen).
3. Each posting to check is opened in Chrome via the claude-in-chrome MCP and
   judged ✅ Match / 🟡 Almost / ❌ Mismatch / ❓ Unsure.
4. `mark_read.py` marks an email read only if every one of its postings was
   skipped or ❌.
5. Summary: matches and almost-matches, plus a **To apply** list.

Needs `cli/profile.md` filled in and the Claude in Chrome extension
connected; Ollama isn't used. Defined in `.claude/skills/internships/SKILL.md`;
agents should not run it on their own initiative (it changes Gmail and
`.github_seen.json`).

### Term filter

`scanner.py`/`compare.py`/`plan_postings.py` (per digest listing) and `github_watcher.py`
drop a posting on term only if it explicitly says Winter or Fall
(`_EXCLUDED_SEASON_REGEX` in `scanner.py`) and not Summer 2027. Termless,
Spring and other-year postings pass; the Chrome pass below checks the real
term on the posting page.

### Verifying postings in Chrome (`verify-postings` skill)

`cli/.claude/skills/verify-postings/SKILL.md` runs `plan_postings.py` and
`github_watcher.py -o .github_new.json`, opens each posting to check in the
user's Chrome (claude-in-chrome MCP), and checks it against the filter and
`cli/profile.md` (gitignored; the user fills it in). The plan is
`cli/.postings_plan.json` (gitignored). The only Gmail change is
`.claude/skills/verify-postings/mark_read.py --failed <posting ids>`, which
marks read just the emails whose postings were all skipped or ❌; GitHub
verdicts change nothing.

When trying `plan_postings.py` yourself it is safe (read-only), but don't
run `mark_read.py` without `--dry-run`. `compare.py --apply` still exists
and still marks emails read on its own rules; the `/internships` flow no
longer uses it.

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
