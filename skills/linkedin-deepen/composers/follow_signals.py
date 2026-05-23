# <!-- CDD-CONTRACT: ~/gbrain/skills/linkedin-deepen/composers/follow_signals.py -->
# Contract:   Phase 4b — set `followed_by_operator: true` frontmatter on
#             name-matched people + institution pages from Member_Follows
#             + Company Follows CSVs; emit Watch List section on operator
#             page.
# Inputs:     source_dir, brain_root, operator_page, registry, counters.
# Outputs:    Frontmatter updates on matched pages; Watch List section on
#             operator self-page.
# Invariants: name-only match; no new pages; idempotent.
# Idempotent: yes.

"""Phase 4b composer: follow signals."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Optional

from _shared.csv_utils import iter_rows, clean
from _shared.dedup_proxy import (
    find_existing_person_by_name,
    find_existing_institution_by_name,
)
from _shared.page_io import (
    add_frontmatter_field,
    read_page,
    write_page,
    replace_or_append_section,
)


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

    member_follows = list(_read_member_follows(source_dir))
    company_follows = list(_read_company_follows(source_dir))

    counters["phase4b_member_follows_seen"] += len(member_follows)
    counters["phase4b_company_follows_seen"] += len(company_follows)

    matched_people = []
    unmatched_people = []
    for name, date in member_follows:
        target = find_existing_person_by_name(name, brain_root)
        if target is None:
            unmatched_people.append((name, date))
            counters["phase4b_member_follow_no_match"] += 1
            continue
        matched_people.append((name, date, target))
        if not dry_run:
            try:
                if add_frontmatter_field(target, "followed_by_operator", True):
                    counters["phase4b_member_follow_frontmatter_set"] += 1
            except Exception:
                error_classes["phase4b_member_follow_frontmatter_failed"] += 1

    matched_institutions = []
    unmatched_institutions = []
    for org, date in company_follows:
        target = find_existing_institution_by_name(org, brain_root)
        if target is None:
            unmatched_institutions.append((org, date))
            counters["phase4b_company_follow_no_match"] += 1
            continue
        matched_institutions.append((org, date, target))
        if not dry_run:
            try:
                if add_frontmatter_field(target, "followed_by_operator", True):
                    counters["phase4b_company_follow_frontmatter_set"] += 1
            except Exception:
                error_classes["phase4b_company_follow_frontmatter_failed"] += 1

    # Watch List section on operator page
    if matched_people or matched_institutions or unmatched_people or unmatched_institutions:
        watch_hash = ";".join(
            [n for n, _, _ in matched_people]
            + [o for o, _, _ in matched_institutions]
            + [f"x:{n}" for n, _ in unmatched_people]
            + [f"x:{o}" for o, _ in unmatched_institutions]
        )
        section_key = _key(str(operator_page), "watch-list", watch_hash)
        if registry is not None and registry.was_applied(section_key):
            counters["phase4b_watch_list_idempotent_skip"] += 1
        elif dry_run:
            counters["phase4b_dry_run_pending"] += 1
        else:
            try:
                fm, body = read_page(operator_page)
                if fm is not None:
                    content = _render_watch_list(
                        matched_people, matched_institutions,
                        unmatched_people, unmatched_institutions,
                    )
                    body = replace_or_append_section(
                        body, "## Watch List (LinkedIn follows)", content,
                    )
                    write_page(operator_page, fm, body)
                    if registry is not None:
                        registry.mark_applied(section_key)
                    counters["phase4b_watch_list_written"] += 1
            except Exception:
                error_classes["phase4b_watch_list_write_failed"] += 1


def _read_member_follows(source_dir: Path):
    for row in iter_rows(
        source_dir / "Member_Follows.csv",
        ["FullName", "Full Name", "Date"],
    ):
        name = clean(row.get("FullName") or row.get("Full Name"))
        if not name:
            continue
        yield name, clean(row.get("Date"))


def _read_company_follows(source_dir: Path):
    for row in iter_rows(
        source_dir / "Company Follows.csv",
        ["Organization", "organization", "Followed On", "followed on"],
    ):
        org = clean(row.get("Organization"))
        if not org:
            continue
        yield org, clean(row.get("Followed On"))


def _render_watch_list(
    matched_people, matched_institutions,
    unmatched_people, unmatched_institutions,
) -> str:
    lines = []
    if matched_people:
        lines.append("**People (linked):**")
        for name, date, target in matched_people:
            slug = target.stem
            d = f" — {date}" if date else ""
            lines.append(f"- [[people/{slug}]] · *{name}*{d}")
        lines.append("")
    if matched_institutions:
        lines.append("**Institutions (linked):**")
        for org, date, target in matched_institutions:
            slug = target.stem
            d = f" — {date}" if date else ""
            lines.append(f"- [[institutions/{slug}]] · *{org}*{d}")
        lines.append("")
    if unmatched_people:
        lines.append("**Unlinked people follows (LinkedIn-only):**")
        for name, date in unmatched_people[:30]:
            d = f" — {date}" if date else ""
            lines.append(f"- {name}{d}")
        lines.append("")
    if unmatched_institutions:
        lines.append("**Unlinked organization follows (LinkedIn-only):**")
        for org, date in unmatched_institutions[:30]:
            d = f" — {date}" if date else ""
            lines.append(f"- {org}{d}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _key(page: str, section: str, content_hash_input: str) -> str:
    import hashlib
    h = hashlib.sha256()
    h.update(page.encode("utf-8"))
    h.update(b"\x1f")
    h.update(section.encode("utf-8"))
    h.update(b"\x1f")
    h.update((content_hash_input or "").encode("utf-8"))
    return h.hexdigest()[:16]
