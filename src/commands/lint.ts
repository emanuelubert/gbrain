/**
 * gbrain lint — Deterministic brain page quality checker.
 *
 * Zero LLM calls. Catches common quality issues:
 * - LLM preamble artifacts ("Of course! Here is...")
 * - Placeholder dates (YYYY-MM-DD, XX-XX left unfilled)
 * - Missing required frontmatter fields
 * - Broken citations (unclosed brackets, missing dates)
 * - Empty/stub sections
 * - Wrapping code fences from LLM output
 *
 * Usage:
 *   gbrain lint <dir>              # report issues
 *   gbrain lint <dir> --fix        # auto-fix what's fixable
 *   gbrain lint <dir> --fix --dry-run  # preview fixes
 *   gbrain lint <file.md>          # lint single file
 */

import { readFileSync, writeFileSync, readdirSync, statSync, lstatSync, existsSync } from 'fs';
import { join, relative } from 'path';
import { parseMarkdown, type ParseValidationCode } from '../core/markdown.ts';

export interface LintIssue {
  file: string;
  line: number;
  rule: string;
  message: string;
  fixable: boolean;
}

/** Map of frontmatter validation codes to lint rule names. Stable across
 *  releases — agents and CI consumers can target specific rule names. */
const FRONTMATTER_RULE_NAMES: Record<ParseValidationCode, string> = {
  MISSING_OPEN: 'frontmatter-missing-open',
  MISSING_CLOSE: 'frontmatter-missing-close',
  YAML_PARSE: 'frontmatter-yaml-parse',
  SLUG_MISMATCH: 'frontmatter-slug-mismatch',
  NULL_BYTES: 'frontmatter-null-bytes',
  NESTED_QUOTES: 'frontmatter-nested-quotes',
  EMPTY_FRONTMATTER: 'frontmatter-empty',
};

/** Codes whose lint findings are fixable by `gbrain frontmatter validate --fix`. */
const FRONTMATTER_FIXABLE: ReadonlySet<ParseValidationCode> = new Set<ParseValidationCode>([
  'MISSING_CLOSE',
  'NULL_BYTES',
  'NESTED_QUOTES',
]);

// ── LLM artifact patterns ──────────────────────────────────────────

const LLM_PREAMBLES = [
  /^Of course\.?\s*Here is (?:a |the )?(?:detailed |comprehensive |updated )?(?:brain )?page[^.\n]*\.?\s*\n*/gim,
  /^Certainly\.?\s*Here is[^.\n]*\.?\s*\n*/gim,
  /^Here is (?:a |the )?(?:detailed |comprehensive |updated )?(?:brain )?page[^.\n]*\.?\s*\n*/gim,
  /^I've (?:created|updated|written|prepared) (?:a |the )?(?:detailed |comprehensive )?(?:brain )?page[^.\n]*\.?\s*\n*/gim,
  /^Sure(?:!|,)?\s*Here (?:is|are)[^.\n]*\.?\s*\n*/gim,
  /^Absolutely\.?\s*Here[^.\n]*\.?\s*\n*/gim,
];

// ── Rules ──────────────────────────────────────────────────────────

