---
name: gbrain-upgrade-runner
version: 0.1.0
description: |
  Executable runbook for gbrain version upgrades (Stream B Phase 5-8).
  Consumes watcher-briefing output, executes the canonical upgrade sequence
  with safety guards (dim parity check, pg_dump freshness, supervisor state),
  and propagates version pins downstream. Requires --confirm-destructive for
  Phase 2-6 destructive ops. Outputs JSON envelope to stdout + JSONL audit log.
triggers:
  - "gbrain upgrade runner"
  - "run the upgrade"
  - "execute gbrain migration"
  - "upgrade-gbrain"
tools:
  - terminal
requires_args: [briefing]
mutating: true
writes_to:
  - ~/.hermes/logs/gbrain-upgrade-runner.jsonl
expected_runtime_seconds: 1800
expected_max_rss_mb: 512
cites_decisions: [D5, D6, D7, D11, D27, D60, D72]
upstream_layer: ~/resources/local-agent-system/knowledge-base/checklist_gbrain-upgrade-v0.22.6-to-v0.33.0_20260512.md
---

# gbrain Upgrade Runner Skill

> **Runtime execution (per D27):** Fully-local Python implementation at `run.py`
> (sibling file). No external agent runtime in the loop. All execution via
> subprocess calls to gbrain CLI, bun, psql, launchctl.

## Contract

This skill guarantees:

- Every upgrade execution follows the canonical sequence from Stream B checklist
  Phase 5 (migration execution) through Phase 8 (post-migration validation).
- **Dim parity check** is the critical guard: `config.embedding_dimensions` must
  equal DB column dimension before any vector operations. If mismatch detected,
  the skill aborts with actionable remediation steps.
- **pg_dump freshness** is verified before Phase 6 destructive ops: the snapshot
  must be less than 30 minutes old or the operator must re-confirm.
- **Supervisor state** is checked before/after Phase 6: gbrain jobs supervisor
  must be stopped before upgrade, confirmed running after restart.
- **Operator gate**: `--confirm-destructive` flag REQUIRED for Phase 2-6 destructive
  ops (stop supervisor, run upgrade, apply migrations). Without it, the skill stops
  at Phase 1 end and prints "awaiting --confirm-destructive".
- **Rollback**: Three-tier rollback available on any verify failure:
  (1) per-version transaction abort during apply-migrations,
  (2) full pg_restore from snapshot (~10-20s),
  (3) code-only downgrade to previous version.
- **Idempotent** on the same briefing + installed version pair. Re-running against
  an already-upgraded system is a no-op with outcome "already-current".

## Phases

### Phase 0: Preflight

**Input:** `--briefing` path argument.
**Output:** Briefing parsed + validated; system state snapshot captured.
**Side effects:** None (read-only).
**Failure modes:** Briefing file missing/malformed → exit 2.

Steps:
1. Parse briefing YAML frontmatter (fire_id, severity, installed, upstream).
2. Verify gbrain is installed: `gbrain --version` returns current version.
3. Capture system state: supervisor status, DB connectivity, disk space.
4. Log preflight results to JSONL audit trail.

### Phase 1: Briefing Intake

**Input:** Parsed briefing from Phase 0.
**Output:** Structured upgrade plan extracted (patches to apply, schema changes, risks).
**Side effects:** None.
**Failure modes:** Severity F without migration plan → exit 1 with actionable error.

Steps:
1. Extract patch-by-patch plan from briefing (keep/retire/port decisions).
2. Extract schema migration plan if `schema_has_delta: true`.
3. Validate that a rollback path is documented in the briefing.
4. If severity F and no migration plan exists → abort with error.

### Phase 2: Stop Supervisor + Snapshot DB

**Input:** Upgrade plan from Phase 1.
**Output:** Supervisor stopped; pg_dump snapshot created at `/tmp/gbrain-pre-<version>.dump`.
**Side effects:** gbrain jobs supervisor stopped (jobs queue paused).
**Failure modes:** Supervisor already stopped → warn, continue. pg_dump fails → exit 1.

Steps:
1. Check supervisor state: `launchctl list | grep minions-supervisor`.
2. Stop supervisor if running: `launchctl unload ~/.hermes/launchd/local.hermes.minions-supervisor.plist`.
3. Create pg_dump snapshot: `pg_dump gbrain -F c > /tmp/gbrain-pre-<version>.dump`.
4. Verify snapshot: `pg_restore --list /tmp/gbrain-pre-<version>.dump` (exit 0 = valid).
5. Log snapshot path + size to audit trail.

### Phase 3: Canonical Upgrade

**Input:** pg_dump snapshot from Phase 2; patch decisions from Phase 1.
**Output:** gbrain upgraded to target version in global bun store.
**Side effects:** `~/.bun/install/global/node_modules/gbrain/` replaced with new version.
**Failure modes:** `gbrain upgrade` fails → exit 1; snapshot available for rollback.

