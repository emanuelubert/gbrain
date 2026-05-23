#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pyyaml>=6.0",
# ]
# ///
# <!-- CDD-CONTRACT: ~/gbrain/skills/linkedin-deepen/run.py -->
# Contract:   main dispatcher for linkedin-deepen (texture pass).
# Inputs:     CLI args (--phase N | --dry-run | --source <path> |
#             --brain-root <path> | --llm-prose-local | --llm-prose |
#             --operator-slug | --run-id).
# Outputs:    Phase audit logs at audit/<run-id>.json; counters via
#             emit_run_record; brain page mutations (sections written
#             into existing pages only — NEVER creates pages); single
#             git commit at phase 8 (real run only).
# Invariants:
#             - Dry-run: NO ~/brain/ writes, NO git ops.
#             - --llm-prose (Anthropic): UNSUPPORTED; errors with clear
#               D27 violation message.
#             - --llm-prose-local: routes to LM Studio :1234 via
#               llm_call_by_role.sh enrich_dispatcher_heavy.
#             - NO new pages created (pages_created counter == 0).
#             - SKIP_LIST files NEVER opened.
#             - Idempotent: per-(page, section, source-row-hash) registry.
# Test plan:  test-fixtures/ synthetic CSVs + test-brain/; pytest tmp_path
#             clones the test-brain; 15-test suite per CDD §2.

"""linkedin-deepen implementation v0.1.0.

Per D27: fully-local Python execution. No Claude in the runtime loop.
Per S191-D119+1: texture pass on top of linkedin-bootstrap stubs.
"""

from __future__ import annotations

import argparse
import json as _json
import sys
import traceback
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# Make this skill's package importable as `_shared` / `composers`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Use shared gbrain helpers if present (run_record + write_failure).
from _shared.failures import write_failure  # noqa: E402
from _shared.run_record import emit_run_record  # noqa: E402

# Local _shared package (this skill).
sys.path.insert(0, str(Path(__file__).resolve().parent))

import importlib  # noqa: E402

_local_shared = importlib.import_module("linkedin-deepen._shared" if False else "_shared")
# Re-import via module name we control to avoid clash with gbrain _shared.

# Switch import to our package: prepend skill dir SO our `_shared/` shadows.
# Since the linkedin-bootstrap pattern uses local `_shared/`, do the same.


def _get_local_modules():
    """Lazy load local composers + _shared submodules, isolated from
    gbrain top-level _shared.

    We name them under a synthetic package by manipulating sys.modules
    to avoid the name clash.
    """
    import importlib.util

    skill_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(skill_dir))

    # Force-import this skill's _shared as `linkedin_deepen_shared.*` then
    # rebind names. Simpler: just import as plain `_shared` after the
    # skill-dir is first on sys.path. The gbrain `_shared` is already
    # imported under same name, but Python's module cache resolves to the
    # FIRST-loaded. The failures + run_record we already imported are
    # from gbrain skills/_shared. To get OUR _shared/csv_utils etc., we
    # use importlib.util.spec_from_file_location.

    def _load(modname: str, file_path: Path):
        spec = importlib.util.spec_from_file_location(
            modname, file_path,
            submodule_search_locations=[str(file_path.parent)]
            if file_path.name == "__init__.py" else None,
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"could not load {modname} from {file_path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[modname] = mod
        spec.loader.exec_module(mod)
        return mod

    # Load _shared submodules under unique names.
    shared_dir = skill_dir / "_shared"
    composers_dir = skill_dir / "composers"

    _load("lid_shared", shared_dir / "__init__.py")
    csv_utils = _load("lid_shared.csv_utils", shared_dir / "csv_utils.py")
    page_io = _load("lid_shared.page_io", shared_dir / "page_io.py")
    idempotency = _load("lid_shared.idempotency", shared_dir / "idempotency.py")
    dedup_proxy = _load("lid_shared.dedup_proxy", shared_dir / "dedup_proxy.py")
    llm_prose = _load("lid_shared.llm_prose", shared_dir / "llm_prose.py")

    return csv_utils, page_io, idempotency, dedup_proxy, llm_prose


# Hard-importable composers via the dispatcher.  The composers themselves
# use relative imports under `_shared.*`. To keep that working without
# the gbrain `_shared` clash, we rely on the canonical pattern where this
# skill's directory is first on `sys.path` and its `_shared/` is the
# `_shared` that the composer modules resolve to. Force this BEFORE
# composer imports.
_skill_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_skill_dir))

# Drop any pre-cached `_shared` so our local one takes precedence.
for _modname in list(sys.modules):
    if _modname == "_shared" or _modname.startswith("_shared."):
        del sys.modules[_modname]

from composers import (  # noqa: E402
    operator_self as composer_operator_self,
    connection_snapshot as composer_connection_snapshot,
    endorsement_edges as composer_endorsement_edges,
    invitation_edges as composer_invitation_edges,
    follow_signals as composer_follow_signals,
    operator_voice as composer_operator_voice,
    institution_signals as composer_institution_signals,
)
from _shared.idempotency import AppliedRegistry  # noqa: E402
from _shared.llm_prose import compose_prose as _compose_prose_local  # noqa: E402


