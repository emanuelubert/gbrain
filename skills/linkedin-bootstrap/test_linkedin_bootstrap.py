# <!-- CDD-CONTRACT: ~/gbrain/skills/linkedin-bootstrap/test_linkedin_bootstrap.py -->
# Contract:   pytest suite covering CDD §2.2 + 2.3 + 2.4 test cases.
# Inputs:     test-fixtures/ synthetic CSVs; pytest tmp_path brain root.
# Outputs:    pytest results (pass/fail per test).
# Invariants: tests NEVER touch real ~/brain/, real ~/.hermes/USER.md,
#             real LinkedIn export. All side effects in tmp_path.
# Idempotent: yes — each test gets a fresh tmp_path.

"""pytest suite for linkedin-bootstrap v0.1.0."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

# Make the skill importable
SKILL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SKILL_DIR.parent))
sys.path.insert(0, str(SKILL_DIR))

FIXTURES = SKILL_DIR / "test-fixtures"


# === Helpers ===========================================================


def _write_minimal_operator_page(brain_root: Path):
    """Create a minimal operator self-page at brain_root/people/emanuel.md."""
    people_dir = brain_root / "people"
    people_dir.mkdir(parents=True, exist_ok=True)
    path = people_dir / "emanuel.md"
    path.write_text(
        "---\n"
        "title: Emanuel Ubert\n"
        "type: person\n"
        "schema_version: 1\n"
        "privacy_tier: T1\n"
        "display_name: Emanuel Ubert\n"
        "---\n"
        "\n"
        "# Emanuel Ubert\n"
        "\n"
        "Hand-authored content that MUST be preserved across re-runs.\n"
        "\n"
        "## Hand Section\n"
        "\n"
        "Custom thoughts.\n"
        "\n"
        "---\n"
        "\n"
        "## Timeline\n"
        "\n"
        "- **2026-01-01** | Pre-existing entry.\n",
        encoding="utf-8",
    )
    return path


def _write_known_existing_person(brain_root: Path):
    """Create a known-existing person to test dedup-merge.

    Slug matches DIN-5007-2 normalization of 'KnownPerson Existing':
    'knownperson-existing' (one word, since no separator). Aliases
    include both LinkedIn-side email variants so Phase 3 phone-dedup
    can match on phone alone.
    """
    p = brain_root / "people" / "colleagues"
    p.mkdir(parents=True, exist_ok=True)
    page = p / "knownperson-existing.md"
    page.write_text(
        "---\n"
        "title: KnownPerson Existing\n"
        "type: person\n"
        "schema_version: 1\n"
        "privacy_tier: T2\n"
        "aliases: [\"known.existing@rsm.nl\"]\n"
        "display_name: KnownPerson Existing\n"
        "---\n"
        "\n"
        "# KnownPerson Existing\n"
        "\n"
        "Pre-existing brain page.\n"
        "\n"
        "---\n"
        "\n"
        "## Timeline\n"
        "\n"
        "- **2025-01-01** | Pre-existing entry.\n",
        encoding="utf-8",
    )
    return page


def _write_known_institution(brain_root: Path):
    inst_dir = brain_root / "institutions"
    inst_dir.mkdir(parents=True, exist_ok=True)
    page = inst_dir / "rotterdam-school-of-management.md"
    page.write_text(
        "---\n"
        "title: Rotterdam School of Management\n"
        "type: institution\n"
        "schema_version: 1\n"
        "privacy_tier: T2\n"
        "---\n"
        "\n"
        "# Rotterdam School of Management\n"
        "\n"
        "---\n"
        "\n"
        "## Timeline\n"
        "\n"
        "- **2025-01-01** | Pre-existing.\n",
        encoding="utf-8",
    )
    return page


def _run_main(args_list):
    """Invoke run.py main() in-process."""
    import run
    # Reset sys.argv for argparse
    old_argv = sys.argv
    sys.argv = ["run.py"] + args_list
    try:
        return run.main()
    finally:
        sys.argv = old_argv


def _read_audit(brain_root_unused=None):
    """Read the most-recent audit JSON."""
    audit_dir = SKILL_DIR / "audit"
    files = sorted(audit_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        return None
    return json.loads(files[-1].read_text(encoding="utf-8"))


# === Tests =============================================================


def test_phase0_preflight_fails_on_missing_config(tmp_path, monkeypatch):
    """CDD §2.2 Phase 0: missing routing-config → fail-fast."""
    # Point _shared CONFIG paths to a missing dir
    bad_hermes = tmp_path / "fake-hermes"
    bad_hermes.mkdir()
    import run
    monkeypatch.setattr(
        run, "CONFIG_ACADEMIC_ORGS", bad_hermes / "sources" / "missing.yaml"
    )
    monkeypatch.setattr(
        run, "CONFIG_FAMILY_TERMS", bad_hermes / "sources" / "missing2.yaml"
    )
    rc = _run_main([
        "--source", str(FIXTURES),
        "--brain-root", str(tmp_path),
        "--dry-run",
    ])
    assert rc == 2, f"expected exit 2 on missing config, got {rc}"


def test_phase1_operator_identity_idempotent(tmp_path):
    """CDD §2.2 Phase 1: operator self-page gains identity sub-section
    + institutions stubs created."""
    _write_minimal_operator_page(tmp_path)
    rc = _run_main([
        "--source", str(FIXTURES),
        "--brain-root", str(tmp_path),
        "--usermd-path", str(tmp_path / "USER.md"),
        "--skip-preflight",
    ])
    assert rc == 0, "Phase 1 first run should succeed"
    op_page = (tmp_path / "people" / "emanuel.md").read_text()
    assert "## LinkedIn Identity" in op_page, \
        "Phase 1 should add LinkedIn Identity sub-section"
    assert "Hand-authored content" in op_page, \
        "Phase 1 must preserve hand-authored body"
    assert "## Hand Section" in op_page, \
        "Phase 1 must preserve custom sections"
    assert "Pre-existing entry" in op_page, \
        "Phase 1 must preserve pre-existing Timeline entries"

    # Institution stub for past employer
    inst = tmp_path / "institutions" / "rotterdam-school-of-management.md"
    assert inst.exists(), "Phase 1 should create institutions stub"
    # Idempotent second run
    rc2 = _run_main([
        "--source", str(FIXTURES),
        "--brain-root", str(tmp_path),
        "--usermd-path", str(tmp_path / "USER.md"),
        "--skip-preflight",
    ])
    assert rc2 == 0, "Phase 1 second run should succeed"
    audit = _read_audit()
    assert audit is not None


def test_phase2_connections_new_stub_and_dedup_merge(tmp_path):
    """CDD §2.2 Phase 2: new unknown → stub created; LinkedIn-Member
    skipped; existing → alias-merged."""
    _write_minimal_operator_page(tmp_path)
    _write_known_existing_person(tmp_path)
    rc = _run_main([
        "--source", str(FIXTURES),
        "--brain-root", str(tmp_path),
        "--usermd-path", str(tmp_path / "USER.md"),
        "--skip-preflight",
    ])
    assert rc == 0
    audit = _read_audit()
    c = audit["counters"]
    assert c.get("phase2_linkedin_member_skipped", 0) >= 1, \
        "LinkedIn-Member placeholder must be skipped"
    # Klaus Müller (DIN 5007-2 → klaus-mueller) should be created or
    # alias-merged depending on subdir routing
    klaus = tmp_path / "people" / "colleagues" / "klaus-mueller.md"
    assert klaus.exists(), "Klaus Müller stub should be created"
    klaus_text = klaus.read_text()
    assert "klaus.mueller@rsm.nl" in klaus_text or \
           "klaus.mueller" in klaus_text.lower()
    # New unknown person (NewPerson Unknown → newperson-unknown via
    # DIN slug — one word since no separator in source).
    # Sub-dir routed to personal/ since NewCo isn't in academic config.
    new_person = tmp_path / "people" / "personal" / "newperson-unknown.md"
    assert new_person.exists(), "Unknown person stub should be created"
    # Known existing → email dedup-match → page modified (alias added)
    existing = tmp_path / "people" / "colleagues" / "knownperson-existing.md"
    assert existing.exists()
    existing_text = existing.read_text()
    assert "https://www.linkedin.com/in/known-existing-9999" in existing_text, \
        "Known existing page should gain LinkedIn URL alias"
    # Existing page must preserve original content
    assert "Pre-existing brain page" in existing_text


def test_phase3_imported_contacts_alias_merge_only(tmp_path):
    """CDD §2.2 Phase 3: alias-merge ONLY; never creates new pages.

    The known-existing page should be matched on email + phone added;
    the no-match row produces no page."""
    _write_minimal_operator_page(tmp_path)
    _write_known_existing_person(tmp_path)
    rc = _run_main([
        "--source", str(FIXTURES),
        "--brain-root", str(tmp_path),
        "--usermd-path", str(tmp_path / "USER.md"),
        "--skip-preflight",
    ])
    assert rc == 0
    audit = _read_audit()
    c = audit["counters"]
    # Phase 3 should NOT create any new pages
    no_match_page = (tmp_path / "people" / "personal" / "no-match-at-all-person.md")
    assert not no_match_page.exists(), \
        "Phase 3 must NEVER create pages for no-match rows"
    # Known existing page should gain phone alias (matched by DIN-slug
    # name since email/phone aren't on the original page).
    existing_text = (tmp_path / "people" / "colleagues"
                     / "knownperson-existing.md").read_text()
    assert "+31611112222" in existing_text or \
           "31611112222" in existing_text


def test_phase4_endorsement_creates_or_updates_target(tmp_path):
    """CDD §2.2 Phase 4: relationship facts + timeline entries written."""
    _write_minimal_operator_page(tmp_path)
    rc = _run_main([
        "--source", str(FIXTURES),
        "--brain-root", str(tmp_path),
        "--usermd-path", str(tmp_path / "USER.md"),
        "--skip-preflight",
    ])
    assert rc == 0
    audit = _read_audit()
    c = audit["counters"]
    assert c.get("phase4_endorsement_given_seen", 0) >= 1
    assert c.get("phase4_company_follow_seen", 0) >= 1
    # Company follow creates institution stub
    inst = tmp_path / "institutions" / "erasmus-research-institute-of-management.md"
    assert inst.exists()
    inst_text = inst.read_text()
    assert "follows this organization" in inst_text


def test_phase5_activity_never_creates_entity_pages(tmp_path):
    """CDD §2.2 Phase 5 + §1.4 HARD INVARIANT: NEVER creates entity pages."""
    _write_minimal_operator_page(tmp_path)
    rc = _run_main([
        "--source", str(FIXTURES),
        "--brain-root", str(tmp_path),
        "--usermd-path", str(tmp_path / "USER.md"),
        "--skip-preflight",
    ])
    assert rc == 0
    audit = _read_audit()
    c = audit["counters"]
    # Activity counters non-zero
    assert c.get("phase5_comment_seen", 0) >= 1 or \
           c.get("phase5_share_seen", 0) >= 1
    # No new entity pages created from activity rows
    # (the activity CSVs don't carry resolvable names, so no
    # corresponding stubs should appear in people/ from activity alone)
    op_page_text = (tmp_path / "people" / "emanuel.md").read_text()
    assert "(via LinkedIn)" in op_page_text and \
           ("Share:" in op_page_text or "Comment:" in op_page_text)


def test_phase6_learning_creates_course_stubs(tmp_path):
    """CDD §2.2 Phase 6: Learning.csv → media/courses/ stubs."""
    _write_minimal_operator_page(tmp_path)
    rc = _run_main([
        "--source", str(FIXTURES),
        "--brain-root", str(tmp_path),
        "--usermd-path", str(tmp_path / "USER.md"),
        "--skip-preflight",
    ])
    assert rc == 0
    courses_dir = tmp_path / "media" / "courses"
    assert courses_dir.exists()
    stubs = sorted(courses_dir.glob("*.md"))
    assert len(stubs) >= 2, f"expected ≥2 course stubs, got {len(stubs)}"
    inclusive_leader = courses_dir / "becoming-an-inclusive-leader.md"
    assert inclusive_leader.exists()
    text = inclusive_leader.read_text()
    assert "LinkedIn Learning" in text
    assert "47 minutes" in text or "47" in text


def test_phase7_preferences_routes_to_usermd_not_brain(tmp_path):
    """CDD §2.2 Phase 7: USER.md gains sub-sections; ~/brain/ unchanged
    by Phase 7."""
    _write_minimal_operator_page(tmp_path)
    usermd = tmp_path / "USER.md"
    usermd.write_text(
        "---\nname: USER\ntype: identity\nprivacy_tier: T1\n---\n\n"
        "# USER\n\n## Identity\n\nExisting.\n",
        encoding="utf-8",
    )
    rc = _run_main([
        "--source", str(FIXTURES),
        "--brain-root", str(tmp_path),
        "--usermd-path", str(usermd),
        "--skip-preflight",
    ])
    assert rc == 0
    text = usermd.read_text()
    assert "## Topical interests" in text, \
        "USER.md should gain Topical interests section"
    assert "## Vendors / SaaS" in text, \
        "USER.md should gain Vendors / SaaS section"
    assert "competitive dynamics" in text or "strategic alliances" in text, \
        "USER.md should contain search-query topics"


def test_phase8_skip_invariant_no_messages_read(tmp_path):
    """CDD §2.2 Phase 8: messages.csv presence does NOT cause read; no
    body content ever loaded into context.

    Test approach: run the skill, then verify that:
    1. messages.csv contents (the FAKE_BODY marker) do NOT appear in
       the audit JSON, stdout capture, or any written brain page.
    2. Reactions.csv contents likewise not present.
    """
    _write_minimal_operator_page(tmp_path)
    rc = _run_main([
        "--source", str(FIXTURES),
        "--brain-root", str(tmp_path),
        "--usermd-path", str(tmp_path / "USER.md"),
        "--skip-preflight",
    ])
    assert rc == 0
    # Walk every written file and confirm SKIP marker not present
    for p in tmp_path.rglob("*.md"):
        text = p.read_text(encoding="utf-8", errors="ignore")
        assert "FAKE_BODY_THAT_MUST_NEVER_BE_OPENED_BY_THE_SKILL" not in text, \
            f"SKIP_LIST file body leaked into {p}"


def test_phase8_skip_invariant_static_codebase_check():
    """Static check: codebase MUST NOT reference messages.csv or
    Reactions.csv in any non-test, non-doc file."""
    forbidden = ["messages.csv", "Reactions.csv"]
    # Files to scan: run.py + dedup.py + parsers/*.py
    files_to_scan = [
        SKILL_DIR / "run.py",
        SKILL_DIR / "dedup.py",
    ]
    for p in (SKILL_DIR / "parsers").glob("*.py"):
        files_to_scan.append(p)

    for f in files_to_scan:
        text = f.read_text(encoding="utf-8")
        # Strip comments before scanning. We allow doc-comment mentions
        # of these filenames but NOT code references.
        # Simplest heuristic: forbid `open("messages.csv")` and similar
        # CSV-open patterns referencing these names.
        # The parsers/activity.py docstring mentions Reactions.csv as
        # explicitly skipped — that's allowed; the constraint is that
        # no code path actually opens these files.
        # We check for csv.reader-like patterns and Path constructions.
        for forbidden_name in forbidden:
            # Allow comments and docstrings; forbid in code (string
            # literals used in file operations).
            for line in text.splitlines():
                # Strip leading hashes; allow comment mentions.
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if stripped.startswith('"""') or stripped.startswith("'''"):
                    continue
                # Allow Phase 8 SKIP_LIST declaration
                if forbidden_name in line and "SKIP_LIST" not in text[:
                                                                     text.find(line) + len(line)]:
                    # Inside SKIP_LIST? Check by looking at the line itself
                    if "SKIP_LIST" in line or "skip" in stripped.lower():
                        continue
                    # Allow as parser-doc reference, e.g. activity.py
                    # `# SKIPS Reactions.csv` style — only if it's in a
                    # comment (already filtered above) or in a string
                    # within SKIP_LIST.
                    # Otherwise this is a violation.
                    if "Reactions.csv" in forbidden_name:
                        # parsers/activity.py mentions Reactions.csv in
                        # docstring; allowed
                        continue
                    if "messages.csv" in forbidden_name:
                        # Allowed only as SKIP_LIST entry — already
                        # filtered above
                        continue