Steps:
1. Run canonical upgrade command: `GBRAIN_NO_REEMBED=1 gbrain upgrade`.
   - This runs bun update + post-upgrade hooks + apply-migrations + initSchema.
   - GBRAIN_NO_REEMBED=1 is REQUIRED: CJK chunker re-embed prompt defaults to proceed
     when non-TTY; this env var prevents accidental full re-index.
2. Re-apply ported patches to `~/.bun/install/global/node_modules/gbrain/`.
3. Verify upgrade: `gbrain --version` returns target version.

### Phase 4: Verify

**Input:** Upgraded gbrain binary from Phase 3.
**Output:** Verification report (schema check, dim parity, doctor pass).
**Side effects:** None.
**Failure modes:** Dim mismatch → exit 1 with rollback commands. Doctor fails → exit 1.

Steps:
1. **Dim parity check** (critical guard):
   - Read config: `gbrain config get embedding_dimensions` (or equivalent).
   - Query DB column dim: `psql gbrain -c "SELECT atttypmod FROM pg_attribute WHERE attrelid='content_chunks'::regclass AND attname='embedding';"`
   - Compare: config dim must equal DB column dim. If mismatch → abort with rollback commands.
2. Run `gbrain doctor` — verify all checks pass.
3. Verify schema version: `psql gbrain -c "SELECT * FROM _gbrain_migrations ORDER BY version DESC LIMIT 1;"`.
4. Log verification results to audit trail.

### Phase 5: Restart Supervisor

**Input:** Verified upgrade from Phase 4.
**Output:** gbrain jobs supervisor running and processing jobs.
**Side effects:** Supervisor restarted; job queue active again.
**Failure modes:** Supervisor fails to start → exit 1 with rollback commands.

Steps:
1. Load supervisor plist: `launchctl load ~/.hermes/launchd/local.hermes.minions-supervisor.plist`.
2. Wait for supervisor to initialize: poll `launchctl list | grep minions-supervisor` until PID present.
3. Verify job processing: `gbrain jobs list --status active` or check recent job completions.
4. Log supervisor restart to audit trail.

### Phase 6: Smoke Query

**Input:** Running supervisor from Phase 5.
**Output:** Smoke test results (query retrieval, stats consistency).
**Side effects:** None.
**Failure modes:** Query returns empty/stale results → exit 1 with rollback commands.

Steps:
1. Run `gbrain stats` — compare page/chunk counts against pre-migration baseline.
2. Run 3-5 known-good queries: `gbrain query "known search term"` — verify retrieval works.
3. Verify embedding dimension consistency: `gbrain query "test" --debug` (check vector dim in output).
4. Log smoke test results to audit trail.

### Phase 7: Propagate

**Input:** Verified upgrade from Phase 6.
**Output:** CLAUDE.md version pin updated; manifest of propagation edits printed.
**Side effects:** `~/resources/local-agent-system/CLAUDE.md` updated with new version pin.
**Failure modes:** CLAUDE.md not found → warn, continue (print manifest for manual edit).

Steps:
1. Update CLAUDE.md gbrain version reference (e.g., "0.22.6 pinned at be8fffad" → "0.33.0 pinned at 17b190e").
2. Print manifest of other propagation edits needed (model-cookbook.md, gbrain-patch-verify, roles.yaml).
3. Log propagation results to audit trail.

### Phase 8: Validate

**Input:** All previous phases complete.
**Output:** Final validation report; skill exit 0 (success) or non-zero (failure).
**Side effects:** None.

Steps:
1. Confirm `gbrain stats` counts match pre-migration baseline (allowing for migration deltas).
2. Confirm 3-5 known-good queries return valid results with no quality regression.
3. Verify supervisor is processing jobs (check recent cron-fire or manual job submission).
4. Write final JSON envelope to stdout with outcome "success".

## Safety Guards

### Dim Parity Check (Phase 4)
- **What:** `config.embedding_dimensions` must equal DB column dimension.
- **Why:** Mismatch causes silent vector incompatibility — existing embeddings become
  unreadable, queries return empty or garbage results.
- **How:** Compare config value against `pg_attribute.atttypmod` for the embedding column.
- **Abort action:** Print rollback commands (pg_restore + downgrade) and exit 1.

### pg_dump Freshness Check (Phase 2)
- **What:** Snapshot must be less than 30 minutes old.
- **Why:** Stale snapshots miss recent changes; rollback would lose data.
- **How:** Check file modification time of the dump file.
- **Abort action:** Require operator re-confirmation or create fresh snapshot.

### Supervisor State Check (Phase 2 + Phase 5)
- **What:** Supervisor must be stopped before upgrade, running after restart.
- **Why:** Running supervisor holds DB connections and file locks during upgrade.
- **How:** `launchctl list | grep minions-supervisor` — check for PID presence.
- **Abort action:** If supervisor still running during Phase 3, stop it and retry.

