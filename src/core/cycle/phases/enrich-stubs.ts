/**
 * S196 Phase 5 — `enrich_stubs` dream-cycle phase.
 *
 * Per CDD S195-enrich-stubs-phase.md §1.11 (forward + backward cadence).
 *
 * Forward mode: dream-cycle nightly invocation. Selects up to
 * BATCH_SIZE_FORWARD stub pages whose signal_score >= THRESHOLD_ENRICH;
 * for each, spawns the Python skill (~/gbrain/skills/enrich-stubs/run.py)
 * which assembles the substrate bundle, runs privacy assertion, calls
 * the configured LLM client (Mock or Real), post-processes per §1.8, and
 * writes back a markdown sidecar.
 *
 * Ships with `enrich_stubs.enabled = false` default (safe). Operator
 * explicitly enables via `gbrain config set enrich_stubs.enabled true`
 * AFTER Gate B clearance unlocks the Real LLM client.
 *
 * Backward mode: invoked via `gbrain enrich-stubs --backward` CLI
 * (separate file: src/commands/enrich-stubs.ts). Not part of dream cycle.
 *
 * Per CDD §I10: per-page failures don't crash the batch; each subprocess
 * is isolated; the phase reports total/success/abstain/failed/skipped counts.
 */

import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { homedir } from 'node:os';
import type { BrainEngine } from '../../engine.ts';
import type { PhaseResult } from '../../cycle.ts';

const ENRICH_STUBS_SKILL_PATH = join(homedir(), 'gbrain', 'skills', 'enrich-stubs', 'run.py');

export interface EnrichStubsPhaseOpts {
  dryRun?: boolean;
  /** Override config — useful for tests. */
  enabled?: boolean;
  threshold?: number;
  batchSize?: number;
  model?: string;
  /** Override the brain-path (mostly for tests). */
  brainPath?: string;
}

interface SubprocessResult {
  slug: string;
  status: 'success' | 'abstain' | 'failed' | 'skipped';
  cost_usd?: number;
  wall_time_ms?: number;
  error?: string;
}

interface SkillBatchSummary {
  total: number;
  success: number;
  abstain: number;
  failed: number;
  skipped: number;
  results: SubprocessResult[];
}

/**
 * Read DB-plane config with fallback default. Mirrors the engine-aware
 * pattern used by the A.4 lint phase patch (cycle.ts engine-threaded
 * config reads).
 */
async function readConfig<T extends string | number | boolean>(
  engine: BrainEngine,
  key: string,
  defaultValue: T,
): Promise<T> {
  try {
    const value = await engine.getConfig?.(key);
    if (value === undefined || value === null) return defaultValue;
    if (typeof defaultValue === 'boolean') {
      return ((value === 'true' || value === '1' || value === true) as unknown) as T;
    }
    if (typeof defaultValue === 'number') {
      const n = Number(value);
      return (Number.isFinite(n) ? n : defaultValue) as unknown as T;
    }
    return value as unknown as T;
  } catch {
    return defaultValue;
  }
}

/**
 * Select candidate stub pages for this batch.
 *
 * v1 simplification: query pages by type-eligible + not-recently-enriched +
 * deleted_at IS NULL. The TS signal-score utility is NOT re-implemented in
 * this phase yet — we trust the Python skill to compute its own signal score
 * via its own queries (per CDD §1.2 the bundler queries postgres anyway).
 * For v1 we order by created_at DESC NULLS FIRST to surface unread stubs.
 *
 * Phase 5.1 (future) will compute signal_score in-phase via the TS module
 * and pre-filter to >= threshold; for now the Python skill handles its own
 * gating.
 */
async function selectCandidateStubs(
  engine: BrainEngine,
  batchSize: number,
): Promise<string[]> {
  // Use raw query — types.ts Page interface doesn't expose last_enriched_at,
  // and we want to filter on the new v81 column.
  const rows = await engine.executeRaw<{ slug: string }>(`
    SELECT slug FROM pages
    WHERE type IN ('person', 'institution', 'concept', 'idea', 'meeting')
      AND deleted_at IS NULL
      AND (last_enriched_at IS NULL OR last_enriched_at < updated_at)
      AND (last_enrichment_signal_score IS NULL OR last_enrichment_signal_score >= 0)
    ORDER BY last_enrichment_signal_score DESC NULLS LAST,
             last_enriched_at NULLS FIRST,
             created_at DESC
    LIMIT $1
  `, [batchSize]);
  return rows.map(r => r.slug);
}

/**
 * Spawn the Python skill subprocess for one page and parse its JSON output.
 *
 * Per CDD §I10: failures here are isolated; the calling loop continues.
 */
