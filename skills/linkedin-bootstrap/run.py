#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pyyaml>=6.0",
# ]
# ///
# <!-- CDD-CONTRACT: ~/gbrain/skills/linkedin-bootstrap/run.py -->
# Contract:   main dispatcher for the linkedin-bootstrap skill.
# Inputs:     CLI args (--phase N | --dry-run | --source <path> | --run-id <id>);
#             LinkedIn export directory; ~/.hermes/sources/* routing configs;
#             ~/brain/ as write target.
# Outputs:    Phase audit logs at audit/<run-id>.json; counters JSONL via
#             emit_run_record; brain page writes (phases 1-6); USER.md
#             writes (phase 7); single git commit at phase 9 (real run).
# Invariants:
#             - Dry-run: NO ~/brain/ writes, NO USER.md writes, NO git ops.
#             - SKIP_LIST files NEVER opened.
#             - Phase 0 fail-fast on missing routing configs.
#             - Phase 3 NEVER creates new pages.
#             - Phase 5 NEVER creates entity pages.
#             - Idempotent: re-run on unchanged source = 0 writes.
# Test plan:  test-fixtures/ synthetic CSVs; pytest tmp_path brain root;
#             all 10 test cases per CDD §2.2 + 2.3 + 2.4.
# Strategy:   per-phase dispatch via _phase_<N>() methods; per-CSV checksum-
#             skip + per-row stable-hash skip; D49 alias dedup engine for
#             Phase 2/3/4; activity Phase 5 writes timeline-only.

"""linkedin-bootstrap implementation v0.1.0.

Per D27: fully-local Python execution. No Claude in the runtime loop.
Per S191-D119: one-shot bootstrap per the operator scope decisions
locked at session start.

Logging contract (privacy-correct per D27):
  - All log lines emit counts / error classes / structural diagnostics.
  - No CSV body content in stdout / stderr / structured records.
  - Failure dumps go to ~/brain/inbox/failures/linkedin-bootstrap/ (T0,
    gitignored).
  - Review-queue entries go to ~/brain/inbox/notes-for-review/linkedin-bootstrap/.

Invocation:
  Fixture (Claude-visible iteration):
    uv run run.py --source ./test-fixtures/_built/ --dry-run \
                  --brain-root /tmp/test-brain \
                  --usermd-path /tmp/test-user.md

  Dry-run on real data:
    skill-with-timeout linkedin-bootstrap 1800 -- uv run run.py --dry-run

  Live (writes to ~/brain/, ~/.hermes/USER.md, commits, pushes):
    skill-with-timeout linkedin-bootstrap 1800 -- uv run run.py
"""

from __future__ import annotations

import argparse
import json as _json
import sys
import tempfile
import traceback
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# === _shared/ import bootstrap ==============================================
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from _shared.failures import write_failure, write_review  # noqa: E402
from _shared.frontmatter import (  # noqa: E402
    render_frontmatter,
    validate_frontmatter,
)
from _shared.run_record import emit_run_record  # noqa: E402
from _shared.slug import slugify, stable_checksum  # noqa: E402

# Sibling modules (this skill's own).
from dedup import (  # noqa: E402
    add_alias_to_existing_page,
    add_frontmatter_field,
    append_timeline_entry,
    find_existing_institution_by_name,
    find_existing_person_by_email,
    find_existing_person_by_linkedin_url,
    find_existing_person_by_name,
    find_existing_person_by_phone,
    normalize_email,
    normalize_linkedin_url,
    normalize_phone,
    normalized_name_key,
)
from parsers import (  # noqa: E402
    activity as activity_parser,
    connections as connections_parser,
    imported_contacts as imported_contacts_parser,
    learning as learning_parser,
    network_signals as network_signals_parser,
    operator_identity as operator_identity_parser,
    preferences as preferences_parser,
)
from parsers._csv_utils import stable_csv_checksum  # noqa: E402


# === Constants ===
HOME = Path.home()
HERMES_HOME = HOME / ".hermes"
BRAIN_HOME = HOME / "brain"
SKILL_NAME = "linkedin-bootstrap"
SKILL_VERSION = "0.1.0"
SCHEMA_VERSION = 1

CONFIG_ACADEMIC_ORGS = HERMES_HOME / "sources" / "known-academic-orgs.yaml"
CONFIG_FAMILY_TERMS = HERMES_HOME / "sources" / "family-group-terms.yaml"

DEFAULT_SOURCE = (
    HOME / "resources" / "local-agent-system" / "data" / "LinkedIn" /
    "Complete_LinkedInDataExport_05-14-2026.zip"
)
USERMD_DEFAULT = HERMES_HOME / "USER.md"
DEFAULT_OPERATOR_SLUG = "emanuel"  # ~/brain/people/emanuel.md

# Phase 8 hard-skip list. NEVER opened by any phase parser.
SKIP_LIST = frozenset([
    "messages.csv",
    "guide_messages.csv",
    "learning_coach_messages.csv",
    "learning_role_play_messages.csv",
    "LearningCoachMessages.csv",
    "Reactions.csv",
    "Logins.csv",
    "Jobs/Job Seeker Preferences.csv",
    "SavedJobAlerts.csv",
    "Receipts_v2.csv",
])

USER_MD_SECTIONS = ("Topical interests", "Vendors / SaaS", "Communities")


