# <!-- CDD-CONTRACT: ~/gbrain/skills/linkedin-deepen/composers/endorsement_edges.py -->
# Contract:   Phase 3 — bidirectional endorsement timeline entries from
#             Endorsement_Given_Info.csv + Endorsement_Received_Info.csv.
# Inputs:     source_dir, brain_root, operator_page, registry, counters.
# Outputs:    Timeline entries on counterparty + operator page (both
#             endpoints). gitleaks-safe format: "Endorsement given/
#             received: <Skill> (via LinkedIn)".
# Invariants:
#             - Every successful write is bidirectional.
#             - Counterparty page missing → log + skip; NEVER creates.
#             - Idempotent via per-edge stable-row-hash.
# Idempotent: yes.

"""Phase 3 composer: endorsement edges."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Optional

from _shared.csv_utils import iter_rows, clean, stable_row_hash
from _shared.dedup_proxy import find_existing_person_by_linkedin_url
from _shared.page_io import append_timeline_entry


def run(
    source_dir: Path,
    brain_root: Path,
    operator_page: Path,
    *,
    today_iso: str,
    dry_run: bool,
    registry=None,
    counters: Optional[Counter] = None,
    warnings: Optional[Counter] = None,
    error_classes: Optional[Counter] = None,
) -> None:
    counters = counters if counters is not None else Counter()
    warnings = warnings if warnings is not None else Counter()
    error_classes = error_classes if error_classes is not None else Counter()

    _process_endorsements(
        source_dir / "Endorsement_Given_Info.csv",
        brain_root, operator_page,
        kind="given",
        url_col="Endorsee Public Url",
        date_col="Endorsement Date",
        skill_col="Skill Name",
        first_col="Endorsee First Name",
        last_col="Endorsee Last Name",
        dry_run=dry_run, registry=registry,
        counters=counters, warnings=warnings, error_classes=error_classes,
    )
    _process_endorsements(
        source_dir / "Endorsement_Received_Info.csv",
        brain_root, operator_page,
        kind="received",
        url_col="Endorser Public Url",
        date_col="Endorsement Date",
        skill_col="Skill Name",
        first_col="Endorser First Name",
        last_col="Endorser Last Name",
        dry_run=dry_run, registry=registry,
        counters=counters, warnings=warnings, error_classes=error_classes,
    )


def _process_endorsements(
    csv_path: Path,
    brain_root: Path,
    operator_page: Path,
    *,
    kind: str,
    url_col: str,
    date_col: str,
    skill_col: str,
    first_col: str,
    last_col: str,
    dry_run: bool,
    registry,
    counters: Counter,
    warnings: Counter,
    error_classes: Counter,
):
    if not csv_path.exists():
        counters[f"phase3_{kind}_csv_missing"] += 1
        return

    for row in iter_rows(
        csv_path,
        [url_col, date_col, first_col],
    ):
        counterparty_url = clean(row.get(url_col))
        skill = clean(row.get(skill_col))
        date = clean(row.get(date_col)) or ""
        first = clean(row.get(first_col)) or ""
        last = clean(row.get(last_col)) or ""
        counters[f"phase3_endorsement_{kind}_seen"] += 1

        if not skill:
            counters[f"phase3_endorsement_{kind}_no_skill"] += 1
            continue

        target = None
        if counterparty_url:
            target = find_existing_person_by_linkedin_url(
                counterparty_url, brain_root,
            )
        if target is None:
            counters[f"phase3_endorsement_{kind}_no_target_page"] += 1
            continue

        row_hash = stable_row_hash(
            [str(target), kind, skill, date, counterparty_url or ""]
        )
        edge_key = f"endorsement-{kind}-{row_hash}"
        if registry is not None and registry.was_applied(edge_key):
            counters[f"phase3_endorsement_{kind}_idempotent_skip"] += 1
            continue

        date_label = date or today_iso
        full_name = " ".join(p for p in (first, last) if p) or "(unknown)"

        # Counterparty page line — gitleaks-safe (LinkedIn at end).
        if kind == "given":
            cp_line = (
                f"- **{date_label}** | Endorsement received: {skill} "
                f"(operator endorsed via LinkedIn)"
            )
            op_line = (
                f"- **{date_label}** | Endorsement given: {skill} "
                f"to [[people/{_slug_from_path(target)}]] (via LinkedIn)"
            )
        else:
            cp_line = (
                f"- **{date_label}** | Endorsement given: {skill} "
                f"(to operator via LinkedIn)"
            )
            op_line = (
                f"- **{date_label}** | Endorsement received: {skill} "
                f"from [[people/{_slug_from_path(target)}]] (via LinkedIn)"
            )

        if dry_run:
            counters[f"phase3_endorsement_{kind}_dry_run_pending"] += 1
            continue

        try:
            wrote_cp = append_timeline_entry(target, cp_line)
            wrote_op = append_timeline_entry(operator_page, op_line)
            if wrote_cp:
                counters[f"phase3_endorsement_{kind}_cp_written"] += 1
            if wrote_op:
                counters[f"phase3_endorsement_{kind}_op_written"] += 1
            if wrote_cp or wrote_op:
                if registry is not None:
                    registry.mark_applied(edge_key)
                counters[f"phase3_endorsement_{kind}_edges_written"] += 1
        except Exception:
            error_classes[f"phase3_endorsement_{kind}_write_failed"] += 1


def _slug_from_path(p: Path) -> str:
    return p.stem
