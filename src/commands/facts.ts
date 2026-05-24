/**
 * S195 Phase 1 (enrich_stubs prerequisite) — `gbrain facts` CLI.
 *
 * Per S195-enrich-stubs-phase.md §1.10 (Layer 2 operator-correction
 * persistence). Required for the enrich_stubs phase §I8 invariant: operator
 * corrections (e.g. "operator no longer doing CSOL Academy") must persist
 * across re-enrichment via facts.valid_until tags.
 *
 * Subcommands:
 *   facts add --entity <slug> --claim "<text>" [...]
 *                                          Insert a new fact attributed to the operator
 *   facts expire <id> [--reason "..."]      Mark a fact expired (UPDATE expired_at = now())
 *   facts list [--entity <slug>] [--include-expired] [--json]
 *                                          List active (or all) facts; default active-only
 *
 * Underlying primitives:
 *   engine.insertFact(NewFact, ctx)         — facts table insert
 *   engine.expireFact(id, opts)             — facts table expire
 *   engine.listFactsByEntity(srcId, slug)   — list by entity
 *   engine.listFactsSince(srcId, ts)        — list-all fallback (when --entity unset)
 *
 * Tier: 2 (DB write; gates downstream enrich_stubs implementation).
 * CDD: ~/resources/local-agent-system/knowledge-base/cdd-contracts/S195-enrich-stubs-phase.md
 */

import type {
  BrainEngine,
  NewFact,
  FactKind,
  FactVisibility,
  FactRow,
} from '../core/engine.ts';
import { ALL_FACT_KINDS } from '../core/engine.ts';

const DEFAULT_SOURCE = 'cli:facts';

// --- Flag helpers (consistent with takes.ts patterns) ---

function flagValue(args: string[], name: string): string | undefined {
  const i = args.indexOf(name);
  if (i === -1) return undefined;
  return args[i + 1];
}

function flagPresent(args: string[], name: string): boolean {
  return args.includes(name);
}

function parseISODate(raw: string | undefined): Date | undefined {
  if (!raw) return undefined;
  const lower = raw.toLowerCase();
  if (lower === 'now') return new Date();
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) {
    process.stderr.write(`Invalid date: "${raw}". Use ISO 8601 (e.g. 2026-05-24T12:00:00Z) or "now".\n`);
    process.exit(1);
  }
  return d;
}

function isFactKind(s: string | undefined): s is FactKind {
  return !!s && (ALL_FACT_KINDS as readonly string[]).includes(s);
}

function isVisibility(s: string | undefined): s is FactVisibility {
  return s === 'private' || s === 'world';
}

// --- Source-row guard ---
// `facts.source_id` is FK to sources(id) DEFAULT 'default'. The 'default'
// source row is seeded by initSchema. New custom source_id values would
// need a sources-table insert; v1 of this CLI uses the default source and
// records operator-attribution via the `source` column ('operator' vs
// 'extract_facts' etc.), NOT via source_id. Per CDD §1.10 invariant.

async function resolveSourceId(_engine: BrainEngine, override?: string): Promise<string> {
  if (override) return override;
  return 'default';
}

// --- cmdAdd ---