function runSkillSubprocess(
  slug: string,
  model: string,
  dryRun: boolean,
  skillPath: string,
): Promise<SubprocessResult> {
  return new Promise((resolve) => {
    const args = [
      skillPath,
      '--slug', slug,
      '--model', model,
      '--mode', 'forward',
    ];
    if (dryRun) args.push('--dry-run');
    const child = spawn('python3', args, {
      timeout: 5 * 60 * 1000, // 5 min per page
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (chunk) => { stdout += chunk.toString(); });
    child.stderr.on('data', (chunk) => { stderr += chunk.toString(); });
    child.on('error', (err) => {
      resolve({ slug, status: 'failed', error: `subprocess_error: ${err.message}` });
    });
    child.on('close', (code) => {
      if (code === null) {
        resolve({ slug, status: 'failed', error: 'subprocess_killed (likely timeout)' });
        return;
      }
      // run.py prints only the JSON summary to stdout; logging goes to stderr.
      // Parse from the FIRST `{` (outer object) through the LAST matching `}`.
      try {
        const trimmed = stdout.trim();
        const jsonStart = trimmed.indexOf('{');
        if (jsonStart === -1) {
          resolve({ slug, status: 'failed', error: `no_json_in_stdout; stderr_tail=${stderr.slice(-200)}` });
          return;
        }
        // Slice from first `{` to end; JSON.parse handles trailing newlines.
        const summary = JSON.parse(trimmed.slice(jsonStart)) as SkillBatchSummary;
        const result = summary.results?.[0];
        if (!result) {
          resolve({ slug, status: 'failed', error: `empty_results_in_summary; code=${code}` });
          return;
        }
        resolve({
          slug: result.slug,
          status: result.status,
          cost_usd: result.cost_usd,
          wall_time_ms: result.wall_time_ms,
          error: result.error,
        });
      } catch (parseErr) {
        resolve({
          slug,
          status: 'failed',
          error: `json_parse: ${parseErr instanceof Error ? parseErr.message : String(parseErr)}; stdout_tail=${stdout.slice(-200)}`,
        });
      }
    });
  });
}

export async function runPhaseEnrichStubs(
  engine: BrainEngine,
  opts: EnrichStubsPhaseOpts = {},
): Promise<PhaseResult> {
  const dryRun = opts.dryRun === true;

  // Config gate — default OFF per CDD; operator must opt in
  const enabled = opts.enabled ?? await readConfig<boolean>(engine, 'enrich_stubs.enabled', false);
  if (!enabled) {
    return {
      phase: 'enrich_stubs' as never, // CyclePhase enum extension lands in cycle.ts edit
      status: 'skipped',
      duration_ms: 0,
      summary: 'enrich_stubs.enabled=false (default; explicit opt-in required AFTER Gate B clearance)',
      details: { enabled: false, gate_b_required: true },
    };
  }

  const threshold = opts.threshold ?? await readConfig<number>(engine, 'enrich_stubs.threshold', 8);
  const batchSize = opts.batchSize ?? await readConfig<number>(engine, 'enrich_stubs.batch_size_forward', 50);
  const model = opts.model ?? await readConfig<string>(engine, 'enrich_stubs.model_t23', 'mock');

  // Pre-flight: skill present?
  if (!existsSync(ENRICH_STUBS_SKILL_PATH)) {
    return {
      phase: 'enrich_stubs' as never,
      status: 'fail',
      duration_ms: 0,
      summary: `enrich_stubs skill not found at ${ENRICH_STUBS_SKILL_PATH}`,
      details: { skill_path: ENRICH_STUBS_SKILL_PATH },
      error: {
        class: 'FilesystemError',
        code: 'ENOENT',
        message: 'enrich-stubs skill missing',
      },
    };
  }

  const started = Date.now();
  const slugs = await selectCandidateStubs(engine, batchSize);
  if (slugs.length === 0) {
    return {
      phase: 'enrich_stubs' as never,
      status: 'ok',
      duration_ms: Date.now() - started,
      summary: '0 stub pages qualified for enrichment this batch',
      details: { threshold, batchSize, candidates: 0, model, dryRun },
    };
  }

  // Per-page subprocess; sequential for v1 (LM Studio single-occupant per call;
  // parallelization later)
  const results: SubprocessResult[] = [];
  for (const slug of slugs) {
    const r = await runSkillSubprocess(slug, model, dryRun, ENRICH_STUBS_SKILL_PATH);
    results.push(r);
  }

  const counts = {
    total: results.length,
    success: results.filter(r => r.status === 'success').length,
    abstain: results.filter(r => r.status === 'abstain').length,
    failed: results.filter(r => r.status === 'failed').length,
    skipped: results.filter(r => r.status === 'skipped').length,
  };
  const totalCost = results.reduce((acc, r) => acc + (r.cost_usd ?? 0), 0);

  // Status: warn if any failed, ok otherwise (CDD §I10 failure isolation)
  const status = counts.failed > 0 ? 'warn' : 'ok';

  return {
    phase: 'enrich_stubs' as never,
    status,
    duration_ms: Date.now() - started,
    summary: `${counts.success}/${counts.total} enriched (${counts.abstain} abstain, ${counts.failed} fail, ${counts.skipped} skip); cost=$${totalCost.toFixed(4)}`,
    details: {
      ...counts,
      threshold,
      batchSize,
      model,
      dryRun,
      total_cost_usd: totalCost,
      first_5_results: results.slice(0, 5),
    },
  };
}