export function lintContent(content: string, filePath: string): LintIssue[] {
  const issues: LintIssue[] = [];
  const lines = content.split('\n');

  // ── Frontmatter validation (delegates to parseMarkdown(validate:true)) ──
  // This is the single source of truth for frontmatter shape rules. Each
  // ParseValidationCode maps to a stable lint rule name in
  // FRONTMATTER_RULE_NAMES. Keeps brain-page lint, doctor's
  // frontmatter_integrity subcheck, and the frontmatter CLI in lockstep.
  const parsed = parseMarkdown(content, filePath, { validate: true });
  for (const err of parsed.errors ?? []) {
    // Skip MISSING_OPEN — the legacy `no-frontmatter` rule below covers this
    // exact case with a stable rule name. Emitting both is double-reporting.
    if (err.code === 'MISSING_OPEN') continue;
    issues.push({
      file: filePath,
      line: err.line ?? 1,
      rule: FRONTMATTER_RULE_NAMES[err.code],
      message: err.message,
      fixable: FRONTMATTER_FIXABLE.has(err.code),
    });
  }

  // Rule: LLM preamble artifacts
  for (const pattern of LLM_PREAMBLES) {
    pattern.lastIndex = 0;
    if (pattern.test(content)) {
      issues.push({
        file: filePath, line: 1, rule: 'llm-preamble',
        message: 'LLM preamble artifact detected (e.g., "Of course! Here is...")',
        fixable: true,
      });
    }
  }

  // Rule: Wrapping code fences (```markdown ... ```)
  if (content.match(/^```(?:markdown|md)\s*\n/m) && content.match(/\n```\s*$/m)) {
    issues.push({
      file: filePath, line: 1, rule: 'code-fence-wrap',
      message: 'Page wrapped in ```markdown code fences (LLM artifact)',
      fixable: true,
    });
  }

  // Rule: Placeholder dates
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].match(/\bYYYY-MM-DD\b/) || lines[i].match(/\bXX-XX\b/) || lines[i].match(/\b\d{4}-XX-XX\b/)) {
      issues.push({
        file: filePath, line: i + 1, rule: 'placeholder-date',
        message: `Placeholder date found: ${lines[i].trim().slice(0, 60)}`,
        fixable: false,
      });
    }
  }

  // Rule: Missing frontmatter
  if (content.startsWith('---')) {
    const fmEnd = content.indexOf('---', 3);
    if (fmEnd > 0) {
      const fm = content.slice(3, fmEnd);
      if (!fm.match(/^title:/m)) {
        issues.push({
          file: filePath, line: 1, rule: 'missing-title',
          message: 'Frontmatter missing required field: title',
          fixable: false,
        });
      }
      if (!fm.match(/^type:/m)) {
        issues.push({
          file: filePath, line: 1, rule: 'missing-type',
          message: 'Frontmatter missing required field: type',
          fixable: false,
        });
      }
      if (!fm.match(/^created:/m)) {
        issues.push({
          file: filePath, line: 1, rule: 'missing-created',
          message: 'Frontmatter missing required field: created',
          fixable: false,
        });
      }
    }
  } else {
    // No frontmatter at all
    issues.push({
      file: filePath, line: 1, rule: 'no-frontmatter',
      message: 'Page has no YAML frontmatter',
      fixable: false,
    });
  }

  // Rule: Broken citations (unclosed [Source: ...)
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    // Open [Source: without closing ]
    if (line.match(/\[Source:[^\]]*$/) && !(i + 1 < lines.length && lines[i + 1].match(/^\s*[^\[]*\]/))) {
      issues.push({
        file: filePath, line: i + 1, rule: 'broken-citation',
        message: 'Unclosed [Source: ...] citation',
        fixable: false,
      });
    }
  }

  // Rule: Empty/stub sections
  const sectionPattern = /^##\s+(.+)$/gm;
  let sectionMatch;
  while ((sectionMatch = sectionPattern.exec(content)) !== null) {
    const sectionStart = sectionMatch.index + sectionMatch[0].length;
    const nextSection = content.indexOf('\n## ', sectionStart);
    const sectionBody = content.slice(sectionStart, nextSection > 0 ? nextSection : undefined).trim();

    // S195 fork-patch (2026-05-24): `[No data yet]` is the canonical L40
    // "no signal" marker per ~/gbrain/docs/ethos/MARKDOWN_SKILLS_AS_RECIPES.md
    // + ~/gbrain/docs/GBRAIN_RECOMMENDED_SCHEMA.md — it is VALID section
    // content marking explicit absence of signal, NOT an error condition.
    // Truly-empty sections still flag; `*[To be filled by agent]*` still
    // flags as "awaiting enrichment". This change clears ~16,344 false-
    // positive lint issues (33% of pre-patch backlog). To revert: restore
    // `|| sectionBody === '[No data yet]'` to the disjunction.
    if (sectionBody === '' || sectionBody === '*[To be filled by agent]*') {
      const lineNum = content.slice(0, sectionMatch.index).split('\n').length;
      issues.push({
        file: filePath, line: lineNum, rule: 'empty-section',
        message: `Empty section: ## ${sectionMatch[1]}`,
        fixable: false,
      });
    }
  }

  return issues;
}

