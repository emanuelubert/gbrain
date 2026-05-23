# linkedin-deepen — D27 invocation contract

Sibling skill to `linkedin-bootstrap`. Runs AFTER bootstrap to deepen
existing pages with the rich textual data sitting in the LinkedIn
export (Profile.Summary, Positions.Description, Education.Notes,
Connections per-row, Endorsements bidirectional, Invitations, Follows,
Comments + Shares operator-voice).

## Three invocation modes (per D27)

### 1. Fixture mode (Claude-visible iteration; no real data)

```bash
~/.hermes/hermes-agent/venv/bin/python -m pytest \
    ~/gbrain/skills/linkedin-deepen/test_linkedin_deepen.py -v
```

Uses synthetic `test-fixtures/` data only. Test brain is a `tmp_path`
copy of `test-fixtures/test-brain/`. Safe to run in Claude context —
no real export content leaks.

### 2. Dry-run on real data (no writes, summary counts only)

```bash
cd ~/gbrain/skills/linkedin-deepen && \
~/.hermes/hermes-agent/venv/bin/python run.py --dry-run --run-id S191-deepen-dryrun
```

Reads real export; emits per-phase counters to stdout + audit log. No
brain writes, no git ops. Output is summary-only per D27.

### 3. Live (writes to ~/brain/, commits, pushes)

```bash
# Deterministic mode (fast — no LLM):
cd ~/gbrain/skills/linkedin-deepen && \
~/.hermes/hermes-agent/venv/bin/python run.py --run-id S191-deepen-1

# LLM-prose-local mode (~2-4h compute on qwen3.6-35b):
cd ~/gbrain/skills/linkedin-deepen && \
~/.hermes/hermes-agent/venv/bin/python run.py --llm-prose-local --run-id S191-deepen-llm-1
```

`--llm-prose` (Anthropic API) is intentionally REJECTED with a clear
D27 violation message. Use `--llm-prose-local` for prose composition.

## What this skill does NOT do

- Does NOT create new pages (`pages_created` counter is structurally bounded to 0).
- Does NOT call external APIs (no requests, no Anthropic, no LinkedIn API).
- Does NOT open `messages.csv` / `Reactions.csv` / 8 other SKIP_LIST files.
- Does NOT cross-link Comments/Shares to counterparties (operator scope lock
  S191-D119+1 — avoids regex-name-match false positives).
- Does NOT overwrite user-authored body content above skill-owned sections.

## Operator scope decisions (locked S191-D119+1)

- LLM-prose mode: local-LM-only (qwen3.6-35b via LM Studio).
- Comments cross-link: SKIP.
- Member_Follows name-match: ENABLED.

See: `~/resources/local-agent-system/knowledge-base/cdd-contracts/S191-linkedin-deepen.md`
