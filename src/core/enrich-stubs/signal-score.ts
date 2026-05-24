/**
 * S196 Phase 2 — signal-score + is_stub + substantive_section detectors.
 *
 * Pure utility functions. No DB, no LLM, no IO. Take Page + pre-computed
 * counts as inputs; return scoring + detection results. The Phase 3
 * dispatcher (separate file) is responsible for the DB queries that
 * populate the count inputs.
 *
 * Per CDD ~/resources/local-agent-system/knowledge-base/cdd-contracts/
 * S195-enrich-stubs-phase.md §1.3 (is_stub), §1.4 (signal-score),
 * §1.6 (substantive_section enumeration).
 */

import type { Page } from '../types.ts';

// ============================================================
// Canonical section set — §1.3 + §1.6
// ============================================================

/**
 * The 8 canonical sections per GBRAIN_RECOMMENDED_SCHEMA.md. is_stub()
 * counts how many of these have `[No data yet]` body to determine
 * canonical-scaffold-stub status (§1.3 criterion 3).
 *
 * Names match the markdown heading text (without leading `## `).
 */
export const CANONICAL_SECTIONS: readonly string[] = [
  'State',
  'What They Believe',
  'What They\'re Building',
  'What They\'ve Done',
  'How They Operate',
  'What They Engage On',
  'Open Questions',
  'Assessment',
] as const;

/**
 * Headings produced by linkedin-deepen skill (linkedin-bootstrap +
 * linkedin-deepen v0.1.x). Any present = LinkedIn substantive source.
 */
export const LINKEDIN_SECTION_HEADINGS: readonly string[] = [
  'LinkedIn Profile Snapshot',
  'Career Arc (from LinkedIn)',
  'What They Engage On (LinkedIn signal)',
  'What They\'ve Done (from LinkedIn endorsements)',
] as const;

// ============================================================
// SubstantiveSource enum + detector — §1.6
// ============================================================

export type SubstantiveSource =
  | 'LinkedIn'
  | 'AppleContacts'
  | 'EmailSignature'
  | 'ExternalBio'
  | { kind: 'OperatorAuthored'; heading: string };

/**
 * Match an H2 heading at the start of a line in the page body.
 * Tolerant of trailing whitespace; case-sensitive on heading text.
 */
function hasH2(body: string, heading: string): boolean {
  const escaped = heading.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const re = new RegExp(`^##\\s+${escaped}\\s*$`, 'm');
  return re.test(body);
}

/**
 * Returns the body text of an H2 section (between `## <heading>` and the
 * next H2 or EOF). Returns null if the section is absent.
 */
function sectionBody(body: string, heading: string): string | null {
  const escaped = heading.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const startRe = new RegExp(`^##\\s+${escaped}\\s*$`, 'm');
  const startMatch = startRe.exec(body);
  if (!startMatch) return null;
  const startIdx = startMatch.index + startMatch[0].length;
  const rest = body.slice(startIdx);
  const nextRe = /^##\s+/m;
  const nextMatch = nextRe.exec(rest);
  const sectionText = nextMatch ? rest.slice(0, nextMatch.index) : rest;
  return sectionText.trim();
}

/**
 * Detect substantive sources present on a page. Counts by SOURCE not by
 * subsection — LinkedIn = 1 regardless of how many LinkedIn-* headings
 * appear (prevents subsection-count gaming per CDD §1.6).
 *
 * Operator-authored sources: any H2 heading NOT in CANONICAL_SECTIONS
 * AND NOT in any other source's heading set, with body length >= 100 chars.
 */