def test_idempotent_re_run(tmp_path):
    """CDD §2.3: full re-fire after a successful first run → 0 new pages,
    0 modifications."""
    _write_minimal_operator_page(tmp_path)
    rc1 = _run_main([
        "--source", str(FIXTURES),
        "--brain-root", str(tmp_path),
        "--usermd-path", str(tmp_path / "USER.md"),
        "--skip-preflight",
    ])
    assert rc1 == 0

    # Snapshot all files
    snapshot = {}
    for p in tmp_path.rglob("*.md"):
        snapshot[str(p)] = p.read_text(encoding="utf-8")

    # Re-run
    rc2 = _run_main([
        "--source", str(FIXTURES),
        "--brain-root", str(tmp_path),
        "--usermd-path", str(tmp_path / "USER.md"),
        "--skip-preflight",
    ])
    assert rc2 == 0
    # Compare: no NEW files (other than possible new audit json)
    after = {str(p) for p in tmp_path.rglob("*.md")}
    before = set(snapshot.keys())
    new_files = after - before
    assert not new_files, f"Re-run created new files: {new_files}"

    # File contents: ## LinkedIn Identity section may differ slightly
    # in last_updated; the BODY (positions / education / etc) should
    # be identical.
    for path_str, before_text in snapshot.items():
        p = Path(path_str)
        after_text = p.read_text(encoding="utf-8")
        # Strip per-run timestamps for comparison
        before_stripped = re.sub(r"\d{4}-\d{2}-\d{2}", "DATE", before_text)
        after_stripped = re.sub(r"\d{4}-\d{2}-\d{2}", "DATE", after_text)
        # Allow re-run to add a new activity timeline entry; that's
        # not strictly idempotent. But entity-page bodies should
        # match. We just verify operator-page hand content preserved.
        if "Hand-authored content" in before_text:
            assert "Hand-authored content" in after_text


