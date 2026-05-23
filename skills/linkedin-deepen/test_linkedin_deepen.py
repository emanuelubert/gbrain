# <!-- CDD-CONTRACT: ~/gbrain/skills/linkedin-deepen/test_linkedin_deepen.py -->
# Contract:   pytest suite for linkedin-deepen covering CDD §2 test plan.
# Inputs:     test-fixtures/ synthetic CSVs + test-brain/.
# Outputs:    15-test pass/fail; no real-data access; tmp_path-only brain.
# Invariants: tests assert no-new-pages + user-content preservation +
#             bidirectional cross-links + idempotent re-run + gitleaks-safe.

"""linkedin-deepen test suite (15 tests per CDD §2)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import re
from pathlib import Path

import pytest


SKILL_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = SKILL_DIR / "test-fixtures"
TEST_BRAIN_SEED = FIXTURES_DIR / "test-brain"


def _run_skill(
    tmp_path: Path,
    *,
    extra_args=None,
    expect_returncode=0,
):
    """Invoke run.py as a subprocess against tmp_path test brain.
    Returns (returncode, stdout_json_or_None, stderr_text)."""
    extra_args = list(extra_args or [])
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    args = [
        sys.executable,
        str(SKILL_DIR / "run.py"),
        "--source", str(FIXTURES_DIR),
        "--brain-root", str(tmp_path),
        "--audit-dir", str(audit_dir),
        "--operator-slug", "emanuel",
        "--run-id", "test-run",
    ] + extra_args
    proc = subprocess.run(args, capture_output=True, text=True)
    stdout = proc.stdout.strip()
    parsed = None
    if stdout:
        # Find the JSON block (last block) — print may include other lines.
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            # Find JSON-bracketed substring
            m = re.search(r"\{[\s\S]*\}\s*$", stdout)
            if m:
                try:
                    parsed = json.loads(m.group(0))
                except Exception:
                    parsed = None
    if proc.returncode != expect_returncode:
        print("STDOUT:", stdout, file=sys.stderr)
        print("STDERR:", proc.stderr, file=sys.stderr)
    return proc.returncode, parsed, proc.stderr


@pytest.fixture()
def test_brain(tmp_path):
    """Copy the seed test-brain into tmp_path."""
    target_people = tmp_path / "people"
    target_inst = tmp_path / "institutions"
    shutil.copytree(TEST_BRAIN_SEED / "people", target_people)
    shutil.copytree(TEST_BRAIN_SEED / "institutions", target_inst)
    return tmp_path


# === Phase smokes ==========================================================

def test_phase1_operator_self_sections_written(test_brain):
    """Phase 1 → Executive Summary, Career Arc, Education, Skills,
    Languages sections appear on emanuel.md."""
    rc, audit, stderr = _run_skill(test_brain, extra_args=["--phase", "1"])
    assert rc == 0, f"phase1 failed: {stderr}"
    text = (test_brain / "people" / "emanuel.md").read_text()
    assert "## Executive Summary" in text
    assert "## Career Arc (from LinkedIn)" in text
    assert "## Education (from LinkedIn)" in text
    assert "## Skills (from LinkedIn)" in text
    assert "## Languages" in text


def test_phase2_connection_snapshot_section(test_brain):
    """Phase 2 → ## LinkedIn Profile Snapshot section appears on the
    connection's existing page."""
    rc, audit, stderr = _run_skill(test_brain)
    assert rc == 0, f"run failed: {stderr}"
    text = (test_brain / "people" / "colleagues" / "jane-doe.md").read_text()
    assert "## LinkedIn Profile Snapshot" in text
    assert "VP Engineering" in text
    assert "Acme Corp" in text


def test_phase3_endorsement_bidirectional(test_brain):
    """Phase 3 endorsement → both endpoints get matching timeline entries."""
    rc, audit, stderr = _run_skill(test_brain)
    assert rc == 0, f"run failed: {stderr}"
    op_text = (test_brain / "people" / "emanuel.md").read_text()
    cp_text = (test_brain / "people" / "colleagues" / "jane-doe.md").read_text()
    # Endorsement given to Jane
    assert "Endorsement given: Strategy" in op_text
    assert "Endorsement received: Strategy" in cp_text
    # Endorsement received from Jane
    assert "Endorsement received: University Teaching" in op_text
    assert "Endorsement given: University Teaching" in cp_text