export function detectSubstantiveSections(page: Page): SubstantiveSource[] {
  const body = `${page.compiled_truth}\n${page.timeline}`;
  const sources: SubstantiveSource[] = [];

  // LinkedIn (any of the linkedin-deepen headings)
  if (LINKEDIN_SECTION_HEADINGS.some(h => hasH2(body, h))) {
    sources.push('LinkedIn');
  }

  // Apple Contacts (heading OR frontmatter marker)
  const fm = page.frontmatter ?? {};
  const hasAppleContactsMarker = typeof fm.apple_contacts_unique_id === 'string'
    && fm.apple_contacts_unique_id.length > 0;
  if (hasH2(body, 'Contact Details') || hasAppleContactsMarker) {
    sources.push('AppleContacts');
  }

  // Email signature (heading OR frontmatter marker)
  const hasEmailSigMarker = fm.email_signature_data != null
    && typeof fm.email_signature_data === 'object';
  if (hasH2(body, 'Email Signature Profile') || hasEmailSigMarker) {
    sources.push('EmailSignature');
  }

  // External bio (future-use; defined for forward-compat)
  if (hasH2(body, 'External Bio')) {
    sources.push('ExternalBio');
  }

  // Operator-authored sections (any H2 heading not in the well-known sets,
  // with body length >= 100 chars). Each distinct heading counts once.
  const wellKnown = new Set<string>([
    ...CANONICAL_SECTIONS,
    ...LINKEDIN_SECTION_HEADINGS,
    'Contact Details',
    'Email Signature Profile',
    'External Bio',
    'Facts', // facts-fence convention
  ]);
  const h2Re = /^##\s+(.+?)\s*$/gm;
  let m: RegExpExecArray | null;
  const seenHeadings = new Set<string>();
  while ((m = h2Re.exec(body)) !== null) {
    const heading = m[1];
    if (wellKnown.has(heading)) continue;
    if (seenHeadings.has(heading)) continue;
    const sectionText = sectionBody(body, heading);
    if (sectionText && sectionText.length >= 100) {
      sources.push({ kind: 'OperatorAuthored', heading });
      seenHeadings.add(heading);
    }
  }

  return sources;
}

// ============================================================
// is_stub — §1.3
// ============================================================

const STUB_ELIGIBLE_TYPES: ReadonlySet<string> = new Set([
  'person',
  'institution',
  'concept',
  'idea',
  'meeting',
]);

const PLACEHOLDER_BODY_RE = /^\s*(\[No data yet\]|\[awaiting.*\])\s*$/m;

export interface IsStubOpts {
  /**
   * The page's last-modification timestamp from postgres (pages.updated_at).
   * If provided, the detector compares against last_enriched_at to skip
   * pages whose substrate hasn't changed since last enrichment.
   */
  lastModifiedAt?: Date | null;
  /**
   * Per CDD §1.3 criterion 5: how many canonical sections must be
   * `[No data yet]` or absent for the page to qualify as a
   * canonical-scaffold-stub. Default 5 of 8.
   */
  minPlaceholderCanonicalSections?: number;
}

/**
 * Returns true if the page is a stub eligible for enrichment by the
 * enrich_stubs dream-cycle phase. Implements all 5 criteria from CDD §1.3:
 *
 *   1. Type-eligible (person|institution|concept|idea|meeting)
 *   2. No operator-authored Assessment (section absent OR placeholder body)
 *   3. Canonical-scaffold-stub (>=5 of 8 canonical sections are placeholder/absent)
 *   4. Not operator-protected (frontmatter.protected != true)
 *   5. Not very recently enriched (last_enriched_at IS NULL OR < last-modified)
 *
 * Returns false on ANY failing criterion.
 */
export function isStub(page: Page, opts: IsStubOpts = {}): boolean {
  // Criterion 1: type-eligible
  if (!STUB_ELIGIBLE_TYPES.has(page.type)) return false;

  // Criterion 4: not operator-protected
  const fm = page.frontmatter ?? {};
  if (fm.protected === true) return false;

  const body = page.compiled_truth ?? '';

  // Criterion 2: no operator-authored Assessment
  const assessmentBody = sectionBody(body, 'Assessment');
  if (assessmentBody !== null && assessmentBody.length > 0
      && !PLACEHOLDER_BODY_RE.test(assessmentBody)) {
    return false;
  }

  // Criterion 3: canonical-scaffold-stub (>=N placeholder/absent canonical sections)
  const threshold = opts.minPlaceholderCanonicalSections ?? 5;
  let placeholderCount = 0;
  for (const section of CANONICAL_SECTIONS) {
    const sb = sectionBody(body, section);
    if (sb === null) {
      // Section absent — counts as placeholder
      placeholderCount += 1;
    } else if (sb.length === 0 || PLACEHOLDER_BODY_RE.test(sb)) {
      placeholderCount += 1;
    }
  }
  if (placeholderCount < threshold) return false;

  // Criterion 5: not very recently enriched against current substrate
  const lastEnriched = (page as Page & { last_enriched_at?: Date | null }).last_enriched_at;
  if (lastEnriched) {
    const lastMod = opts.lastModifiedAt ?? page.updated_at;
    if (lastMod && new Date(lastEnriched) >= new Date(lastMod)) {
      return false;
    }
  }

  return true;
}