/** Auto-fix fixable issues */
export function fixContent(content: string): string {
  let fixed = content;

  // Fix LLM preambles
  for (const pattern of LLM_PREAMBLES) {
    pattern.lastIndex = 0;
    fixed = fixed.replace(pattern, '');
  }

  // Fix wrapping code fences
  fixed = fixed.replace(/^```(?:markdown|md)\s*\n/, '');
  fixed = fixed.replace(/\n```\s*$/, '');

  // Clean up excessive blank lines left by fixes
  fixed = fixed.replace(/\n{3,}/g, '\n\n');

  return fixed.trim() + '\n';
}

/** Collect markdown files from a directory */
function collectPages(dir: string): string[] {
  const pages: string[] = [];
  function walk(d: string) {
    for (const entry of readdirSync(d)) {
      if (entry.startsWith('.') || entry.startsWith('_')) continue;
      const full = join(d, entry);
      if (lstatSync(full).isDirectory()) walk(full);
      else if (entry.endsWith('.md')) pages.push(full);
    }
  }
  walk(dir);
  return pages.sort();
}

export interface LintOpts {
  target: string;
  fix?: boolean;
  dryRun?: boolean;
}

export interface LintResult {
  pages_scanned: number;
  pages_with_issues: number;
  total_issues: number;
  total_fixed: number;
  dryRun: boolean;
  applied_fix: boolean;
}

// ── S195 A.4 (2026-05-24): per-category routing for the dream-cycle ─────────
// lint phase. Mode values map to behaviors documented in
// `~/resources/local-agent-system/knowledge-base/cdd-contracts/
// S195-dream-lint-active-mode.md` §1.2 operator-approved table.
// To revert this surface area: remove `runLintCoreWithCategories` + the
// frontmatter import below; restore cycle.ts call to plain `runLintCore`.
//
// Background lint rule names (stable; see FRONTMATTER_RULE_NAMES + per-rule
// `issues.push({rule: '<name>'})` calls above) — categories operators
// configure via `gbrain config set dream.lint.<category>.mode <mode>`:
//   - body-fixable:       code-fence-wrap, llm-preamble
//   - frontmatter-fixable: frontmatter-null-bytes, frontmatter-missing-close,
//                          frontmatter-nested-quotes
//   - structural:          frontmatter-slug-mismatch, frontmatter-empty,
//                          frontmatter-yaml-parse, missing-title,
//                          missing-type, missing-created, no-frontmatter,
//                          placeholder-date, broken-citation, empty-section
//
// Operators may pass either the lint rule name (`frontmatter-null-bytes`)
// or the SHOUTY validation code (`NULL_BYTES`) — the former is canonical;
// the latter is honored as a convenience alias against the
// FRONTMATTER_RULE_NAMES map.

export type LintCategoryMode = 'auto-fix' | 'surface-to-operator' | 'audit';

export interface LintCategoryStats {
  total: number;
  fixed: number;
  surfaced: number;
  mode: LintCategoryMode;
}

export interface LintWithCategoriesOpts {
  target: string;
  dryRun?: boolean;
  /** Per-category mode lookup. Receives the canonical lint rule name; may
   *  return undefined (defaults to 'audit'). Async to allow DB reads. */
  getMode: (category: string) => Promise<LintCategoryMode | undefined>;
  /** Where to write the inbox surfacing doc. Defaults to
   *  `<target>/_inbox/lint-surfaced-<YYYY-MM-DD>.md`. */
  inboxDir?: string;
}

export interface LintWithCategoriesResult extends LintResult {
  total_surfaced: number;
  by_category: Record<string, LintCategoryStats>;
  /** Absolute path to the inbox doc, when surfacing wrote something. */
  inbox_path?: string;
}

/**
 * v0.37.x (S195 A.4): library-level lint with per-category mode routing.
 * Active-fix replacement for the audit-only lint phase. Walks pages once;
 * collects issues; routes each category by mode:
 *   - `auto-fix`     → body fixes via fixContent(), frontmatter fixes via
 *                      autoFixFrontmatter(); non-fixable issues in this
 *                      mode degrade silently (counted, not written).
 *   - `surface-to-operator` → collects into <inboxDir>/lint-surfaced-<DATE>.md
 *                             (ONE doc, sections per category — invariant I3).
 *   - `audit`        → counts only (legacy behavior; safe default).
 *
 * Honored only when explicit getMode() returns a mode; otherwise audit.
 * Engine-aware callers wire getMode() to a DB read of `dream.lint.<cat>.mode`.
 */
