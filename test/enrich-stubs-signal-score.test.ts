/**
 * S196 Phase 2 unit tests for enrich-stubs/signal-score.ts.
 *
 * Per CDD S195-enrich-stubs-phase.md §2.1 T3 + T4 (pre-flight tests).
 *
 * Pure-function tests; no DB, no LLM, no IO. Each test builds a synthetic
 * Page via makePage() helper + asserts the utility function's output.
 */

import { describe, test, expect } from 'bun:test';
import type { Page } from '../src/core/types.ts';
import {
  CANONICAL_SECTIONS,
  detectSubstantiveSections,
  isStub,
  isCalendarAttendanceBacklink,
  signalScore,
  DEFAULT_THRESHOLD_ENRICH,
} from '../src/core/enrich-stubs/signal-score.ts';

// ============================================================
// Fixture builder
// ============================================================

function makePage(overrides: Partial<Page> & { type: string; slug: string } = { type: 'person', slug: 'people/test' }): Page {
  // Default body = all 8 canonical sections with [No data yet] placeholder
  // — i.e. the canonical fresh-stub shape.
  const defaultBody = CANONICAL_SECTIONS
    .map(s => `## ${s}\n\n[No data yet]\n`)
    .join('\n');
  return {
    id: 1,
    slug: overrides.slug ?? 'people/test',
    type: (overrides.type ?? 'person') as Page['type'],
    title: overrides.title ?? 'Test',
    compiled_truth: overrides.compiled_truth ?? defaultBody,
    timeline: overrides.timeline ?? '',
    frontmatter: overrides.frontmatter ?? {},
    content_hash: undefined,
    created_at: overrides.created_at ?? new Date('2026-01-01T00:00:00Z'),
    updated_at: overrides.updated_at ?? new Date('2026-05-24T00:00:00Z'),
    source_id: overrides.source_id ?? 'default',
    ...overrides,
  };
}

// ============================================================
// isStub — §1.3
// ============================================================

describe('isStub — CDD §1.3 (5 criteria)', () => {

  test('PASS: fresh person-stub (all-placeholder canonical sections, no operator content)', () => {
    const page = makePage({ type: 'person', slug: 'people/audia' });
    expect(isStub(page)).toBe(true);
  });

  test('PASS: fresh institution-stub', () => {
    const page = makePage({ type: 'institution', slug: 'institutions/rsm' });
    expect(isStub(page)).toBe(true);
  });

  test('FAIL: type not eligible (diary-entry)', () => {
    const page = makePage({ type: 'diary-entry', slug: 'diary/2026/03/2026-03-15' });
    expect(isStub(page)).toBe(false);
  });

  test('FAIL: operator-authored Assessment section present', () => {
    const body = CANONICAL_SECTIONS
      .map(s => s === 'Assessment'
        ? `## ${s}\n\nMy assessment: substantive operator content here, definitely not a placeholder.\n`
        : `## ${s}\n\n[No data yet]\n`)
      .join('\n');
    const page = makePage({ type: 'person', slug: 'people/marc-ventresca', compiled_truth: body });
    expect(isStub(page)).toBe(false);
  });

  test('PASS: empty Assessment counts as placeholder', () => {
    const body = CANONICAL_SECTIONS
      .map(s => s === 'Assessment' ? `## ${s}\n\n[No data yet]\n` : `## ${s}\n\n[No data yet]\n`)
      .join('\n');
    const page = makePage({ type: 'person', compiled_truth: body });
    expect(isStub(page)).toBe(true);
  });

  test('FAIL: frontmatter.protected == true', () => {
    const page = makePage({ type: 'person', frontmatter: { protected: true } });
    expect(isStub(page)).toBe(false);
  });

  test('FAIL: only 4 of 8 canonical sections are placeholder (below default threshold of 5)', () => {
    // 4 sections with real content + 4 with [No data yet]
    const body = CANONICAL_SECTIONS
      .map((s, i) => i < 4
        ? `## ${s}\n\nReal substantive content for section ${s}. Multiple sentences. Operator authored. Not a placeholder at all.\n`
        : `## ${s}\n\n[No data yet]\n`)
      .join('\n');
    const page = makePage({ type: 'person', compiled_truth: body });
    expect(isStub(page)).toBe(false);
  });

  test('FAIL: page already enriched against current substrate (last_enriched_at >= updated_at)', () => {
    const page = makePage({
      type: 'person',
      updated_at: new Date('2026-05-20T00:00:00Z'),
    });
    // Inject last_enriched_at AFTER updated_at (simulates page enriched + not yet modified)
    (page as Page & { last_enriched_at: Date }).last_enriched_at = new Date('2026-05-22T00:00:00Z');
    expect(isStub(page)).toBe(false);
  });

  test('PASS: page enriched but substrate has changed since (last_enriched_at < updated_at)', () => {
    const page = makePage({
      type: 'person',
      updated_at: new Date('2026-05-24T00:00:00Z'),
    });
    (page as Page & { last_enriched_at: Date }).last_enriched_at = new Date('2026-05-20T00:00:00Z');
    expect(isStub(page)).toBe(true);
  });

  test('PASS: meeting type also eligible', () => {
    const page = makePage({ type: 'meeting', slug: 'meetings/2026/05/2026-05-24-test' });
    expect(isStub(page)).toBe(true);
  });

});

