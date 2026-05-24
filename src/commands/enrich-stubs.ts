/**
 * S196 Phase 5 — `gbrain enrich-stubs` CLI (backward-mode + on-demand).
 *
 * Per CDD S195-enrich-stubs-phase.md §1.11 (backward cadence).
 *
 * Forward mode runs via dream-cycle (cycle.ts dispatch). This CLI is for:
 *   - Backward bulk: one-time pass over ~20K corrupted compiled_truth pages
 *     post Gate B clearance (subscription-billed; weekend run)
 *   - On-demand single-page enrichment (debugging + operator-triggered fixes)
 *
 * Usage:
 *   gbrain enrich-stubs <slug>                              — single page (forward-shape semantics)
 *   gbrain enrich-stubs --slug-file <path>                  — batch from file
 *   gbrain enrich-stubs --slugs <s1,s2,s3>                  — batch from CSV
 *   gbrain enrich-stubs --backward --since 2026-05-23       — bulk over canonical-enrich-stale pages
 *   Flags:
 *     --model <name>     mock | claude-sonnet-4-6 | qwen3.6-35b (default: mock until Gate B)
 *     --mode <m>         forward | backward (default: forward)
 *     --dry-run          no writeback, no cost-log insert
 *     --max-pages N      cap (default 50 for forward, no cap for backward)
 *     --signal-score N   pass-through to skill (default 8)
 *     --json             emit JSON summary instead of human-readable
 */

import { spawn } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { homedir } from 'node:os';
import type { BrainEngine } from '../core/engine.ts';

const SKILL_PATH = join(homedir(), 'gbrain', 'skills', 'enrich-stubs', 'run.py');

function flagValue(args: string[], name: string): string | undefined {
  const i = args.indexOf(name);
  if (i === -1) return undefined;
  return args[i + 1];
}

function flagPresent(args: string[], name: string): boolean {
  return args.includes(name);
}

function printUsage(): void {
  process.stdout.write(`Usage: gbrain enrich-stubs <slug> | --slug-file <path> | --slugs <csv> | --backward --since <iso>

Per CDD S195-enrich-stubs-phase.md section 1.11. Forward mode runs via
dream-cycle automatically; this CLI is for backward-bulk + on-demand.

Options:
  --model <name>     mock | claude-sonnet-4-6 | qwen3.6-35b (default: mock)
  --mode <m>         forward | backward (default: forward)
  --dry-run          no writeback, no cost-log insert
  --max-pages N      cap (default 50 forward / no cap backward)
  --signal-score N   pass-through to skill (default 8)
  --json             JSON summary instead of human-readable
  --backward --since <iso>  bulk over pages with canonical-enrich-stale state

Notes:
  - --model claude-sonnet-4-6 + non-mock requires Gate B operator clearance
    (see ~/gbrain/skills/enrich-stubs/llm_client.py RealLLMClient stub)
  - For backward mode, --slug-file or --slugs is required (operator-curated
    list); --since alone is reserved for Phase 8 bulk-script work
`);
}

async function collectSlugs(args: string[]): Promise<string[] | { error: string }> {
  const slugs: string[] = [];
  const slugFile = flagValue(args, '--slug-file');
  const slugsCsv = flagValue(args, '--slugs');
  const positional = args.filter((a, i) => !a.startsWith('-')
    && (i === 0 || !args[i - 1]?.startsWith('--')));

  if (positional.length > 0) {
    slugs.push(...positional);
  }
  if (slugsCsv) {
    slugs.push(...slugsCsv.split(',').map(s => s.trim()).filter(Boolean));
  }
  if (slugFile) {
    if (!existsSync(slugFile)) {
      return { error: `--slug-file path does not exist: ${slugFile}` };
    }
    const lines = readFileSync(slugFile, 'utf-8').split('\n')
      .map(l => l.trim())
      .filter(l => l && !l.startsWith('#'));
    slugs.push(...lines);
  }
  return slugs;
}

function spawnSkillBatch(
  slugs: string[],
  model: string,
  mode: string,
  signalScore: number,
  dryRun: boolean,
  maxPages: number,
): Promise<{ stdout: string; stderr: string; code: number | null }> {
  return new Promise((resolve) => {
    const args = [
      SKILL_PATH,
      '--model', model,
      '--mode', mode,
      '--signal-score', String(signalScore),
      '--max-pages', String(maxPages),
    ];
    if (dryRun) args.push('--dry-run');
    // Pass slugs via --slug-file (use stdin shim via tmp file if many)
    if (slugs.length === 1) {
      args.push('--slug', slugs[0]);
    } else {
      // Write to a tmp file
      const tmpPath = join('/tmp', `enrich-stubs-batch-${Date.now()}.txt`);
      const fs = require('node:fs');
      fs.writeFileSync(tmpPath, slugs.join('\n'));
      args.push('--slug-file', tmpPath);
    }
    const child = spawn('python3', args);
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (chunk) => { stdout += chunk.toString(); });
    child.stderr.on('data', (chunk) => { stderr += chunk.toString(); });
    child.on('error', (err) => {
      resolve({ stdout, stderr: stderr + `\n[spawn-err] ${err.message}`, code: null });
    });
    child.on('close', (code) => {
      resolve({ stdout, stderr, code });
    });
  });
}

export async function runEnrichStubs(_engine: BrainEngine, args: string[]): Promise<void> {
  if (args.length === 0 || args[0] === '--help' || args[0] === '-h') {
    printUsage();
    return;
  }

  if (!existsSync(SKILL_PATH)) {
    process.stderr.write(`enrich-stubs skill not found at ${SKILL_PATH}\n`);
    process.exit(1);
  }

  const model = flagValue(args, '--model') ?? 'mock';
  const mode = flagValue(args, '--mode') ?? 'forward';
  const dryRun = flagPresent(args, '--dry-run');
  const json = flagPresent(args, '--json');
  const signalScore = Number(flagValue(args, '--signal-score') ?? '8');
  const maxPagesDefault = mode === 'backward' ? 10000 : 50;
  const maxPages = Number(flagValue(args, '--max-pages') ?? String(maxPagesDefault));

  const slugsOrErr = await collectSlugs(args);
  if ('error' in slugsOrErr) {
    process.stderr.write(`${slugsOrErr.error}\n`);
    process.exit(1);
  }
  const slugs = slugsOrErr;
  if (slugs.length === 0) {
    process.stderr.write('No slugs provided. Use <slug> | --slug-file | --slugs.\n');
    process.exit(1);
  }
  if (mode !== 'forward' && mode !== 'backward') {
    process.stderr.write(`Invalid --mode: ${mode}. Must be forward|backward.\n`);
    process.exit(1);
  }

  process.stderr.write(`[enrich-stubs] dispatching ${slugs.length} slug(s); model=${model}; mode=${mode}; dry-run=${dryRun}\n`);

  const result = await spawnSkillBatch(slugs, model, mode, signalScore, dryRun, maxPages);
  process.stdout.write(result.stdout);
  if (result.stderr) {
    process.stderr.write(result.stderr);
  }
  if (result.code !== 0) {
    process.exit(result.code ?? 1);
  }
  // For human mode, render brief summary at end (stdout already has JSON)
  if (!json) {
    process.stderr.write('[enrich-stubs] done\n');
  }
}
