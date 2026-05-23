# linkedin-bootstrap — invocation contract

**Per D27.** This skill's implementation is fully-local Python at `run.py`.
**Claude Code orchestration at runtime is forbidden** — tool-use returns
enter Claude's Anthropic-hosted context, violating D18 T1 invariant.

## Three-mode invocation contract

### 1. Fixture mode (Claude-visible iteration; tests use this path)

```sh
cd ~/gbrain/skills/linkedin-bootstrap
uv run run.py \
  --source ./test-fixtures \
  --brain-root /tmp/test-brain \
  --usermd-path /tmp/test-user.md \
  --skip-preflight \
  --dry-run
```

- Synthetic CSV fixtures at `test-fixtures/` only.
- `--brain-root /tmp/test-brain` keeps writes out of `~/brain/`.
- `--skip-preflight` is for tests where the operator routing-configs
  aren't relevant.

### 2. Dry-run on real data (user-only inspection)

```sh
skill-with-timeout linkedin-bootstrap 1800 -- \
  uv run run.py --dry-run
```

- Reads real `~/resources/local-agent-system/data/LinkedIn/...`.
- Writes ZERO brain pages, ZERO USER.md sections, NO git commits.
- Emits audit JSON at `audit/<run-id>-dryrun.json` + JSONL run record
  at `~/.hermes/logs/runs.jsonl` (counters only — no CSV body).
- Use the audit to inform the Gate B operator decision.

### 3. Live (writes to ~/brain/, ~/.hermes/USER.md, commits, pushes)

```sh
skill-with-timeout linkedin-bootstrap 1800 -- \
  uv run run.py
```

- Reads real source; writes real brain.
- Phase 9 commits + pushes via `_shared/git_commit.py:commit_and_push_brain`
  (mirrors apple-contacts-bootstrap pattern).
- T1-leak pre-flight enforced by the shared helper.

## Per-phase resume

If a phase fails mid-run, resume via `--phase N`:

```sh
uv run run.py --phase 5  # Skip phases 1-4; start at activity timeline
```

Each phase has its own checksum-skip + audit log entry; resuming is
safe.

## Privacy invariants

- `messages.csv` (LinkedIn DMs) is NEVER opened. Hard-coded in
  `run.py:SKIP_LIST`. Test `test_phase8_skip_invariant_no_messages_read`
  enforces.
- `Reactions.csv` is NEVER opened (operator decision S191-default-2).
- All CSV body content stays on local disk; no body text enters
  Claude / Hermes / external API context.
- Phase 7 writes to `~/.hermes/USER.md` (T1 identity-doc); never
  routes preference data to `~/brain/` corpus.

## Re-run safety

The skill is idempotent — re-running on unchanged source = no-op
(counters[idempotent_skip] > 0). To force re-process a phase:
delete the relevant `linkedin_*_csum:` frontmatter field from the
operator self-page (Phase 1) or the per-stub-page (Phases 2-6).
