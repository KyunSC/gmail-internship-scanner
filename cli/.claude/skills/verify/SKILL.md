---
name: verify
description: How to run and drive the cli/ scripts to verify a change (github_watcher.py recipe; scanner/compare need Gmail + Ollama).
---

# Verifying cli/ changes

Always run from `cli/` with `venv/bin/python` (see AGENTS.md).

## github_watcher.py (network only)

- Never let a verification run write the user's `cli/.github_seen.json`.
  Use `--no-mark` against the real dir; `shasum .github_seen.json` before/after to prove it.
- Live preview: `venv/bin/python github_watcher.py --no-mark --show-all [--include-termless] -o <scratch>/x.json`
  — `--show-all` bypasses ETags so every source is re-fetched; the JSON export carries `source`, `terms`, `category` for analysis.
- Marking / ETag / state probes: copy `scanner.py` + `github_watcher.py` into a scratch dir
  and run them there with `cli/venv/bin/python` — `STATE_PATH` is next to the script, so state stays isolated.
  Cycle worth driving: fresh run → rerun (expect 0 new, all `unchanged`) → `--include-termless` (filters changed → refetch).
- Per-source counts (to see cross-repo dedupe): in the scratch copy,
  `python -c "import sys,github_watcher as g; g.SOURCES=[s for s in g.SOURCES if s['name']=='negar']; sys.argv=[...]; g.main()"`.
  Sum of per-source counts minus combined count = rows deduped.
- Upgrade path: `git show HEAD:cli/github_watcher.py` into another scratch dir, run it to write old-format state,
  then run the new version on that state with `--no-mark`.
- Strip colours for captures: `| sed 's/\x1b\[[0-9;]*m//g'`.