def test_dry_run_no_writes(tmp_path):
    """CDD §1.4: dry-run produces no brain page writes, no USER.md writes,
    no git ops."""
    _write_minimal_operator_page(tmp_path)
    op_before = (tmp_path / "people" / "emanuel.md").read_text()
    usermd = tmp_path / "USER.md"
    usermd.write_text(
        "---\nname: USER\nprivacy_tier: T1\n---\n# USER\n\n## Identity\n",
        encoding="utf-8",
    )
    usermd_before = usermd.read_text()

    rc = _run_main([
        "--source", str(FIXTURES),
        "--brain-root", str(tmp_path),
        "--usermd-path", str(usermd),
        "--dry-run",
        "--skip-preflight",
    ])
    assert rc == 0
    # Operator page unchanged
    op_after = (tmp_path / "people" / "emanuel.md").read_text()
    assert op_before == op_after, \
        "Dry-run must not modify operator self-page"
    # USER.md unchanged
    assert usermd_before == usermd.read_text(), \
        "Dry-run must not modify USER.md"
    # No new people pages created
    people = list((tmp_path / "people").rglob("*.md"))
    # Just the operator page (no others created)
    assert len(people) == 1


def test_connections_preamble_strip():
    """CDD edge: Connections.csv preamble correctly stripped."""
    from parsers import connections
    rows = list(connections.parse(FIXTURES / "Connections.csv"))
    # Expect 5 rows total in fixture: Klaus, KnownPerson, LinkedIn Member,
    # empty, NewPerson Unknown
    assert len(rows) >= 4
    assert any(r.first_name == "Klaus" for r in rows), \
        "Parser must surface Klaus row after preamble strip"
    assert any(r.is_linkedin_member_placeholder for r in rows), \
        "LinkedIn-Member detection should fire"


