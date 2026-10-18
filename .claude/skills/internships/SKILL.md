---
name: internships
description: Go through unread internship emails and new GitHub postings, check each posting in Chrome (claude-in-chrome MCP) against the filter and cli/profile.md, mark read only the emails in which no posting works, and summarize the jobs that match or almost match.
argument-hint: "[--days N] [--all]"
disable-model-invocation: true
---

# /internships

Shortcut for the `verify-postings` skill, so it works from the project root.

1. Read `cli/.claude/skills/verify-postings/SKILL.md` and follow it exactly,
   pre-flight and guardrails included.
2. Run every script from `cli/` with `venv/bin/python` (see AGENTS.md).
3. Arguments: $ARGUMENTS
   Pass `--days N` and `--all` through to `plan_postings.py` only. With no
   arguments, use the defaults (last 30 days, unread only).