async function cmdAdd(engine: BrainEngine, args: string[]): Promise<void> {
  const entity = flagValue(args, '--entity');
  const claim = flagValue(args, '--claim');
  if (!claim) {
    process.stderr.write('Error: --claim is required.\n');
    printAddUsage();
    process.exit(1);
  }
  const kindRaw = flagValue(args, '--kind');
  const kind: FactKind | undefined = kindRaw && isFactKind(kindRaw)
    ? kindRaw
    : kindRaw ? (process.stderr.write(`Invalid --kind "${kindRaw}". Must be one of: ${ALL_FACT_KINDS.join(', ')}\n`), process.exit(1)) as never
              : undefined;
  const visRaw = flagValue(args, '--visibility');
  const visibility: FactVisibility | undefined = visRaw && isVisibility(visRaw)
    ? visRaw
    : visRaw ? (process.stderr.write(`Invalid --visibility "${visRaw}". Must be 'private' or 'world'.\n`), process.exit(1)) as never
             : undefined;
  const validFrom = parseISODate(flagValue(args, '--valid-from'));
  const validUntil = parseISODate(flagValue(args, '--valid-until'));
  const context = flagValue(args, '--context');
  const sourceLabel = flagValue(args, '--source') ?? 'operator';
  const sourceId = await resolveSourceId(engine, flagValue(args, '--source-id'));
  const sessionId = flagValue(args, '--session') ?? null;
  const confidenceRaw = flagValue(args, '--confidence');
  const confidence = confidenceRaw ? Number(confidenceRaw) : undefined;
  if (confidence !== undefined && (Number.isNaN(confidence) || confidence < 0 || confidence > 1)) {
    process.stderr.write(`Invalid --confidence "${confidenceRaw}". Must be a number in [0,1].\n`);
    process.exit(1);
  }
  const notabilityRaw = flagValue(args, '--notability');
  const notability: 'high' | 'medium' | 'low' | undefined =
    notabilityRaw === 'high' || notabilityRaw === 'medium' || notabilityRaw === 'low'
      ? notabilityRaw
      : notabilityRaw ? (process.stderr.write(`Invalid --notability "${notabilityRaw}". Must be high|medium|low.\n`), process.exit(1)) as never
                      : undefined;

  const input: NewFact = {
    fact: claim,
    kind,
    entity_slug: entity ?? null,
    visibility,
    context: context ?? null,
    valid_from: validFrom,
    valid_until: validUntil ?? null,
    source: sourceLabel,
    source_session: sessionId,
    confidence,
    notability,
    embedding: null,
  };

  const result = await engine.insertFact(input, { source_id: sourceId });
  if (flagPresent(args, '--json')) {
    process.stdout.write(JSON.stringify(result) + '\n');
  } else {
    process.stdout.write(`Inserted fact #${result.id} (status: ${result.status})\n`);
    process.stdout.write(`  entity:    ${entity ?? '(unscoped)'}\n`);
    process.stdout.write(`  claim:     ${claim}\n`);
    process.stdout.write(`  kind:      ${kind ?? 'fact (default)'}\n`);
    process.stdout.write(`  source:    ${sourceLabel}\n`);
    if (validUntil) process.stdout.write(`  valid_until: ${validUntil.toISOString()}\n`);
  }
}

// --- cmdExpire ---

