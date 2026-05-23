# <!-- CDD-CONTRACT: ~/gbrain/skills/linkedin-deepen/composers/institution_signals.py -->
# Contract:   Phase 6 — per institution touched by Positions / Education /
#             Company Follows: add ## Operator's Role Here + ## LinkedIn
#             Connections Employed Here sections.
# Inputs:     source_dir, brain_root, registry, counters.
# Outputs:    Institution pages mutated; sections written; no new pages.
# Invariants: only writes to existing institution pages; skips missing.
# Idempotent: yes.

"""Phase 6 composer: institution signals."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Optional

from _shared.csv_utils import iter_rows, clean, stable_row_hash
from _shared.dedup_proxy import find_existing_institution_by_name
from _shared.page_io import (
    read_page,
    write_page,
    replace_or_append_section,
)


_TOP_N_CONNECTIONS = 20


def run(
    source_dir: Path,
    brain_root: Path,
    *,
    today_iso: str,
    dry_run: bool,
    llm_prose_local: bool,
    llm_compose_fn=None,
    registry=None,
    counters: Optional[Counter] = None,
    warnings: Optional[Counter] = None,
    error_classes: Optional[Counter] = None,
) -> None:
    counters = counters if counters is not None else Counter()
    warnings = warnings if warnings is not None else Counter()
    error_classes = error_classes if error_classes is not None else Counter()

    positions_by_org = _group_positions(source_dir)
    education_by_school = _group_education(source_dir)
    connections_by_org = _group_connections(source_dir)

    # Collect every institution touched by ANY source.
    touched_orgs = set(positions_by_org.keys()) \
        | set(education_by_school.keys()) \
        | set(connections_by_org.keys())

    counters["phase6_distinct_institutions_seen"] += len(touched_orgs)

    for org in touched_orgs:
        target = find_existing_institution_by_name(org, brain_root)
        if target is None:
            counters["phase6_no_institution_page"] += 1
            continue

        counters["phase6_institutions_touched"] += 1
        positions = positions_by_org.get(org, [])
        edu_entries = education_by_school.get(org, [])
        connection_rows = connections_by_org.get(org, [])

        # ## Operator's Role Here
        role_hash = stable_row_hash([
            str(p) for p in (positions, edu_entries)
        ])
        section_key = _key(str(target), "operator-role", role_hash)
        if registry is not None and registry.was_applied(section_key):
            counters["phase6_role_section_idempotent_skip"] += 1
        elif positions or edu_entries:
            if dry_run:
                counters["phase6_role_section_dry_run_pending"] += 1
            else:
                try:
                    fm, body = read_page(target)
                    if fm is not None:
                        content = _render_operator_role(
                            positions, edu_entries,
                            use_llm=llm_prose_local, llm_fn=llm_compose_fn,
                        )
                        body = replace_or_append_section(
                            body, "## Operator's Role Here", content,
                        )
                        write_page(target, fm, body)
                        if registry is not None:
                            registry.mark_applied(section_key)
                        counters["phase6_role_section_written"] += 1
                except Exception:
                    error_classes["phase6_role_section_write_failed"] += 1

        # ## LinkedIn Connections Employed Here
        conn_hash = stable_row_hash([
            c.get("full_name", "") for c in connection_rows
        ])
        section_key2 = _key(str(target), "connections-employed", conn_hash)
        if registry is not None and registry.was_applied(section_key2):
            counters["phase6_connections_section_idempotent_skip"] += 1
        elif connection_rows:
            if dry_run:
                counters["phase6_connections_section_dry_run_pending"] += 1
            else:
                try:
                    fm, body = read_page(target)
                    if fm is not None:
                        content = _render_connections_employed(
                            connection_rows
                        )
                        body = replace_or_append_section(
                            body,
                            "## LinkedIn Connections Employed Here",
                            content,
                        )
                        write_page(target, fm, body)
                        if registry is not None:
                            registry.mark_applied(section_key2)
                        counters["phase6_connections_section_written"] += 1
                except Exception:
                    error_classes["phase6_connections_section_write_failed"] += 1


def _group_positions(source_dir: Path) -> dict:
    out: dict = {}
    for row in iter_rows(
        source_dir / "Positions.csv", ["Company Name", "company name"]
    ):
        company = clean(row.get("Company Name"))
        if not company:
            continue
        out.setdefault(company, []).append({
            "title": clean(row.get("Title")) or "",
            "started_on": clean(row.get("Started On")) or "",
            "finished_on": clean(row.get("Finished On")) or "",
            "location": clean(row.get("Location")) or "",
            "description": clean(row.get("Description")) or "",
        })
    return out


def _group_education(source_dir: Path) -> dict:
    out: dict = {}
    for row in iter_rows(
        source_dir / "Education.csv", ["School Name", "school name"]
    ):
        school = clean(row.get("School Name"))
        if not school:
            continue
        out.setdefault(school, []).append({
            "degree": clean(row.get("Degree Name")) or "",
            "notes": clean(row.get("Notes")) or "",
            "activities": clean(row.get("Activities")) or "",
            "started_on": clean(row.get("Start Date")) or "",
            "finished_on": clean(row.get("End Date")) or "",
        })
    return out


def _group_connections(source_dir: Path) -> dict:
    out: dict = {}
    for row in iter_rows(
        source_dir / "Connections.csv",
        ["First Name", "first name", "Email Address", "email address"],
    ):
        first = clean(row.get("First Name"))
        last = clean(row.get("Last Name"))
        if first == "LinkedIn" and last == "Member":
            continue
        company = clean(row.get("Company"))
        if not company:
            continue
        full_name = " ".join(p for p in (first, last) if p) or "(unknown)"
        out.setdefault(company, []).append({
            "full_name": full_name,
            "position": clean(row.get("Position")) or "",
            "url": clean(row.get("URL")) or "",
            "connected_on": clean(row.get("Connected On")) or "",
        })
    return out


def _render_operator_role(positions, edu_entries, *, use_llm, llm_fn) -> str:
    lines = []
    for p in positions:
        dates = " — ".join(d for d in (p["started_on"], p["finished_on"]) if d) or "(undated)"
        title = p["title"] or "(role)"
        lines.append(f"- **{title}** · *{dates}*")
        if p["description"]:
            if use_llm and llm_fn:
                prompt = (
                    f"Summarize this role at the organization in 1-2 sentences "
                    f"(present tense, third-person, factual).\n\n"
                    f"Role: {title}\nDescription: {p['description']}"
                )
                try:
                    composed = llm_fn(prompt)
                    if composed:
                        lines.append(f"  {composed.replace(chr(10), chr(10) + '  ')}")
                    else:
                        lines.append(f"  {p['description']}")
                except Exception:
                    lines.append(f"  {p['description']}")
            else:
                lines.append(f"  {p['description']}")
    for e in edu_entries:
        dates = " — ".join(d for d in (e["started_on"], e["finished_on"]) if d) or "(undated)"
        title = e["degree"] or "(degree)"
        lines.append(f"- **{title}** (degree) · *{dates}*")
        if e["notes"]:
            lines.append(f"  Notes: {e['notes']}")
        if e["activities"]:
            lines.append(f"  Activities: {e['activities']}")
    return "\n".join(lines).rstrip() + "\n"


def _render_connections_employed(connection_rows) -> str:
    n = len(connection_rows)
    sorted_rows = sorted(
        connection_rows, key=lambda c: c.get("full_name") or "",
    )
    top = sorted_rows[:_TOP_N_CONNECTIONS]
    lines = [f"**{n} LinkedIn connection(s) listed this organization** (top {min(n, _TOP_N_CONNECTIONS)} by name):", ""]
    for c in top:
        pos = f" · {c['position']}" if c["position"] else ""
        lines.append(f"- {c['full_name']}{pos}")
    if n > _TOP_N_CONNECTIONS:
        lines.append("")
        lines.append(f"*+{n - _TOP_N_CONNECTIONS} more not shown.*")
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