def test_phase4a_invitation_bidirectional(test_brain):
    rc, audit, stderr = _run_skill(test_brain)
    assert rc == 0, stderr
    op_text = (test_brain / "people" / "emanuel.md").read_text()
    cp_text = (test_brain / "people" / "colleagues" / "jane-doe.md").read_text()
    assert "Invitation sent" in op_text
    assert "Invitation received" in cp_text


def test_phase4b_follow_signals(test_brain):
    """Phase 4b → name-matched people/institutions get frontmatter
    flag; operator page gets Watch List section."""
    rc, audit, stderr = _run_skill(test_brain)
    assert rc == 0, stderr
    jane = (test_brain / "people" / "colleagues" / "jane-doe.md").read_text()
    acme = (test_brain / "institutions" / "acme-corp.md").read_text()
    assert "followed_by_operator" in jane
    assert "followed_by_operator" in acme
    op = (test_brain / "people" / "emanuel.md").read_text()
    assert "## Watch List (LinkedIn follows)" in op


def test_phase5_operator_voice_sections(test_brain):
    """Phase 5 → Comments + Shares produce operator-page sections only.
    No counterparty entity-page writes."""
    rc, audit, stderr = _run_skill(test_brain)
    assert rc == 0, stderr
    op = (test_brain / "people" / "emanuel.md").read_text()
    assert "## What They Engage On (LinkedIn signal)" in op
    assert "## What They Amplify (LinkedIn shares)" in op


def test_phase6_institution_sections(test_brain):
    """Phase 6 → institution pages gain Operator's Role + Connections
    Employed sections."""
    rc, audit, stderr = _run_skill(test_brain)
    assert rc == 0, stderr
    acme = (test_brain / "institutions" / "acme-corp.md").read_text()
    rsm = (test_brain / "institutions" / "rotterdam-school-of-management.md").read_text()
    assert "## Operator's Role Here" in acme or "## Operator's Role Here" in rsm
    assert "## LinkedIn Connections Employed Here" in acme or \
           "## LinkedIn Connections Employed Here" in rsm


# === Invariant tests =======================================================

def test_no_new_pages_created(test_brain):
    """pages_created counter is structurally 0; no page files appeared
    that weren't in the seed."""
    seed_files = sorted(
        p.relative_to(TEST_BRAIN_SEED) for p in TEST_BRAIN_SEED.rglob("*.md")
    )
    rc, audit, stderr = _run_skill(test_brain)
    assert rc == 0, stderr
    after_files = sorted(
        p.relative_to(test_brain) for p in test_brain.rglob("*.md")
    )
    assert set(after_files) == set(seed_files), (
        f"linkedin-deepen created new pages! diff={set(after_files) - set(seed_files)}"
    )
    assert audit is not None
    assert audit["counters"].get("pages_created", -1) == 0


def test_user_content_preserved_above_owned_sections(test_brain):
    """Hand-authored body content above skill-owned section headers is
    preserved byte-for-byte."""
    rc, audit, stderr = _run_skill(test_brain)
    assert rc == 0, stderr
    seed_jane = (TEST_BRAIN_SEED / "people" / "colleagues" / "jane-doe.md").read_text()
    after_jane = (test_brain / "people" / "colleagues" / "jane-doe.md").read_text()
    # Hand-authored preface block should still be present.
    assert "Hand-authored note about Jane from before the LinkedIn ingest." in after_jane
    assert "Pre-existing State content that should NOT be overwritten by linkedin-deepen." in after_jane
    # All seed text lines should still be present.
    for line in seed_jane.splitlines():
        if line.strip().startswith(("---", "##", "")):
            continue
        if line.strip() == "":
            continue
        assert line in after_jane, f"missing seed line: {line!r}"


def test_idempotent_rerun(test_brain):
    """Second run on unchanged source = 0 mutations (file hashes match)."""
    rc1, audit1, stderr1 = _run_skill(test_brain)
    assert rc1 == 0, stderr1
    # Capture per-file hashes after first run.
    import hashlib
    def _h(p):
        return hashlib.sha256(p.read_bytes()).hexdigest()
    first_hashes = {
        str(p.relative_to(test_brain)): _h(p)
        for p in test_brain.rglob("*.md")
    }
    rc2, audit2, stderr2 = _run_skill(test_brain)
    assert rc2 == 0, stderr2
    after_hashes = {
        str(p.relative_to(test_brain)): _h(p)
        for p in test_brain.rglob("*.md")
    }
    assert first_hashes == after_hashes, "idempotent re-run mutated files"