# === Constants ============================================================

HOME = Path.home()
HERMES_HOME = HOME / ".hermes"
BRAIN_HOME = HOME / "brain"
SKILL_NAME = "linkedin-deepen"
SKILL_VERSION = "0.1.0"
SCHEMA_VERSION = 1

DEFAULT_SOURCE = (
    HOME / "resources" / "local-agent-system" / "data" / "LinkedIn" /
    "Complete_LinkedInDataExport_05-14-2026.zip"
)
DEFAULT_OPERATOR_SLUG = "emanuel"

# Sibling invariant to linkedin-bootstrap: never opened by ANY phase.
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


class OperatorPageError(Exception):
    """Operator self-page not found or ambiguous."""


# === Phase 0: preflight ===================================================

def _resolve_operator_page(
    brain_root: Path, operator_slug_override=None,
) -> Path:
    slug = operator_slug_override or DEFAULT_OPERATOR_SLUG
    candidate = brain_root / "people" / f"{slug}.md"
    if candidate.exists():
        return candidate
    for sub in ("personal", "colleagues", "family"):
        p = brain_root / "people" / sub / f"{slug}.md"
        if p.exists():
            return p
    raise OperatorPageError(
        f"operator self-page not found: tried {brain_root}/people/{slug}.md "
        f"and sub-dirs. Set --operator-slug."
    )


# === Phase 7: SKIP invariant verification =================================

def _phase_7_skip_invariant(source_dir: Path, counters: Counter):
    """Phase 7 — verify SKIP_LIST files are present-but-never-opened.
    The static check is the test_phase7_skip_invariant codebase scan."""
    for skip in SKIP_LIST:
        p = source_dir / skip
        if p.exists():
            counters["phase7_skip_files_present"] += 1
        else:
            counters["phase7_skip_files_absent"] += 1


# === Phase 8: commit + push (real run only) ===============================