export async function runLintCoreWithCategories(
  opts: LintWithCategoriesOpts,
): Promise<LintWithCategoriesResult> {
  if (!opts.target) {
    throw new Error('lint: target (dir|file.md) required');
  }
  if (!existsSync(opts.target)) {
    throw new Error(`Not found: ${opts.target}`);
  }
  const isSingleFile = statSync(opts.target).isFile();
  const pages = isSingleFile ? [opts.target] : collectPages(opts.target);

  // SHOUTY-code → canonical-name alias map (operators may use either).
  // Reuses the same mapping the lint rules use internally.
  const SHOUTY_ALIASES: Record<string, string> = {
    NULL_BYTES: 'frontmatter-null-bytes',
    MISSING_CLOSE: 'frontmatter-missing-close',
    NESTED_QUOTES: 'frontmatter-nested-quotes',
    SLUG_MISMATCH: 'frontmatter-slug-mismatch',
    YAML_PARSE: 'frontmatter-yaml-parse',
    EMPTY_FRONTMATTER: 'frontmatter-empty',
  };
  const BODY_FIXABLE = new Set(['code-fence-wrap', 'llm-preamble']);
  const FRONTMATTER_FIXABLE_RULES = new Set([
    'frontmatter-null-bytes',
    'frontmatter-missing-close',
    'frontmatter-nested-quotes',
  ]);

  // Resolve mode per category, with shouty-alias fallback. Cache lookups
  // so we don't query the DB twice per category.
  const modeCache = new Map<string, LintCategoryMode>();
  async function resolveMode(category: string): Promise<LintCategoryMode> {
    if (modeCache.has(category)) return modeCache.get(category)!;
    let mode = await opts.getMode(category);
    if (!mode) {
      // Try SHOUTY alias if the canonical name has no mode set.
      const shouty = Object.entries(SHOUTY_ALIASES).find(([, v]) => v === category)?.[0];
      if (shouty) mode = await opts.getMode(shouty);
    }
    const final: LintCategoryMode = mode ?? 'audit';
    modeCache.set(category, final);
    return final;
  }

  const byCategory: Record<string, LintCategoryStats> = {};
  function bump(category: string, mode: LintCategoryMode, field: 'total' | 'fixed' | 'surfaced') {
    if (!byCategory[category]) {
      byCategory[category] = { total: 0, fixed: 0, surfaced: 0, mode };
    }
    byCategory[category][field]++;
  }

  let totalIssues = 0;
  let totalFixed = 0;
  let totalSurfaced = 0;
  let pagesWithIssues = 0;

  // Surfacing accumulator: category → list of {file, line, message}.
  const surfaced: Record<string, Array<{ file: string; line: number; message: string }>> = {};

  // Lazy import for frontmatter auto-fix; only loaded when we have
  // frontmatter-fixable issues in auto-fix mode.
  let autoFixFrontmatterFn: typeof import('../core/brain-writer.ts').autoFixFrontmatter | null = null;
  async function ensureFrontmatterFixer() {
    if (!autoFixFrontmatterFn) {
      const mod = await import('../core/brain-writer.ts');
      autoFixFrontmatterFn = mod.autoFixFrontmatter;
    }
    return autoFixFrontmatterFn;
  }

  for (const page of pages) {
    const content = readFileSync(page, 'utf-8');
    const relPath = isSingleFile ? page : relative(opts.target, page);
    const issues = lintContent(content, relPath);
    if (issues.length === 0) continue;
    pagesWithIssues++;
    totalIssues += issues.length;

    // Resolve modes for every category that appears on this page.
    const pageCategories = new Set(issues.map(i => i.rule));
    const pageModes = new Map<string, LintCategoryMode>();
    for (const cat of pageCategories) pageModes.set(cat, await resolveMode(cat));

    // Bump totals first (mode-independent count).
    for (const i of issues) bump(i.rule, pageModes.get(i.rule)!, 'total');

    // ── Pass 1: body fixes (auto-fix mode AND body-fixable) ──────────────
    const hasBodyAutofix = issues.some(
      i => BODY_FIXABLE.has(i.rule) && pageModes.get(i.rule) === 'auto-fix' && i.fixable,
    );
    let working = content;
    let modified = false;
    if (hasBodyAutofix) {
      const fixed = fixContent(working);
      if (fixed !== working) {
        working = fixed;
        modified = true;
        for (const i of issues) {
          if (BODY_FIXABLE.has(i.rule) && pageModes.get(i.rule) === 'auto-fix' && i.fixable) {
            totalFixed++;
            bump(i.rule, pageModes.get(i.rule)!, 'fixed');
          }
        }
      }
    }

    // ── Pass 2: frontmatter fixes (auto-fix mode AND frontmatter-fixable) ─
    const hasFmAutofix = issues.some(
      i => FRONTMATTER_FIXABLE_RULES.has(i.rule) && pageModes.get(i.rule) === 'auto-fix',
    );
    if (hasFmAutofix) {
      const fix = await ensureFrontmatterFixer();
      const { content: fixedFm, fixes } = fix(working, { filePath: page });
      if (fixes.length > 0 && fixedFm !== working) {
        working = fixedFm;
        modified = true;
        // Count one "fixed" per matching issue per applied fix code (1:1 map).
        const appliedCodes = new Set(fixes.map(f => f.code));
        for (const i of issues) {
          if (!FRONTMATTER_FIXABLE_RULES.has(i.rule)) continue;
          if (pageModes.get(i.rule) !== 'auto-fix') continue;
          // Map canonical rule name back to validation code for cross-check.
          const code = Object.entries(SHOUTY_ALIASES).find(([, v]) => v === i.rule)?.[0];
          if (code && appliedCodes.has(code as never)) {
            totalFixed++;
            bump(i.rule, pageModes.get(i.rule)!, 'fixed');
          }
        }
      }
    }

    // ── Pass 3: surfacing collection ─────────────────────────────────────
    for (const i of issues) {
      const mode = pageModes.get(i.rule)!;
      if (mode !== 'surface-to-operator') continue;
      if (!surfaced[i.rule]) surfaced[i.rule] = [];
      surfaced[i.rule].push({ file: relPath, line: i.line, message: i.message });
      totalSurfaced++;
      bump(i.rule, mode, 'surfaced');
    }

    // Write modified content (if any) once per page.
    if (modified && !opts.dryRun) {
      writeFileSync(page, working);
    }
  }

  // ── Inbox emission (one doc, sections per category) ──────────────────
  let inboxPath: string | undefined;
  const surfacedCategories = Object.keys(surfaced);
  if (surfacedCategories.length > 0 && !opts.dryRun) {
    const today = new Date().toISOString().slice(0, 10);
    const inboxDir = opts.inboxDir ?? join(opts.target, '_inbox');
    if (!existsSync(inboxDir)) mkdirSync(inboxDir, { recursive: true });
    inboxPath = join(inboxDir, `lint-surfaced-${today}.md`);
    const sections: string[] = [];
    sections.push(`---`);
    sections.push(`title: "Dream-cycle lint surfacing — ${today}"`);
    sections.push(`type: inbox`);
    sections.push(`created: ${today}`);
    sections.push(`tags: [lint, dream-cycle, operator-review]`);
    sections.push(`---`);
    sections.push(``);
    sections.push(`# Lint issues surfaced for operator review`);
    sections.push(``);
    sections.push(`Generated by the dream-cycle lint phase; categories below`);
    sections.push(`are configured to \`surface-to-operator\` mode. Triage by`);
    sections.push(`category cluster; resolve underlying ingest pipeline if`);
    sections.push(`a category recurs.`);
    sections.push(``);
    for (const category of surfacedCategories.sort()) {
      const entries = surfaced[category];
      sections.push(`## ${category} (${entries.length})`);
      sections.push(``);
      for (const e of entries) {
        sections.push(`- \`${e.file}:${e.line}\` — ${e.message}`);
      }
      sections.push(``);
    }
    writeFileSync(inboxPath, sections.join('\n') + '\n');
  }

  return {
    pages_scanned: pages.length,
    pages_with_issues: pagesWithIssues,
    total_issues: totalIssues,
    total_fixed: totalFixed,
    total_surfaced: totalSurfaced,
    dryRun: !!opts.dryRun,
    applied_fix: totalFixed > 0,
    by_category: byCategory,
    inbox_path: inboxPath,
  };
}