async function cmdExpire(engine: BrainEngine, args: string[]): Promise<void> {
  const idArg = args[0];
  if (!idArg || idArg.startsWith('-')) {
    process.stderr.write('Error: <id> is required as first arg.\n');
    printExpireUsage();
    process.exit(1);
  }
  const id = Number(idArg);
  if (!Number.isInteger(id) || id < 1) {
    process.stderr.write(`Invalid id "${idArg}". Must be a positive integer.\n`);
    process.exit(1);
  }
  const reason = flagValue(args, '--reason');
  const supersedeRaw = flagValue(args, '--superseded-by');
  const supersededBy = supersedeRaw ? Number(supersedeRaw) : undefined;
  if (supersedeRaw && (!Number.isInteger(supersededBy!) || supersededBy! < 1)) {
    process.stderr.write(`Invalid --superseded-by "${supersedeRaw}". Must be a positive integer.\n`);
    process.exit(1);
  }
  const ok = await engine.expireFact(id, { supersededBy });
  if (flagPresent(args, '--json')) {
    process.stdout.write(JSON.stringify({ id, expired: ok, reason: reason ?? null, supersededBy: supersededBy ?? null }) + '\n');
  } else if (ok) {
    process.stdout.write(`Expired fact #${id}${supersededBy ? ` (superseded by #${supersededBy})` : ''}\n`);
    if (reason) process.stdout.write(`  reason: ${reason}\n`);
  } else {
    process.stdout.write(`No-op: fact #${id} not found or already expired\n`);
  }
}

// --- cmdList ---

async function cmdList(engine: BrainEngine, args: string[]): Promise<void> {
  const entity = flagValue(args, '--entity');
  const includeExpired = flagPresent(args, '--include-expired');
  const limitRaw = flagValue(args, '--limit');
  const limit = limitRaw ? Number(limitRaw) : 100;
  if (limitRaw && (!Number.isInteger(limit) || limit < 1 || limit > 1000)) {
    process.stderr.write(`Invalid --limit "${limitRaw}". Must be 1..1000.\n`);
    process.exit(1);
  }
  const sourceId = await resolveSourceId(engine, flagValue(args, '--source-id'));

  let rows: FactRow[];
  const opts = { activeOnly: !includeExpired, limit };
  if (entity) {
    rows = await engine.listFactsByEntity(sourceId, entity, opts);
  } else {
    rows = await engine.listFactsSince(sourceId, new Date(0), opts);
  }

  if (flagPresent(args, '--json')) {
    process.stdout.write(JSON.stringify(rows, null, 2) + '\n');
    return;
  }

  if (rows.length === 0) {
    process.stdout.write(`No facts found${entity ? ` for entity ${entity}` : ''}.\n`);
    return;
  }
  process.stdout.write(`# ${rows.length} fact(s)${entity ? ` for ${entity}` : ''}${includeExpired ? ' (including expired)' : ' (active only)'}\n\n`);
  for (const r of rows) {
    const expired = r.expired_at ? ` [EXPIRED ${r.expired_at.toISOString().slice(0, 10)}]` : '';
    const validUntil = r.valid_until ? ` [valid_until ${r.valid_until.toISOString().slice(0, 10)}]` : '';
    process.stdout.write(`#${r.id} (${r.kind}, ${r.notability}, ${r.source})${expired}${validUntil}\n`);
    if (r.entity_slug) process.stdout.write(`  entity: ${r.entity_slug}\n`);
    process.stdout.write(`  claim:  ${r.fact}\n`);
    if (r.context) process.stdout.write(`  context: ${r.context}\n`);
    process.stdout.write('\n');
  }
}

// --- Usage helpers ---

function printAddUsage(): void {
  process.stdout.write(`Usage: gbrain facts add --claim "<text>" [options]

Required:
  --claim "<text>"           Fact / claim text (the assertion itself)

Common:
  --entity <slug>            Page slug this fact is about (e.g. people/personal/emanuel-ubert)
  --kind <kind>              event | preference | commitment | belief | fact (default: fact)
  --valid-until <iso|now>    Time at which this fact stops being current
                             (e.g. for "no longer doing X" corrections)
  --source <label>           Provenance label (default: operator)
  --notability high|medium|low   Salience tier (default: medium)
  --visibility private|world Visibility ACL (default: private)
  --context "<text>"         Free-form context / reason

Less common:
  --valid-from <iso|now>     Start time (default: now)
  --confidence <0..1>        Confidence weight (default: 1.0)
  --session <id>             Source session identifier
  --source-id <id>           Sources-table FK (default: 'default')
  --json                     Emit JSON result instead of human-readable
`);
}

function printExpireUsage(): void {
  process.stdout.write(`Usage: gbrain facts expire <id> [options]

Required:
  <id>                       Positive integer fact ID (from \`gbrain facts list\`)

Options:
  --reason "<text>"          Reason for expiration (audit-only; not persisted to facts table)
  --superseded-by <id>       Mark this fact as replaced by another fact ID
  --json                     Emit JSON result instead of human-readable
`);
}

function printListUsage(): void {
  process.stdout.write(`Usage: gbrain facts list [options]

Options:
  --entity <slug>            Filter to facts about a specific entity slug
  --include-expired          Include expired_at IS NOT NULL rows (default: hide them)
  --limit <n>                Max rows to return (default: 100; max: 1000)
  --source-id <id>           Sources-table FK (default: 'default')
  --json                     Emit JSON instead of human-readable
`);
}

function printRootUsage(): void {
  process.stdout.write(`Usage: gbrain facts <subcommand> [options]

S195 Phase 1 (enrich_stubs prerequisite) — operator-correction persistence
via the facts table. Per CDD knowledge-base/cdd-contracts/S195-enrich-stubs-phase.md
section 1.10.

Subcommands:
  add        Insert a new fact (typical: --entity <slug> --claim "..." --valid-until now
             for "operator no longer doing X" corrections)
  expire     Mark a fact expired (operator retraction / correction)
  list       List active or all facts; --entity to scope; --include-expired to show expired

Run \`gbrain facts <subcommand> --help\` for per-command flags.
`);
}

// --- Entry point ---

export async function runFacts(engine: BrainEngine, args: string[]): Promise<void> {
  if (args.length === 0 || args[0] === '--help' || args[0] === '-h') {
    printRootUsage();
    return;
  }
  const sub = args[0];
  const rest = args.slice(1);

  if (rest.includes('--help') || rest.includes('-h')) {
    switch (sub) {
      case 'add':    printAddUsage();    return;
      case 'expire': printExpireUsage(); return;
      case 'list':   printListUsage();   return;
    }
  }

  switch (sub) {
    case 'add':    return cmdAdd(engine, rest);
    case 'expire': return cmdExpire(engine, rest);
    case 'list':   return cmdList(engine, rest);
    default:
      process.stderr.write(`Unknown subcommand: ${sub}\n\n`);
      printRootUsage();
      process.exit(1);
  }
}