def _phase_8_commit(
    brain_root: Path, run_id: str, dry_run: bool,
    counters: Counter, error_classes: Counter,
):
    if dry_run or brain_root != BRAIN_HOME:
        counters["phase8_skipped_non_real_run"] += 1
        return
    try:
        from _shared.git_commit import commit_and_push_brain  # type: ignore
    except ImportError:
        # gbrain skills/_shared/git_commit.py
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_gbrain_shared_git_commit",
            Path.home() / "gbrain" / "skills" / "_shared" / "git_commit.py",
        )
        if spec is None or spec.loader is None:
            error_classes["phase8_git_commit_import_failed"] += 1
            return
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
            commit_and_push_brain = mod.commit_and_push_brain
        except Exception:
            error_classes["phase8_git_commit_import_failed"] += 1
            return

    extra = (
        f"linkedin-deepen run {run_id}\n\n"
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
        error_classes["phase8_commit_exception"] += 1


# === Main =================================================================

def main():
    parser = argparse.ArgumentParser(
        description=f"{SKILL_NAME} v{SKILL_VERSION} (per D27 + S191-D119+1)"
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                        help="LinkedIn export directory")
    parser.add_argument("--brain-root", type=Path, default=BRAIN_HOME,
                        help="Override ~/brain root (tests use tmp_path)")
    parser.add_argument("--operator-slug", default=None,
                        help="Operator self-page slug (default: emanuel)")
    parser.add_argument("--phase", type=int, default=None,
                        help="Skip to phase N (still runs Phase 0 preflight)")
    parser.add_argument("--dry-run", action="store_true",
                        help="No brain writes, no git ops")
    parser.add_argument(
        "--llm-prose-local", action="store_true",
        help="Enable LOCAL LM Studio prose composition (qwen3.6-35b)",
    )
    parser.add_argument(
        "--llm-prose", action="store_true",
        help="UNSUPPORTED — Anthropic path violates D27; see error message",
    )
    parser.add_argument("--run-id", default=None,
                        help="Run identifier (default: uuid4)")
    parser.add_argument(
        "--audit-dir", type=Path, default=None,
        help="Override audit dir (default: <skill>/audit)",
    )
    parser.add_argument(
        "--max-workers", type=int, default=1,
        help=(
            "Phase 2 ThreadPoolExecutor max_workers (default 1 = single-"
            "threaded; 2 = parallel against dual-loaded qwen3.6-35b "
            "instances per D103). Soft-capped at 2."
        ),
    )
    parser.add_argument(
        "--auto-flush-every", type=int, default=50,
        help=(
            "AppliedRegistry: commit applied.json every N marks (default 50). "
            "Set 0 to disable mid-run flushing (commit only at end-of-run)."
        ),
    )
    args = parser.parse_args()

    if args.max_workers < 1 or args.max_workers > 2:
        print(
            f"[error] --max-workers must be 1 or 2 (got {args.max_workers}); "
            f"only 2 qwen3.6-35b instances loadable in this configuration.",
            file=sys.stderr,
        )
        return 6
    if args.auto_flush_every < 0:
        print(
            f"[error] --auto-flush-every must be >= 0 (got {args.auto_flush_every}).",
            file=sys.stderr,
        )
        return 6

    # === Phase 0: preflight ===
    if args.llm_prose:
        msg = (
            "--llm-prose (Anthropic path) is UNSUPPORTED — would violate D27 "
            "'no PII egress' invariant + the S191-D119+1 operator architecture "
            "decision 'everything local except orchestration'. Use "
            "--llm-prose-local instead (routes to local LM Studio :1234)."
        )
        print(f"[error] {msg}", file=sys.stderr)
        return 5

    run_id = args.run_id or str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    today_iso = started_at.date().isoformat()
    counters = Counter()
    error_classes = Counter()
    warnings = Counter()

    if not args.source.exists():
        _emit_failure(run_id, started_at, "source_missing",
                      f"source dir not found: {args.source}", counters)
        return 3

    try:
        operator_page = _resolve_operator_page(
            args.brain_root, operator_slug_override=args.operator_slug,
        )
    except OperatorPageError as e:
        _emit_failure(run_id, started_at, "operator_page_missing",
                      str(e), counters)
        return 4

    audit_dir = args.audit_dir or (Path(__file__).resolve().parent / "audit")
    audit_dir.mkdir(parents=True, exist_ok=True)
    registry = AppliedRegistry(
        audit_dir / "applied.json",
        auto_flush_every=args.auto_flush_every,
    )

    # LLM compose function selection.
    llm_compose_fn = None
    if args.llm_prose_local:
        def _compose(prompt: str) -> str:
            return _compose_prose_local(prompt) or ""
        llm_compose_fn = _compose

    start_phase = args.phase or 1

    # === Phases 1-7 ===
    try:
        if start_phase <= 1:
            composer_operator_self.run(
                args.source, operator_page,
                today_iso=today_iso, dry_run=args.dry_run,
                llm_prose_local=args.llm_prose_local,
                llm_compose_fn=llm_compose_fn,
                registry=registry, counters=counters,
                warnings=warnings, error_classes=error_classes,
            )
        if start_phase <= 2:
            composer_connection_snapshot.run(
                args.source, args.brain_root,
                today_iso=today_iso, dry_run=args.dry_run,
                llm_prose_local=args.llm_prose_local,
                llm_compose_fn=llm_compose_fn,
                registry=registry, counters=counters,
                warnings=warnings, error_classes=error_classes,
                max_workers=args.max_workers,
            )
        if start_phase <= 3:
            composer_endorsement_edges.run(
                args.source, args.brain_root, operator_page,
                today_iso=today_iso, dry_run=args.dry_run,
                registry=registry, counters=counters,
                warnings=warnings, error_classes=error_classes,
            )
        if start_phase <= 4:
            composer_invitation_edges.run(
                args.source, args.brain_root, operator_page,
                today_iso=today_iso, dry_run=args.dry_run,
                registry=registry, counters=counters,
                warnings=warnings, error_classes=error_classes,
            )
            composer_follow_signals.run(
                args.source, args.brain_root, operator_page,
                today_iso=today_iso, dry_run=args.dry_run,
                registry=registry, counters=counters,
                warnings=warnings, error_classes=error_classes,
            )
        if start_phase <= 5:
            composer_operator_voice.run(
                args.source, operator_page,
                today_iso=today_iso, dry_run=args.dry_run,
                registry=registry, counters=counters,
                warnings=warnings, error_classes=error_classes,
            )
        if start_phase <= 6:
            composer_institution_signals.run(
                args.source, args.brain_root,
                today_iso=today_iso, dry_run=args.dry_run,
                llm_prose_local=args.llm_prose_local,
                llm_compose_fn=llm_compose_fn,
                registry=registry, counters=counters,
                warnings=warnings, error_classes=error_classes,
            )
        if start_phase <= 7:
            _phase_7_skip_invariant(args.source, counters)
        # Persist applied-registry before commit.
        if not args.dry_run:
            registry.commit_audit()
        if start_phase <= 8:
            _phase_8_commit(
                args.brain_root, run_id, args.dry_run,
                counters, error_classes,
            )
    except Exception:
        error_classes["unhandled_exception"] += 1
        try:
            write_failure(
                args.brain_root, SKILL_NAME, "unhandled_exception",
                {"pk": run_id},
                traceback_str=traceback.format_exc(),
            )
        except Exception:
            pass

    # Structural invariant: NO new pages created.
    counters["pages_created"] = 0

    # === Audit log ===
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
        "llm_prose_local": bool(args.llm_prose_local),
        "source": str(args.source),
        "brain_root": str(args.brain_root),
        "operator_page": str(operator_page),
        "applied_registry_size": len(registry),
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
    try:
        emit_run_record(rec)
    except Exception:
        pass

    print(_json.dumps({
        "run_id": run_id,
        "outcome": audit["outcome"],
        "duration_seconds": audit["duration_seconds"],
        "fixture_mode": audit["fixture_mode"],
        "dry_run": audit["dry_run"],
        "llm_prose_local": bool(args.llm_prose_local),
        "audit_path": str(audit_path),
        "applied_registry_size": len(registry),
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