/**
 * Library-level lint. Throws on validation errors (missing target, target
 * not found); lints otherwise. Does NOT print human-readable details (the
 * CLI wrapper handles that) — returns counts so Minions handlers can
 * report structured results. Safe from the worker — no process.exit.
 */
export async function runLintCore(opts: LintOpts): Promise<LintResult> {
  if (!opts.target) {
    throw new Error('lint: target (dir|file.md) required');
  }
  if (!existsSync(opts.target)) {
    throw new Error(`Not found: ${opts.target}`);
  }

  const isSingleFile = statSync(opts.target).isFile();
  const pages = isSingleFile ? [opts.target] : collectPages(opts.target);

  let totalIssues = 0;
  let totalFixed = 0;
  let pagesWithIssues = 0;

  for (const page of pages) {
    const content = readFileSync(page, 'utf-8');
    const issues = lintContent(content, isSingleFile ? page : relative(opts.target, page));
    if (issues.length === 0) continue;
    pagesWithIssues++;
    totalIssues += issues.length;

    if (opts.fix && issues.some(i => i.fixable)) {
      const fixed = fixContent(content);
      if (fixed !== content) {
        const fixCount = issues.filter(i => i.fixable).length;
        totalFixed += fixCount;
        if (!opts.dryRun) {
          writeFileSync(page, fixed);
        }
      }
    }
  }

  return {
    pages_scanned: pages.length,
    pages_with_issues: pagesWithIssues,
    total_issues: totalIssues,
    total_fixed: totalFixed,
    dryRun: !!opts.dryRun,
    applied_fix: !!opts.fix,
  };
}