// ============================================================
// isCalendarAttendanceBacklink — §1.4 backlink classification
// ============================================================

const ATTENDANCE_CONTEXT_RE = /^(Met|Attended|Calendar:|Event:|Meeting:)/i;

/**
 * Classifies a backlink as calendar-attendance vs. non-attendance per CDD §1.4.
 * Conservative: when in doubt, returns false (non-attendance), which weights
 * the backlink HIGHER (1×) instead of lower (0.5×). Misclassifying a meeting
 * as substantive overweights signal slightly; misclassifying a substantive
 * mention as attendance underweights it more. Default toward non-attendance.
 *
 * Returns true iff:
 *   - referringSlug starts with 'meetings/'
 *   - AND contextText matches ^(Met|Attended|Calendar:|Event:|Meeting:)
 */
export function isCalendarAttendanceBacklink(
  referringSlug: string,
  contextText: string,
): boolean {
  if (!referringSlug.startsWith('meetings/')) return false;
  if (!contextText) return false;
  return ATTENDANCE_CONTEXT_RE.test(contextText.trim());
}

// ============================================================
// signalScore — §1.4 formula
// ============================================================

export interface SignalScoreInputs {
  /** Count of diary mentions (timeline_entries WHERE source LIKE diary-* AND entity_slug = page.slug). */
  diaryMentions: number;
  /** Count of incoming backlinks NOT classified as calendar-attendance. */
  nonAttendanceBacklinks: number;
  /** Count of incoming backlinks classified as calendar-attendance. */
  calendarAttendanceBacklinks: number;
  /**
   * Distinct substantive sources present on the page. Caller computes via
   * detectSubstantiveSections() and passes the result. (Decoupled from the
   * page itself so callers can post-process / filter / dedupe.)
   */
  substantiveSourceCount: number;
}

export interface SignalScoreBreakdown {
  score: number;
  components: {
    substantiveSourceContribution: number;
    diaryMentionContribution: number;
    nonAttendanceBacklinkContribution: number;
    calendarAttendanceContribution: number;
    operatorFlagBonus: number;
  };
  inputs: SignalScoreInputs;
  operatorFlagged: boolean;
}

/**
 * Compute signal score per CDD §1.4 (substantive-source dominant variant
 * locked at S195 design conversation).
 *
 * Formula:
 *   score = 3   × substantiveSourceCount
 *         + 2   × diaryMentions
 *         + 1   × nonAttendanceBacklinks
 *         + 0.5 × calendarAttendanceBacklinks
 *         + 20  if frontmatter.priority == 'high'
 *
 * Returns the score AND a breakdown so audit logs + dashboards can show
 * "score 11 = 9 substantive + 2 diary".
 */
export function signalScore(page: Page, inputs: SignalScoreInputs): SignalScoreBreakdown {
  const fm = page.frontmatter ?? {};
  const operatorFlagged = fm.priority === 'high';

  const substantiveSourceContribution = 3 * inputs.substantiveSourceCount;
  const diaryMentionContribution = 2 * inputs.diaryMentions;
  const nonAttendanceBacklinkContribution = 1 * inputs.nonAttendanceBacklinks;
  const calendarAttendanceContribution = 0.5 * inputs.calendarAttendanceBacklinks;
  const operatorFlagBonus = operatorFlagged ? 20 : 0;

  const score = substantiveSourceContribution
              + diaryMentionContribution
              + nonAttendanceBacklinkContribution
              + calendarAttendanceContribution
              + operatorFlagBonus;

  return {
    score,
    components: {
      substantiveSourceContribution,
      diaryMentionContribution,
      nonAttendanceBacklinkContribution,
      calendarAttendanceContribution,
      operatorFlagBonus,
    },
    inputs,
    operatorFlagged,
  };
}

/**
 * Default threshold for enrich_stubs gate (CDD §1.1 locked = 8).
 * Exposed as a constant so tests + admin tooling can reference it.
 */
export const DEFAULT_THRESHOLD_ENRICH = 8;