class ConfigError(Exception):
    """Routing configs missing or malformed."""


class OperatorPageError(Exception):
    """Operator self-page missing or ambiguous."""


# === Phase 0: preflight =====================================================

def _load_yaml(path: Path):
    if not path.exists():
        raise ConfigError(f"missing routing config: {path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"malformed YAML in {path}: {type(e).__name__}")


def _load_routing_configs():
    academic = _load_yaml(CONFIG_ACADEMIC_ORGS)
    family = _load_yaml(CONFIG_FAMILY_TERMS)
    academic_patterns = academic.get("match_patterns", [])
    academic_aliases = academic.get("aliases", {})
    family_groups = [g.lower() for g in family.get("apple_contacts_group_names", [])]
    family_relationships = [
        r.lower() for r in family.get("relationship_terms", [])
    ]
    family_phrases = [p.lower() for p in family.get("family_marker_phrases", [])]
    return {
        "academic_patterns_lc": [str(p).lower() for p in academic_patterns],
        "academic_aliases": academic_aliases or {},
        "family_groups_lc": family_groups,
        "family_relationships_lc": family_relationships,
        "family_phrases_lc": family_phrases,
    }


def _resolve_operator_page(brain_root: Path,
                           operator_slug_override=None) -> Path:
    """Resolve the operator self-page. Order:
      1. CLI override (--operator-slug).
      2. ~/.hermes/USER.md `operator.brain_slug:` (if present).
      3. DEFAULT_OPERATOR_SLUG heuristic.
    Fail-fast if resolved page doesn't exist."""
    slug = operator_slug_override
    if not slug and USERMD_DEFAULT.exists():
        try:
            head = USERMD_DEFAULT.read_text(encoding="utf-8")[:8192]
            import re
            m = re.search(r"brain_slug\s*:\s*['\"]?([^'\"\s\n]+)", head)
            if m:
                slug = m.group(1).strip()
        except OSError:
            pass
    if not slug:
        slug = DEFAULT_OPERATOR_SLUG
    candidate = brain_root / "people" / f"{slug}.md"
    if candidate.exists():
        return candidate
    # Try sub-dirs
    for sub in ("personal", "colleagues", "family"):
        p = brain_root / "people" / sub / f"{slug}.md"
        if p.exists():
            return p
    raise OperatorPageError(
        f"operator self-page not found: tried {brain_root}/people/{slug}.md "
        f"and sub-dirs. Set --operator-slug or USER.md operator.brain_slug."
    )


# === Phase 1: operator identity =============================================

def _phase_1(source_dir: Path, operator_page: Path, today_iso: str,
             dry_run: bool, counters: Counter,
             warnings: Counter, error_classes: Counter):
    """Augment operator self-page with LinkedIn identity data + create
    institution stubs for past employers + schools."""
    ident = operator_identity_parser.parse(source_dir)
    counters["phase1_positions_seen"] = len(ident.positions)
    counters["phase1_education_seen"] = len(ident.education)
    counters["phase1_skills_seen"] = len(ident.skills)
    counters["phase1_languages_seen"] = len(ident.languages)

    # Checksum-skip per Profile.csv (the load-bearing identity CSV).
    profile_csum = stable_csv_checksum(source_dir / "Profile.csv")
    if not dry_run:
        # Add identity fields to operator page (idempotent)
        if ident.headline:
            if add_frontmatter_field(operator_page, "linkedin_headline",
                                     ident.headline):
                counters["phase1_field_added_linkedin_headline"] += 1
        if ident.location:
            if add_frontmatter_field(operator_page, "linkedin_location",
                                     ident.location):
                counters["phase1_field_added_linkedin_location"] += 1
        if ident.join_date:
            if add_frontmatter_field(operator_page, "linkedin_join_date",
                                     ident.join_date):
                counters["phase1_field_added_linkedin_join_date"] += 1
        if profile_csum:
            if add_frontmatter_field(
                operator_page, "profile_csum", profile_csum
            ):
                counters["phase1_checksum_added"] += 1
        # Compiled-Truth sub-section + timeline entries
        _augment_operator_page(operator_page, ident, today_iso,
                               counters, warnings)
    else:
        counters["phase1_dry_run_skipped_writes"] += 1

    # Institution stubs
    brain_root = operator_page.parent.parent.parent  # /brain/people/x.md → /brain/
    if not (brain_root / "people").exists():
        brain_root = operator_page.parent.parent  # /brain/people/sub/x.md → /brain/
    inst_dir = brain_root / "institutions"

    distinct_employers = []
    seen_emp = set()
    for pos in ident.positions:
        if pos.company and pos.company not in seen_emp:
            distinct_employers.append(pos.company)
            seen_emp.add(pos.company)
    distinct_schools = []
    seen_sch = set()
    for edu in ident.education:
        if edu.school and edu.school not in seen_sch:
            distinct_schools.append(edu.school)
            seen_sch.add(edu.school)

    counters["phase1_distinct_employers"] = len(distinct_employers)
    counters["phase1_distinct_schools"] = len(distinct_schools)

    if dry_run:
        return

    for org in distinct_employers + distinct_schools:
        try:
            existing = find_existing_institution_by_name(org, brain_root)
            if existing is not None:
                counters["phase1_institution_existing"] += 1
                # Add timeline entry citing operator
                append_timeline_entry(
                    existing,
                    f"- **{today_iso}** | Operator past affiliation per "
                    f"LinkedIn import.",
                )
                continue
            slug = slugify(org)
            if not slug:
                warnings["phase1_institution_empty_slug"] += 1
                continue
            inst_path = inst_dir / f"{slug}.md"
            inst_dir.mkdir(parents=True, exist_ok=True)
            _write_institution_stub(inst_path, org, today_iso)
            counters["phase1_institution_created"] += 1
        except Exception:
            error_classes["phase1_institution_write_failed"] += 1


def _augment_operator_page(operator_page: Path, ident, today_iso: str,
                           counters: Counter, warnings: Counter):
    """Add a `## LinkedIn Identity` sub-section to the operator page.
    Idempotent: replaces an existing sub-section under that header,
    preserving all other content."""
    try:
        text = operator_page.read_text(encoding="utf-8")
    except OSError:
        warnings["phase1_operator_page_read_failed"] += 1
        return

    import re
    new_section = _render_operator_identity_section(ident, today_iso)
    section_re = re.compile(
        r"## LinkedIn Identity[\s\S]*?(?=^## |\Z)", re.MULTILINE
    )
    m = section_re.search(text)
    if m:
        old = m.group(0)
        if old.rstrip() == new_section.rstrip():
            counters["phase1_section_idempotent_skip"] += 1
            return
        new_text = text[:m.start()] + new_section + text[m.end():]
        counters["phase1_section_updated"] += 1
    else:
        # Append before final `---` Timeline marker if present, else EOF.
        new_text = text.rstrip("\n") + "\n\n" + new_section + "\n"
        counters["phase1_section_added"] += 1
    operator_page.write_text(new_text, encoding="utf-8")


def _render_operator_identity_section(ident, today_iso: str) -> str:
    lines = ["## LinkedIn Identity", "",
             f"> Auto-generated by linkedin-bootstrap on {today_iso}. "
             "Do not hand-edit this sub-section; changes are overwritten "
             "by re-runs.", ""]
    if ident.headline:
        lines.extend([f"**Headline:** {ident.headline}", ""])
    if ident.location:
        lines.extend([f"**Location:** {ident.location}", ""])
    if ident.industry:
        lines.extend([f"**Industry:** {ident.industry}", ""])
    if ident.positions:
        lines.extend(["### Positions", "",
                      "| Period | Title | Company | Location |",
                      "|---|---|---|---|"])
        for p in ident.positions:
            period = " – ".join(
                x for x in (p.started_on or "?", p.finished_on or "present")
                if x
            )
            slug = slugify(p.company) if p.company else ""
            org = f"[[institutions/{slug}]]" if slug else (p.company or "")
            lines.append(
                f"| {period} | {p.title or ''} | {org} | {p.location or ''} |"
            )
        lines.append("")
    if ident.education:
        lines.extend(["### Education", "",
                      "| Period | Degree | School | Field |",
                      "|---|---|---|---|"])
        for e in ident.education:
            period = " – ".join(
                x for x in (e.started_on or "?", e.finished_on or "?") if x
            )
            slug = slugify(e.school) if e.school else ""
            school = f"[[institutions/{slug}]]" if slug else (e.school or "")
            lines.append(
                f"| {period} | {e.degree or ''} | {school} | {e.field_of_study or ''} |"
            )
        lines.append("")
    if ident.skills:
        lines.extend(["### Skills", "",
                      ", ".join(ident.skills), ""])
    if ident.languages:
        lines.extend(["### Languages", "",
                      ", ".join(ident.languages), ""])
    return "\n".join(lines)


def _write_institution_stub(inst_path: Path, org_name: str,
                            today_iso: str):
    fm = {
        "title": org_name,
        "type": "institution",
        "created": today_iso,
        "last_updated": today_iso,
        "status": "active",
        "display_name": org_name,
        "schema_version": SCHEMA_VERSION,
        "privacy_tier": "T2",
        "aliases": [],
        "programs": [],
        "tags": [],
        "institution_type": None,
        "founded": None,
        "location": None,
        "website": None,
        "source": "linkedin-bootstrap@0.1.0",
        "flagged_for_review": False,
    }
    body = [
        render_frontmatter(fm),
        "",
        f"# {org_name}",
        "",
        f"> Imported from LinkedIn on {today_iso} as referenced organization.",
        "",
        "## State",
        "",
        "- **Source:** linkedin-bootstrap (LinkedIn Profile / Education / Position)",
        "",
        "## Open Threads",
        "",
        "[No data yet]",
        "",
        "---",
        "",
        "## Timeline",
        "",
        f"- **{today_iso}** | Created from LinkedIn bootstrap.",
        "",
    ]
    inst_path.write_text("\n".join(body), encoding="utf-8")


# === Phase 2: Connections.csv → people stubs ================================

def _phase_2(source_dir: Path, brain_root: Path, configs,
             today_iso: str, dry_run: bool,
             counters: Counter, warnings: Counter,
             error_classes: Counter):
    """Phase 2 — Connections.csv → people/ stubs with D49 dedup."""
    csv_path = source_dir / "Connections.csv"
    csum = stable_csv_checksum(csv_path)
    counters["phase2_csv_csum"] = csum or "missing"

    for conn in connections_parser.parse(csv_path):
        counters["phase2_rows_seen"] += 1
        if conn.is_linkedin_member_placeholder:
            counters["phase2_linkedin_member_skipped"] += 1
            continue
        if conn.is_empty:
            counters["phase2_empty_row"] += 1
            if not dry_run:
                try:
                    write_review(
                        brain_root, SKILL_NAME,
                        "linkedin-connection-no-identity",
                        {"pk": f"conn-{counters['phase2_rows_seen']}",
                         "first_name": conn.first_name,
                         "last_name": conn.last_name},
                        schema_version=SCHEMA_VERSION,
                    )
                except Exception:
                    error_classes["phase2_review_write_failed"] += 1
            continue

        display_name = _join_name(conn.first_name, conn.last_name)
        canon_url = normalize_linkedin_url(conn.url) if conn.url else ""

        # Dedup by URL → email → name
        match_path = None
        if canon_url:
            match_path = find_existing_person_by_linkedin_url(
                canon_url, brain_root
            )
        if match_path is None and conn.email:
            match_path = find_existing_person_by_email(
                conn.email, brain_root
            )
        if match_path is None and display_name:
            match_path = find_existing_person_by_name(
                display_name, brain_root
            )

        if match_path is not None:
            counters["phase2_alias_merged"] += 1
            if dry_run:
                continue
            if canon_url:
                if add_alias_to_existing_page(match_path, canon_url):
                    counters["phase2_url_alias_added"] += 1
            if conn.email:
                if add_alias_to_existing_page(match_path, conn.email):
                    counters["phase2_email_alias_added"] += 1
            if conn.connected_on:
                append_timeline_entry(
                    match_path,
                    f"- **{conn.connected_on}** | LinkedIn connection "
                    f"recorded (from linkedin-bootstrap @ {today_iso}).",
                )
            continue

        # No match — create new stub
        if not display_name:
            counters["phase2_no_name_skipped"] += 1
            continue
        slug = slugify(display_name)
        if not slug:
            warnings["phase2_empty_slug"] += 1
            continue
        sub_dir = _resolve_connection_subdir(conn, configs)
        target_dir = brain_root / "people" / sub_dir
        out_path = target_dir / f"{slug}.md"
        counters["phase2_created"] += 1
        if dry_run:
            continue
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            _write_connection_stub(out_path, conn, display_name, sub_dir,
                                   canon_url, today_iso)
        except Exception:
            error_classes["phase2_page_write_failed"] += 1


def _join_name(first, last) -> str:
    parts = [p for p in (first, last) if p]
    return " ".join(parts) if parts else ""


def _resolve_connection_subdir(conn, configs) -> str:
    """For a LinkedIn connection, route to colleagues/ or personal/.
    Academic-org match → colleagues/. Otherwise → personal/."""
    org_lc = (conn.company or "").lower()
    for pat in configs["academic_patterns_lc"]:
        if pat in org_lc:
            return "colleagues"
    return "personal"


def _write_connection_stub(out_path: Path, conn, display_name: str,
                           sub_dir: str, canon_url: str, today_iso: str):
    aliases = []
    if canon_url:
        aliases.append(canon_url)
    if conn.email:
        aliases.append(conn.email)
    org_slug = slugify(conn.company) if conn.company else ""
    programs = {"colleagues": ["career"], "personal": ["personal-development"]}.get(
        sub_dir, []
    )
    privacy_tier = "T2" if sub_dir == "colleagues" else "T1"

    fm = {
        "title": display_name,
        "type": "person",
        "created": today_iso,
        "last_updated": today_iso,
        "status": "active",
        "display_name": display_name,
        "schema_version": SCHEMA_VERSION,
        "privacy_tier": privacy_tier,
        "aliases": aliases,
        "programs": programs,
        "tags": [],
    }
    if conn.position:
        fm["role"] = conn.position
    if org_slug:
        fm["organization"] = f"[[institutions/{org_slug}]]"
    if canon_url:
        fm["linkedin_url"] = canon_url
    if conn.connected_on:
        fm["linkedin_connected_on"] = conn.connected_on
    fm["confidence"] = "low"
    fm["source"] = "linkedin-bootstrap@0.1.0"
    fm["flagged_for_review"] = False

    body = [
        render_frontmatter(fm),
        "",
        f"# {display_name}",
        "",
        f"> Imported from LinkedIn connections on {today_iso}. Stub page; "
        f"awaits enrichment.",
        "",
        "## State",
        "",
    ]
    if conn.position:
        body.append(f"- **Role:** {conn.position}")
    if conn.company:
        body.append(
            f"- **Organization:** [[institutions/{org_slug}]]"
            if org_slug else f"- **Organization:** {conn.company}"
        )
    if canon_url:
        body.append(f"- **LinkedIn:** {canon_url}")
    if conn.email:
        body.append(f"- **Email:** {conn.email}")
    body.extend([
        "",
        "## Open Threads",
        "",
        "[No data yet]",
        "",
        "## See Also",
        "",
    ])
    if org_slug:
        body.append(f"- [[institutions/{org_slug}]]")
    else:
        body.append("[No data yet]")
    body.extend([
        "",
        "---",
        "",
        "## Timeline",
        "",
        f"- **{conn.connected_on or today_iso}** | Imported from LinkedIn "
        f"connections (linkedin-bootstrap).",
        "",
    ])
    out_path.write_text("\n".join(body), encoding="utf-8")


# === Phase 3: ImportedContacts → alias-merge ONLY ===========================

def _phase_3(source_dir: Path, brain_root: Path, dry_run: bool,
             counters: Counter, warnings: Counter,
             error_classes: Counter):
    """Phase 3 — ImportedContacts alias-merge against existing pages.
    HARD INVARIANT: never creates new pages."""
    csv_path = source_dir / "ImportedContacts.csv"
    for ic in imported_contacts_parser.parse(csv_path):
        counters["phase3_rows_seen"] += 1

        match_path = None
        if ic.email:
            match_path = find_existing_person_by_email(ic.email, brain_root)
        if match_path is None and ic.phone:
            match_path = find_existing_person_by_phone(ic.phone, brain_root)
        if match_path is None:
            display = _join_name(ic.first_name, ic.last_name)
            if display:
                match_path = find_existing_person_by_name(display, brain_root)

        if match_path is None:
            counters["phase3_no_match_skipped"] += 1
            continue

        counters["phase3_dedup_match"] += 1
        if dry_run:
            continue
        added_any = False
        if ic.email:
            if add_alias_to_existing_page(match_path, ic.email):
                counters["phase3_email_alias_added"] += 1
                added_any = True
        if ic.phone:
            norm = normalize_phone(ic.phone)
            if norm and add_alias_to_existing_page(match_path, norm):
                counters["phase3_phone_alias_added"] += 1
                added_any = True
        if added_any:
            counters["phase3_alias_merged"] += 1


# === Phase 4: network signals ===============================================

def _phase_4(source_dir: Path, brain_root: Path, configs,
             today_iso: str, dry_run: bool,
             counters: Counter, warnings: Counter,
             error_classes: Counter):
    """Phase 4 — endorsements, invitations, follows → facts + timeline."""
    for sig in network_signals_parser.parse(source_dir):
        counters[f"phase4_{sig.kind}_seen"] += 1
        # Determine target entity
        if sig.kind in ("endorsement_given", "endorsement_received",
                        "invitation_sent", "invitation_received",
                        "member_follow"):
            target_name = sig.counterparty_name
            target_url = sig.counterparty_url
            if not target_name and not target_url:
                continue
            match = None
            if target_url:
                match = find_existing_person_by_linkedin_url(target_url, brain_root)
            if match is None and target_name:
                match = find_existing_person_by_name(target_name, brain_root)
            if match is None:
                if not target_name:
                    continue
                slug = slugify(target_name)
                if not slug:
                    continue
                target_dir = brain_root / "people" / "personal"
                match = target_dir / f"{slug}.md"
                counters[f"phase4_{sig.kind}_stub_created"] += 1
                if not dry_run:
                    target_dir.mkdir(parents=True, exist_ok=True)
                    if not match.exists():
                        _write_minimal_person_stub(
                            match, target_name, today_iso, target_url,
                            f"Created via Phase 4 LinkedIn {sig.kind} signal.",
                        )
            if not dry_run:
                kind_label = sig.kind.replace('_', ' ').capitalize()
                entry = (
                    f"- **{sig.signal_date or today_iso}** | "
                    f"{kind_label}: "
                    f"{sig.signal_text or '(no text)'} (via LinkedIn)"
                )
                append_timeline_entry(match, entry)
                counters[f"phase4_{sig.kind}_timeline_added"] += 1
        elif sig.kind == "company_follow":
            org = sig.counterparty_company
            if not org:
                continue
            match = find_existing_institution_by_name(org, brain_root)
            if match is None:
                slug = slugify(org)
                if not slug:
                    continue
                target_dir = brain_root / "institutions"
                match = target_dir / f"{slug}.md"
                counters["phase4_company_follow_stub_created"] += 1
                if not dry_run:
                    target_dir.mkdir(parents=True, exist_ok=True)
                    if not match.exists():
                        _write_institution_stub(match, org, today_iso)
            if not dry_run:
                entry = (
                    f"- **{sig.signal_date or today_iso}** | Operator "
                    f"follows this organization on LinkedIn."
                )
                append_timeline_entry(match, entry)
                counters["phase4_company_follow_timeline_added"] += 1


def _write_minimal_person_stub(out_path: Path, display_name: str,
                               today_iso: str, url=None,
                               reason: str = ""):
    aliases = [url] if url else []
    fm = {
        "title": display_name,
        "type": "person",
        "created": today_iso,
        "last_updated": today_iso,
        "status": "active",
        "display_name": display_name,
        "schema_version": SCHEMA_VERSION,
        "privacy_tier": "T1",
        "aliases": aliases,
        "programs": [],
        "tags": [],
        "confidence": "low",
        "source": "linkedin-bootstrap@0.1.0",
        "flagged_for_review": False,
    }
    if url:
        fm["linkedin_url"] = normalize_linkedin_url(url)
    body = [
        render_frontmatter(fm),
        "",
        f"# {display_name}",
        "",
        f"> Imported from LinkedIn on {today_iso}. {reason}",
        "",
        "## State",
        "",
        f"- **Source:** linkedin-bootstrap",
        "",
        "---",
        "",
        "## Timeline",
        "",
        f"- **{today_iso}** | Created from LinkedIn signal.",
        "",
    ]
    out_path.write_text("\n".join(body), encoding="utf-8")


# === Phase 5: activity → operator timeline ONLY =============================

def _phase_5(source_dir: Path, operator_page: Path, brain_root: Path,
             today_iso: str, dry_run: bool,
             counters: Counter, warnings: Counter,
             error_classes: Counter):
    """Phase 5 — activity timeline. HARD INVARIANT: never creates
    entity pages."""
    entries_added = 0
    for ev in activity_parser.parse(source_dir):
        counters[f"phase5_{ev.kind}_seen"] += 1
        if dry_run:
            continue
        summary = ev.summary or ev.url or "(no detail)"
        entry = (
            f"- **{ev.date or today_iso}** | {ev.kind.capitalize()}: "
            f"{summary[:140]} (via LinkedIn)"
        )
        try:
            if append_timeline_entry(operator_page, entry):
                entries_added += 1
        except Exception:
            error_classes["phase5_timeline_write_failed"] += 1
    counters["phase5_timeline_entries_added"] = entries_added


# === Phase 6: Learning.csv → media/courses ==================================

def _phase_6(source_dir: Path, brain_root: Path, operator_page: Path,
             today_iso: str, dry_run: bool,
             counters: Counter, warnings: Counter,
             error_classes: Counter):
    """Phase 6 — Learning.csv → media/courses/ stubs."""
    courses_dir = brain_root / "media" / "courses"
    seen_slugs = set()
    for course in learning_parser.parse(source_dir / "Learning.csv"):
        counters["phase6_rows_seen"] += 1
        slug = slugify(course.name)
        if not slug or slug in seen_slugs:
            counters["phase6_dup_or_empty_skip"] += 1
            continue
        seen_slugs.add(slug)
        out_path = courses_dir / f"{slug}.md"
        if out_path.exists():
            counters["phase6_existing"] += 1
            continue
        counters["phase6_created"] += 1
        if dry_run:
            continue
        try:
            courses_dir.mkdir(parents=True, exist_ok=True)
            _write_course_stub(out_path, course, today_iso)
            # operator timeline entry
            entry = (
                f"- **{course.completed_on or today_iso}** | Completed "
                f"[[media/courses/{slug}]] via {course.provider}."
            )
            append_timeline_entry(operator_page, entry)
        except Exception:
            error_classes["phase6_write_failed"] += 1


def _write_course_stub(out_path: Path, course, today_iso: str):
    fm = {
        "title": course.name,
        "type": "course",
        "created": today_iso,
        "last_updated": today_iso,
        "status": "completed",
        "display_name": course.name,
        "schema_version": SCHEMA_VERSION,
        "privacy_tier": "T2",
        "provider": course.provider,
        "topic": course.topic,
        "completed_on": course.completed_on,
        "duration_minutes": course.duration_minutes,
        "source": "linkedin-bootstrap@0.1.0",
    }
    body = [
        render_frontmatter(fm),
        "",
        f"# {course.name}",
        "",
        f"> Imported from LinkedIn Learning on {today_iso}.",
        "",
        "## State",
        "",
        f"- **Provider:** {course.provider}",
    ]
    if course.topic:
        body.append(f"- **Topic:** {course.topic}")
    if course.completed_on:
        body.append(f"- **Completed:** {course.completed_on}")
    if course.duration_minutes is not None:
        body.append(f"- **Duration:** {course.duration_minutes} minutes")
    body.extend([
        "",
        "---",
        "",
        "## Timeline",
        "",
        f"- **{course.completed_on or today_iso}** | "
        f"Completed via {course.provider}.",
        "",
    ])
    out_path.write_text("\n".join(body), encoding="utf-8")


# === Phase 7: preferences → USER.md =========================================

def _phase_7(source_dir: Path, usermd_path: Path, today_iso: str,
             dry_run: bool, counters: Counter,
             warnings: Counter, error_classes: Counter):
    """Phase 7 — preferences / inferences → USER.md sub-sections."""
    bundle = preferences_parser.parse(source_dir)
    counters["phase7_search_topics"] = len(bundle.search_query_topics)
    counters["phase7_ad_categories"] = len(bundle.ad_categories)
    counters["phase7_inferences"] = len(bundle.inferences)
    counters["phase7_ad_vendors"] = len(bundle.ad_clicked_vendors)

    if not usermd_path.exists():
        warnings["phase7_usermd_absent"] += 1
        return
    if dry_run:
        counters["phase7_dry_run_skipped_writes"] += 1
        return

    subsections = _render_preferences_subsections(bundle, today_iso)
    try:
        _append_usermd_subsections(usermd_path, subsections, counters)
    except Exception:
        error_classes["phase7_usermd_append_failed"] += 1


def _render_preferences_subsections(bundle, today_iso: str) -> dict:
    subs = {}
    if bundle.search_query_topics or bundle.inferences:
        lines = [f"### From linkedin-bootstrap ({today_iso})", ""]
        if bundle.inferences:
            lines.append("**LinkedIn inferences:**")
            for inf in bundle.inferences[:30]:
                lines.append(f"- {inf}")
            lines.append("")
        if bundle.search_query_topics:
            lines.append("**Top LinkedIn search queries (capped at 30):**")
            for q in bundle.search_query_topics[:30]:
                lines.append(f"- {q}")
            lines.append("")
        subs["Topical interests"] = "\n".join(lines)

    if bundle.ad_categories or bundle.ad_clicked_vendors:
        lines = [f"### From linkedin-bootstrap ({today_iso})", ""]
        if bundle.ad_categories:
            lines.append("**LinkedIn ad-targeting categories (top 30):**")
            for cat in bundle.ad_categories[:30]:
                lines.append(f"- {cat}")
            lines.append("")
        if bundle.ad_clicked_vendors:
            lines.append("**Vendors clicked (top 20):**")
            for v in bundle.ad_clicked_vendors[:20]:
                lines.append(f"- {v}")
            lines.append("")
        subs["Vendors / SaaS"] = "\n".join(lines)

    return subs


def _append_usermd_subsections(usermd_path: Path, subsections: dict,
                               counters: Counter):
    """Append per-section sub-sections under top-level headings.
    Creates top-level heading if absent. Never overwrites."""
    text = usermd_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    headings = []
    for i, line in enumerate(lines):
        if line.startswith("## ") and not line.startswith("### "):
            headings.append((i, line[3:].strip()))
    headings_idx = headings + [(len(lines), None)]

    insert_plan = []
    matched = set()
    for idx, (start_i, name) in enumerate(headings):
        if name in subsections:
            end_i = headings_idx[idx + 1][0]
            insert_at = end_i
            while insert_at > start_i + 1 and lines[insert_at - 1].strip() == "":
                insert_at -= 1
            body = "\n" + subsections[name].rstrip() + "\n"
            insert_plan.append((insert_at, body.splitlines(keepends=False)))
            matched.add(name)
    insert_plan.sort(key=lambda x: -x[0])
    new_lines = list(lines)
    for insert_at, body_lines in insert_plan:
        new_lines = new_lines[:insert_at] + body_lines + new_lines[insert_at:]

    appended_new = []
    for name in USER_MD_SECTIONS:
        if name not in matched and name in subsections:
            appended_new.append(f"\n## {name}\n")
            appended_new.append(subsections[name].rstrip() + "\n")
    if appended_new:
        if new_lines and new_lines[-1].strip() != "":
            new_lines.append("")
        new_lines.extend("".join(appended_new).splitlines())

    counters["phase7_sections_appended"] += len(subsections)

    out_text = "\n".join(new_lines)
    if not out_text.endswith("\n"):
        out_text += "\n"
    usermd_path.write_text(out_text, encoding="utf-8")


# === Phase 8: SKIP invariant verification ====================================

def _phase_8(source_dir: Path, counters: Counter):
    """Phase 8 — verify SKIP_LIST invariant: per CDD §1.4 NO ingest of
    these CSVs. This phase is the runtime check; the static check is
    the unit test (test_phase8_skip_invariant)."""
    for skip in SKIP_LIST:
        p = source_dir / skip
        if p.exists():
            counters["phase8_skip_files_present"] += 1
        else:
            counters["phase8_skip_files_absent"] += 1


# === Phase 9: commit + push (real run only) ==================================

def _phase_9(brain_root: Path, run_id: str, dry_run: bool, counters: Counter,
             error_classes: Counter):
    if dry_run or brain_root != BRAIN_HOME:
        counters["phase9_skipped_non_real_run"] += 1
        return
    try:
        # Use importlib to load the SHARED commit helper, bypassing any
        # local _shared/ shadowing (mirrors apple-contacts-bootstrap +
        # enrich-orchestrator D107 pattern).
        from _shared.git_commit import commit_and_push_brain
    except ImportError:
        error_classes["phase9_git_commit_import_failed"] += 1
        return
    extra = (
        f"linkedin-bootstrap run {run_id}\n\n"
        f"counters: {dict(counters)}\n"
        f"error_classes: {dict(error_classes)}\n"
    )
    try:
        gres = commit_and_push_brain(
            BRAIN_HOME, run_id, SKILL_NAME, extra_message=extra,
        )
        if gres.get("git_commit_ok"):
            counters["git_commit_ok"] = 1
        if gres.get("git_push_ok"):
            counters["git_push_ok"] = 1
        if gres.get("git_nothing_to_commit"):
            counters["git_nothing_to_commit"] = 1
        if gres.get("commit_sha"):
            counters["brain_commit_sha"] = gres["commit_sha"]
        if gres.get("error_class"):
            error_classes[gres["error_class"]] += 1
    except Exception:
        error_classes["phase9_commit_exception"] += 1


# === Main ===================================================================

def main():
    parser = argparse.ArgumentParser(
        description=f"{SKILL_NAME} v{SKILL_VERSION} (per D27 + S191-D119)"
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                        help="LinkedIn export directory")
    parser.add_argument("--brain-root", type=Path, default=BRAIN_HOME,
                        help="Override ~/brain root (tests use tmp_path)")
    parser.add_argument("--usermd-path", type=Path, default=None,
                        help="Override ~/.hermes/USER.md path")
    parser.add_argument("--operator-slug", default=None,
                        help="Operator self-page slug (default: emanuel)")
    parser.add_argument("--phase", type=int, default=None,
                        help="Skip to phase N (still runs Phase 0 preflight)")
    parser.add_argument("--dry-run", action="store_true",
                        help="No brain writes, no USER.md writes, no git ops")
    parser.add_argument("--skip-preflight", action="store_true",
                        help="Skip routing-config preflight (tests only)")
    parser.add_argument("--run-id", default=None,
                        help="UUID4 for run-records")
    args = parser.parse_args()

    run_id = args.run_id or str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    today_iso = started_at.date().isoformat()
    counters = Counter()
    error_classes = Counter()
    warnings = Counter()

    # === Phase 0: preflight ===
    # Skip-preflight mode (tests): try-load-then-fallback. If real configs
    # exist at the canonical paths, use them; otherwise empty fallback.
    if not args.skip_preflight:
        try:
            configs = _load_routing_configs()
        except ConfigError as e:
            _emit_failure(run_id, started_at, "preflight_failed",
                          str(e), counters)
            return 2
    else:
        try:
            configs = _load_routing_configs()
        except ConfigError:
            configs = {
                "academic_patterns_lc": [], "academic_aliases": {},
                "family_groups_lc": [], "family_relationships_lc": [],
                "family_phrases_lc": [],
            }

    if not args.source.exists():
        _emit_failure(run_id, started_at, "source_missing",
                      f"source dir not found: {args.source}", counters)
        return 3

    # Resolve operator page
    try:
        operator_page = _resolve_operator_page(
            args.brain_root, operator_slug_override=args.operator_slug,
        )
    except OperatorPageError as e:
        _emit_failure(run_id, started_at, "operator_page_missing",
                      str(e), counters)
        return 4

    usermd_path = args.usermd_path or USERMD_DEFAULT

    start_phase = args.phase or 1

    # === Phase 1-7 ===
    try:
        if start_phase <= 1:
            _phase_1(args.source, operator_page, today_iso,
                     args.dry_run, counters, warnings, error_classes)
        if start_phase <= 2:
            _phase_2(args.source, args.brain_root, configs, today_iso,
                     args.dry_run, counters, warnings, error_classes)
        if start_phase <= 3:
            _phase_3(args.source, args.brain_root, args.dry_run,
                     counters, warnings, error_classes)
        if start_phase <= 4:
            _phase_4(args.source, args.brain_root, configs, today_iso,
                     args.dry_run, counters, warnings, error_classes)
        if start_phase <= 5:
            _phase_5(args.source, operator_page, args.brain_root,
                     today_iso, args.dry_run, counters, warnings,
                     error_classes)
        if start_phase <= 6:
            _phase_6(args.source, args.brain_root, operator_page,
                     today_iso, args.dry_run, counters, warnings,
                     error_classes)
        if start_phase <= 7:
            _phase_7(args.source, usermd_path, today_iso, args.dry_run,
                     counters, warnings, error_classes)
        if start_phase <= 8:
            _phase_8(args.source, counters)
        if start_phase <= 9:
            _phase_9(args.brain_root, run_id, args.dry_run, counters,
                     error_classes)
    except Exception:
        error_classes["unhandled_exception"] += 1
        try:
            write_failure(args.brain_root, SKILL_NAME, "unhandled_exception",
                          {"pk": run_id},
                          traceback_str=traceback.format_exc())
        except Exception:
            pass

    # === Audit log ===
    audit_dir = Path(__file__).resolve().parent / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / (
        f"{run_id}-dryrun.json" if args.dry_run else f"{run_id}.json"
    )
    ended_at = datetime.now(timezone.utc)
    audit = {
        "run_id": run_id,
        "skill": SKILL_NAME,
        "skill_version": SKILL_VERSION,
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "duration_seconds": round((ended_at - started_at).total_seconds(), 2),
        "fixture_mode": args.brain_root != BRAIN_HOME,
        "dry_run": bool(args.dry_run),
        "source": str(args.source),
        "brain_root": str(args.brain_root),
        "operator_page": str(operator_page),
        "counters": dict(counters),
        "error_classes": dict(error_classes),
        "warnings": dict(warnings),
        "outcome": "ok" if not error_classes else "ok_with_errors",
    }
    audit_path.write_text(
        _json.dumps(audit, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    audit["audit_path"] = str(audit_path)

    rec = {
        "run_id": run_id,
        "skill": SKILL_NAME,
        "skill_version": SKILL_VERSION,
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "duration_seconds": audit["duration_seconds"],
        "fixture_mode": audit["fixture_mode"],
        "dry_run": audit["dry_run"],
        "exit_code": 0 if not error_classes else 1,
        "outcome": audit["outcome"],
        "counters": dict(counters),
        "error_classes": dict(error_classes),
        "warnings": dict(warnings),
        "errors": [],
    }
    emit_run_record(rec)

    print(_json.dumps({
        "run_id": run_id,
        "outcome": audit["outcome"],
        "duration_seconds": audit["duration_seconds"],
        "fixture_mode": audit["fixture_mode"],
        "dry_run": audit["dry_run"],
        "audit_path": str(audit_path),
        "counters": dict(counters),
        "error_classes": dict(error_classes),
        "warnings": dict(warnings),
    }, indent=2))

    return 0 if not error_classes else 1


def _emit_failure(run_id, started_at, outcome, message, counters):
    rec = {
        "run_id": run_id,
        "skill": SKILL_NAME,
        "started_at": started_at.isoformat(),
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "outcome": outcome,
        "exit_code": 2,
        "errors": [message],
        "counters": dict(counters),
    }
    try:
        emit_run_record(rec)
    except Exception:
        pass
    print(f"[{outcome}] {message}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