// ============================================================
// signalScore — §1.4
// ============================================================

describe('signalScore — CDD §1.4 (3/2/1/0.5 + 20 operator-flag)', () => {

  test('zero substrate → score 0', () => {
    const page = makePage({});
    const result = signalScore(page, {
      diaryMentions: 0,
      nonAttendanceBacklinks: 0,
      calendarAttendanceBacklinks: 0,
      substantiveSourceCount: 0,
    });
    expect(result.score).toBe(0);
    expect(result.operatorFlagged).toBe(false);
  });

  test('1 substantive source → score 3', () => {
    const page = makePage({});
    const result = signalScore(page, {
      diaryMentions: 0,
      nonAttendanceBacklinks: 0,
      calendarAttendanceBacklinks: 0,
      substantiveSourceCount: 1,
    });
    expect(result.score).toBe(3);
    expect(result.components.substantiveSourceContribution).toBe(3);
  });

  test('1 substantive + 2 diary + 1 backlink = 8 (qualifies at default threshold)', () => {
    const page = makePage({});
    const result = signalScore(page, {
      diaryMentions: 2,
      nonAttendanceBacklinks: 1,
      calendarAttendanceBacklinks: 0,
      substantiveSourceCount: 1,
    });
    expect(result.score).toBe(8);
    expect(result.score).toBeGreaterThanOrEqual(DEFAULT_THRESHOLD_ENRICH);
  });

  test('12 calendar attendance backlinks alone = 6 (does NOT qualify — substantive scope intent)', () => {
    const page = makePage({});
    const result = signalScore(page, {
      diaryMentions: 0,
      nonAttendanceBacklinks: 0,
      calendarAttendanceBacklinks: 12,
      substantiveSourceCount: 0,
    });
    expect(result.score).toBe(6);
    expect(result.score).toBeLessThan(DEFAULT_THRESHOLD_ENRICH);
  });

  test('operator priority:high → +20 bonus (qualifies regardless of substrate)', () => {
    const page = makePage({ frontmatter: { priority: 'high' } });
    const result = signalScore(page, {
      diaryMentions: 0,
      nonAttendanceBacklinks: 0,
      calendarAttendanceBacklinks: 0,
      substantiveSourceCount: 0,
    });
    expect(result.score).toBe(20);
    expect(result.operatorFlagged).toBe(true);
    expect(result.components.operatorFlagBonus).toBe(20);
  });

  test('priority != high → no bonus', () => {
    const page = makePage({ frontmatter: { priority: 'medium' } });
    const result = signalScore(page, {
      diaryMentions: 0,
      nonAttendanceBacklinks: 0,
      calendarAttendanceBacklinks: 0,
      substantiveSourceCount: 1,
    });
    expect(result.score).toBe(3);
    expect(result.operatorFlagged).toBe(false);
  });

  test('all components combined', () => {
    const page = makePage({ frontmatter: { priority: 'high' } });
    const result = signalScore(page, {
      diaryMentions: 5,        // 10
      nonAttendanceBacklinks: 3, // 3
      calendarAttendanceBacklinks: 4, // 2
      substantiveSourceCount: 2,  // 6
    });
    // 6 + 10 + 3 + 2 + 20 = 41
    expect(result.score).toBe(41);
    expect(result.components.substantiveSourceContribution).toBe(6);
    expect(result.components.diaryMentionContribution).toBe(10);
    expect(result.components.nonAttendanceBacklinkContribution).toBe(3);
    expect(result.components.calendarAttendanceContribution).toBe(2);
    expect(result.components.operatorFlagBonus).toBe(20);
  });

  test('breakdown.inputs echoes the input counts (debugging convenience)', () => {
    const page = makePage({});
    const inputs = {
      diaryMentions: 7,
      nonAttendanceBacklinks: 11,
      calendarAttendanceBacklinks: 4,
      substantiveSourceCount: 3,
    };
    const result = signalScore(page, inputs);
    expect(result.inputs).toEqual(inputs);
  });

  test('fractional score (single calendar attendance backlink)', () => {
    const page = makePage({});
    const result = signalScore(page, {
      diaryMentions: 0,
      nonAttendanceBacklinks: 0,
      calendarAttendanceBacklinks: 1,
      substantiveSourceCount: 0,
    });
    expect(result.score).toBe(0.5);
  });

  test('borderline-qualify: 3 non-attendance backlinks + 1 substantive = 6 (DOES NOT qualify by 2 pts)', () => {
    const page = makePage({});
    const result = signalScore(page, {
      diaryMentions: 0,
      nonAttendanceBacklinks: 3,
      calendarAttendanceBacklinks: 0,
      substantiveSourceCount: 1,
    });
    expect(result.score).toBe(6);
    expect(result.score).toBeLessThan(DEFAULT_THRESHOLD_ENRICH);
  });

});