### Operator Gate (Phase 2)
- **What:** `--confirm-destructive` flag required for Phase 2+.
- **Why:** Stopping supervisor + upgrading code is destructive; accidental execution
  pauses the entire gbrain job queue.
- **How:** Argparse flag; without it, skill stops at Phase 1 end.
- **Abort action:** Print "awaiting --confirm-destructive" with explanation and exit 3.

## Rollback

Three-tier rollback available on any Phase 4+ failure:

### Tier 1: Transaction Abort (during apply-migrations)
- If `gbrain upgrade` fails during migration phase, the per-version transaction
  should auto-abort. Verify with `psql gbrain -c "SELECT * FROM _gbrain_migrations ORDER BY version DESC LIMIT 1;"`.
- If migration is partially applied: `gbrain apply-migrations --rollback` (if supported).

### Tier 2: pg_restore from Snapshot
```bash
pg_restore -d gbrain -c /tmp/gbrain-pre-<version>.dump
```
- Drops all tables, restores from snapshot (~10-20s).
- Requires supervisor to be stopped during restore.

### Tier 3: Code Downgrade
```bash
bun install --global gbrain@0.22.6
# Re-apply original patches to ~/.bun/install/global/node_modules/gbrain/
```

## Examples

### Inaugural Fire (Phase 6 today)

```bash
python3 ~/gbrain/skills/gbrain-upgrade-runner/run.py \
  --briefing ~/.claude/knowledge-base/gbrain_upgrade_briefings/20260512-v3a.md \
  --confirm-destructive \
  --auto-rollback-on-fail
```

Expected flow: Phase 0 (preflight) → Phase 1 (briefing intake) → Phase 2 (stop supervisor + pg_dump)
→ Phase 3 (canonical upgrade: `GBRAIN_NO_REEMBED=1 gbrain upgrade`) → Phase 4 (verify dim parity + doctor)
→ Phase 5 (restart supervisor) → Phase 6 (smoke query) → Phase 7 (propagate CLAUDE.md pin)
→ Phase 8 (validate stats + queries).

### Dry Run (no destructive ops)

```bash
python3 ~/gbrain/skills/gbrain-upgrade-runner/run.py \
  --briefing ~/.claude/knowledge-base/gbrain_upgrade_briefings/20260512-v3a.md \
  --dry-run
```

Expected flow: Phase 0 (preflight) → Phase 1 (briefing intake) → print "DRY RUN — would execute Phases 2-8" + summary of actions.

### Without Operator Gate (safe mode)

```bash
python3 ~/gbrain/skills/gbrain-upgrade-runner/run.py \
  --briefing ~/.claude/knowledge-base/gbrain_upgrade_briefings/20260512-v3a.md
```

Expected flow: Phase 0 (preflight) → Phase 1 (briefing intake) → STOP.
Prints: "awaiting --confirm-destructive for Phase 2+ destructive operations".

## I/O Contract

### Input
- `--briefing <path>`: Path to watcher-briefing YAML frontmatter file.
- `--confirm-destructive`: Required flag for Phase 2+ destructive operations.
- `--auto-rollback-on-fail`: Execute rollback commands automatically on verify failure (Phase 4+).
- `--dry-run`: Print intended phases and actions without executing.
- `--no-supervisor`: Skip launchctl supervisor start/stop (for testing).

### Output
- **stdout:** JSON envelope with phase results, outcome, duration, error (if any).
- **stderr:** Human-readable progress messages.
- **Audit log:** `~/.hermes/logs/gbrain-upgrade-runner.jsonl` — one entry per phase.

### JSON Envelope Shape
```json
{
  "version": "1.0",
  "fire_id": "20260512-v3a",
  "installed_before": "be8fffad",
  "target_version": "17b190e",
  "outcome": "success|failed|rollback-executed|already-current|operator-gate-required",
  "phases_executed": ["phase0_preflight", "phase1_briefing_intake"],
  "phases_skipped": ["phase2_stop_supervisor", ...],
  "duration_ms": 45000,
  "snapshot_path": "/tmp/gbrain-pre-v33-20260512.dump",
  "rollback_commands": ["pg_restore -d gbrain -c /tmp/gbrain-pre-v33-20260512.dump", ...],
  "error": null,
  "dim_parity_ok": true,
  "doctor_pass": true,
  "smoke_queries_passed": 3
}
```

### Exit Codes
- **0:** Success — upgrade complete, all verifications pass.
- **1:** Phase failure — one or more phases failed (rollback may have been attempted).
- **2:** Usage error — missing required args, malformed briefing.
- **3:** Operator gate required — `--confirm-destructive` not provided for destructive ops.
- **4:** Rollback executed — failure occurred and rollback was successful (auto or manual).
- **5:** Rollback failed — failure occurred and rollback also failed; manual intervention required.
