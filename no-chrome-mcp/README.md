# no-chrome-mcp

A frozen copy of the scanner from before the Chrome (`/internships`) flow
(commit `3d01128`). It runs on its own, with no Chrome extension:

- Ollama classifies emails, and `compare.py --apply` / `scanner.py --clean-inbox --apply`
  mark non-internships read.
- Strict term filter: a digest listing is kept only if it explicitly says Summer 2027.
- `github_watcher.py` shows Summer 2027 postings; `--include-termless` adds ones with no term.

```bash
cd no-chrome-mcp
venv/bin/python compare.py --apply
venv/bin/python scanner.py
venv/bin/python github_watcher.py
```

`venv`, `credentials.json` and `token.json` are symlinks to `../cli/`, so this
copy shares the Gmail login and Python packages. Its scan cache
(`.last_scan.json`) and GitHub seen list (`.github_seen.json`) are its own,
so the first GitHub run shows every current posting as new.

Changes made in `cli/` don't reach this copy.