// ============================================================
// isCalendarAttendanceBacklink — §1.4 classification
// ============================================================

describe('isCalendarAttendanceBacklink — CDD §1.4', () => {

  test('TRUE: meetings/ + "Met re: X"', () => {
    expect(isCalendarAttendanceBacklink('meetings/2025/03/2025-03-15-amy', 'Met re: project review')).toBe(true);
  });

  test('TRUE: meetings/ + "Attended X"', () => {
    expect(isCalendarAttendanceBacklink('meetings/2025/04/2025-04-02-conf', 'Attended speaker session')).toBe(true);
  });

  test('TRUE: meetings/ + "Calendar: X"', () => {
    expect(isCalendarAttendanceBacklink('meetings/2025/05/2025-05-10-cal', 'Calendar: weekly 1:1')).toBe(true);
  });

  test('FALSE: meetings/ but substantive context (not attendance)', () => {
    // Operator note in meeting page that genuinely discusses the entity
    expect(isCalendarAttendanceBacklink('meetings/2025/03/2025-03-15-amy',
      'Discussed her work on consensus mechanisms in DAOs at length')).toBe(false);
  });

  test('FALSE: non-meetings page (diary)', () => {
    expect(isCalendarAttendanceBacklink('diary/2025/03/2025-03-15', 'Met re: project review')).toBe(false);
  });

  test('FALSE: non-meetings page (concept)', () => {
    expect(isCalendarAttendanceBacklink('concepts/ai-agents', 'Met re: framework discussion')).toBe(false);
  });

  test('FALSE: empty context', () => {
    expect(isCalendarAttendanceBacklink('meetings/2025/03/2025-03-15-amy', '')).toBe(false);
  });

});

// ============================================================
// detectSubstantiveSections — §1.6
// ============================================================

describe('detectSubstantiveSections — CDD §1.6 (sources counted, not subsections)', () => {

  test('no substantive sources → empty array', () => {
    const page = makePage({});
    expect(detectSubstantiveSections(page)).toEqual([]);
  });

  test('LinkedIn: single LinkedIn heading counts as 1', () => {
    const page = makePage({
      compiled_truth: '## LinkedIn Profile Snapshot\n\nWorks at Acme. 5 years tenure.\n',
    });
    expect(detectSubstantiveSections(page)).toEqual(['LinkedIn']);
  });

  test('LinkedIn: 3 LinkedIn subsections still count as 1 (no subsection gaming)', () => {
    const page = makePage({
      compiled_truth: [
        '## LinkedIn Profile Snapshot\n\nA',
        '## Career Arc (from LinkedIn)\n\nB',
        '## What They Engage On (LinkedIn signal)\n\nC',
      ].join('\n\n'),
    });
    const sources = detectSubstantiveSections(page);
    expect(sources.filter(s => s === 'LinkedIn').length).toBe(1);
  });

  test('AppleContacts: heading present', () => {
    const page = makePage({
      compiled_truth: '## Contact Details\n\nPhone: redacted. Email: redacted.\n',
    });
    expect(detectSubstantiveSections(page)).toContain('AppleContacts');
  });

  test('AppleContacts: frontmatter marker (no heading) — detected', () => {
    const page = makePage({
      frontmatter: { apple_contacts_unique_id: 'ABC-DEF-123' },
    });
    expect(detectSubstantiveSections(page)).toContain('AppleContacts');
  });

  test('combined: LinkedIn + AppleContacts + 1 operator section', () => {
    const operatorBody = 'A'.repeat(150); // >= 100 chars
    const page = makePage({
      compiled_truth: [
        '## LinkedIn Profile Snapshot\n\nX',
        '## Contact Details\n\nY',
        `## My Notes On This Person\n\n${operatorBody}`,
      ].join('\n\n'),
    });
    const sources = detectSubstantiveSections(page);
    expect(sources).toContain('LinkedIn');
    expect(sources).toContain('AppleContacts');
    expect(sources.some(s => typeof s === 'object' && s.kind === 'OperatorAuthored' && s.heading === 'My Notes On This Person'))
      .toBe(true);
    expect(sources.length).toBe(3);
  });

  test('operator-authored heading with body < 100 chars → not counted', () => {
    const page = makePage({
      compiled_truth: '## Random Note\n\nShort\n',
    });
    expect(detectSubstantiveSections(page)).toEqual([]);
  });

  test('canonical headings (Assessment, State, etc.) NOT counted as operator-authored', () => {
    const page = makePage({
      compiled_truth: '## Assessment\n\n' + 'Real assessment content '.repeat(20),
    });
    // Operator-authored Assessment should NOT count as a substantive source
    // for signal-score purposes (Assessment is part of canonical schema).
    expect(detectSubstantiveSections(page)).toEqual([]);
  });

});