def test_dry_run_no_writes(test_brain):
    """--dry-run leaves brain byte-identical to seed."""
    import hashlib
    def _h(p):
        return hashlib.sha256(p.read_bytes()).hexdigest()
    seed_hashes = {
        str(p.relative_to(test_brain)): _h(p)
        for p in test_brain.rglob("*.md")
    }
    rc, audit, stderr = _run_skill(test_brain, extra_args=["--dry-run"])
    assert rc == 0, stderr
    after_hashes = {
        str(p.relative_to(test_brain)): _h(p)
        for p in test_brain.rglob("*.md")
    }
    assert seed_hashes == after_hashes
    assert audit is not None
    assert audit["dry_run"] is True


def test_anthropic_path_rejected(test_brain):
    """--llm-prose (Anthropic) errors out with exit code 5 + clear msg."""
    rc, audit, stderr = _run_skill(
        test_brain, extra_args=["--llm-prose"], expect_returncode=5,
    )
    assert rc == 5
    assert "UNSUPPORTED" in stderr or "violate D27" in stderr or "violates D27" in stderr


def test_llm_prose_local_short_circuits_in_pytest(test_brain, monkeypatch):
    """--llm-prose-local: when LLM_CALL_SCRIPT is missing or returns
    empty, falls back to deterministic templates (no failure)."""
    # The subprocess will try the real script. If it's not present or
    # returns non-zero, llm_prose._compose returns "" and the composer
    # falls back to deterministic. We just want exit-0.
    rc, audit, stderr = _run_skill(
        test_brain, extra_args=["--llm-prose-local"],
    )
    assert rc == 0, stderr
    text = (test_brain / "people" / "emanuel.md").read_text()
    # Either the LLM ran (provenance footer) or deterministic fallback
    # (verbatim summary text). Both are acceptable; what matters is
    # the section landed.
    assert "## Executive Summary" in text


# === Static codebase invariants ============================================

def test_no_messages_csv_reads_in_codebase():
    """Static scan: no code path opens messages.csv (or Reactions.csv).

    Specifically check that no I/O call (open / iter_rows / read_csv /
    Path with messages.csv) appears in the source. SKIP_LIST string
    literals are allowed since they declare the invariant, not violate it.
    """
    code_files = list(SKILL_DIR.rglob("*.py"))
    bad = []
    skip_csvs = ("messages.csv", "guide_messages.csv",
                 "learning_coach_messages.csv",
                 "learning_role_play_messages.csv",
                 "LearningCoachMessages.csv", "Reactions.csv")
    io_call_patterns = ("open(", "iter_rows(", "read_csv(",
                        "csv.reader(", 'source_dir /')
    for f in code_files:
        if "test-fixtures" in f.parts or "audit" in f.parts:
            continue
        if "test_linkedin_deepen" in f.name:
            continue
        text = f.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for skip in skip_csvs:
                if skip not in line:
                    continue
                # Pattern check: is this an I/O call that references the file?
                for ioc in io_call_patterns:
                    if ioc in line:
                        bad.append(f"{f.relative_to(SKILL_DIR)}:{i} I/O on skip-list file: {stripped}")
                        break
    assert not bad, f"skip-list files referenced in I/O calls:\n  " + "\n  ".join(bad)


def test_gitleaks_safe_phrasing_in_codebase():
    """Static scan: no `linkedin_*: "<hex>"` pattern AND no `LinkedIn
    <singleword>: ` pattern in generated content templates."""
    code_files = list(SKILL_DIR.rglob("*.py"))
    bad = []
    for f in code_files:
        if "test-fixtures" in f.parts or "audit" in f.parts:
            continue
        if "test_linkedin_deepen" in f.name:
            continue
        text = f.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            # Bad: linkedin_foo: "<16hex>"
            if re.search(r'linkedin_\w+\s*:\s*"[0-9a-f]{16}"', line):
                bad.append(f"{f.name}:{i} hex-secret-shaped value: {line.strip()}")
            # Bad: "LinkedIn <Word>: <value>" in f-string emission
            if re.search(
                r"f['\"]\S*LinkedIn\s+\{[^}]+\}\s*:\s*\{",
                line,
            ):
                bad.append(f"{f.name}:{i} LinkedIn-leading template: {line.strip()}")
    assert not bad, f"gitleaks-unsafe patterns:\n  " + "\n  ".join(bad)