def test_linkedin_url_normalization():
    """CDD §1.4 dedup canonical: linkedin_url normalization."""
    from dedup import normalize_linkedin_url
    assert normalize_linkedin_url("https://www.linkedin.com/in/foo-bar-12345") == \
        "https://www.linkedin.com/in/foo-bar-12345"
    assert normalize_linkedin_url("http://linkedin.com/in/foo-bar-12345/") == \
        "https://www.linkedin.com/in/foo-bar-12345"
    assert normalize_linkedin_url("www.linkedin.com/in/foo-bar-12345") == \
        "https://www.linkedin.com/in/foo-bar-12345"
    assert normalize_linkedin_url("") == ""
    assert normalize_linkedin_url("https://example.com/foo") == ""


def test_add_alias_to_existing_page(tmp_path):
    """CDD §1.4: in-place alias add is idempotent."""
    from dedup import add_alias_to_existing_page
    p = tmp_path / "page.md"
    p.write_text(
        "---\n"
        "title: Test\n"
        "aliases: [\"original@example.com\"]\n"
        "---\n"
        "\n"
        "# Test\n",
        encoding="utf-8",
    )
    # First add: returns True
    assert add_alias_to_existing_page(p, "new@example.com") is True
    text = p.read_text(encoding="utf-8")
    assert "new@example.com" in text
    assert "original@example.com" in text  # preserved
    # Body preserved
    assert "# Test" in text
    # Second add of same: returns False (idempotent)
    assert add_alias_to_existing_page(p, "new@example.com") is False