export async function runLint(args: string[]) {
  const target = args.find(a => !a.startsWith('--'));
  const doFix = args.includes('--fix');
  const dryRun = args.includes('--dry-run');

  if (!target) {
    console.error('Usage: gbrain lint <dir|file.md> [--fix] [--dry-run]');
    console.error('  --fix      Auto-fix fixable issues (LLM preambles, code fences)');
    console.error('  --dry-run  Preview fixes without writing');
    process.exit(1);
  }

  if (!existsSync(target)) {
    console.error(`Not found: ${target}`);
    process.exit(1);
  }

  // Single file or directory — print human detail as we go, then rely on
  // Core for the aggregate numbers at the end.
  const isSingleFile = statSync(target).isFile();
  const pages = isSingleFile ? [target] : collectPages(target);

  // Progress on stderr. Stdout keeps the per-issue human output it always had.
  const { createProgress } = await import('../core/progress.ts');
  const { getCliOptions, cliOptsToProgressOptions } = await import('../core/cli-options.ts');
  const progress = createProgress(cliOptsToProgressOptions(getCliOptions()));
  progress.start('lint.pages', pages.length);

  for (const page of pages) {
    const content = readFileSync(page, 'utf-8');
    const relPath = isSingleFile ? page : relative(target, page);
    const issues = lintContent(content, relPath);
    progress.tick(1);
    if (issues.length === 0) continue;

    console.log(`\n${relPath}:`);
    for (const issue of issues) {
      const fixLabel = issue.fixable ? ' [fixable]' : '';
      console.log(`  L${issue.line} ${issue.rule}: ${issue.message}${fixLabel}`);
    }

    if (doFix && issues.some(i => i.fixable)) {
      const fixed = fixContent(content);
      if (fixed !== content) {
        const fixCount = issues.filter(i => i.fixable).length;
        if (!dryRun) {
          writeFileSync(page, fixed);
        }
        console.log(`  ${dryRun ? '(dry run) ' : ''}Fixed ${fixCount} issue(s)`);
      }
    }
  }

  progress.finish();

  // Re-run core for the aggregate counts (cheap; re-parses contents but
  // produces canonical numbers for the summary line).
  const result = await runLintCore({ target, fix: doFix, dryRun });
  console.log(`\n${result.pages_scanned} pages scanned. ${result.total_issues} issue(s) in ${result.pages_with_issues} page(s).`);
  if (doFix) {
    console.log(`${dryRun ? '(dry run) ' : ''}${result.total_fixed} auto-fixed.`);
  } else if (result.total_issues > 0) {
    console.log(`Run with --fix to auto-fix fixable issues.`);
  }
}
