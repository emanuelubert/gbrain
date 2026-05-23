# <!-- CDD-CONTRACT: ~/gbrain/skills/linkedin-deepen/composers/operator_self.py -->
# Contract:   Phase 1 — read Profile/Positions/Education/Skills/Languages
#             CSVs and write skill-owned sections into operator self-page.
# Inputs:     source_dir, operator_page Path, llm_prose flag, registry,
#             counters.
# Outputs:    operator_page mutated; skill-owned sections written:
#             ## Executive Summary, ## Career Arc (from LinkedIn),
#             ## Education (from LinkedIn), ## Skills (from LinkedIn),
#             ## Languages.
# Edge:       Profile.csv Summary empty → omit Executive Summary section.
#             Position with empty Description → structural-only render.
# Idempotent: per-section-hash registry; re-run on unchanged source = no
#             writes.

"""Phase 1 composer: operator-self deepen."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Optional

from _shared.csv_utils import iter_rows, stable_csv_checksum, clean
from _shared.page_io import (
    read_page,
    write_page,
    replace_or_append_section,
)


def run(
    source_dir: Path,
    operator_page: Path,
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
    """Compose + write skill-owned sections on the operator self-page."""
    counters = counters if counters is not None else Counter()
    warnings = warnings if warnings is not None else Counter()
    error_classes = error_classes if error_classes is not None else Counter()

    profile = _read_profile(source_dir)
    positions = list(_read_positions(source_dir))
    education = list(_read_education(source_dir))
    skills = list(_read_skills(source_dir))
    languages = list(_read_languages(source_dir))

    counters["phase1_positions_seen"] += len(positions)
    counters["phase1_education_seen"] += len(education)
    counters["phase1_skills_seen"] += len(skills)
    counters["phase1_languages_seen"] += len(languages)

    fm, body = read_page(operator_page)
    if fm is None:
        warnings["phase1_operator_page_no_frontmatter"] += 1
        return

    # Per-section idempotency keys.
    sections_to_write = []

    # ## Executive Summary
    if profile.get("summary"):
        section_key = _key("emanuel.md", "exec-summary", profile.get("summary"))
        if registry is None or not registry.was_applied(section_key):
            content = _render_exec_summary(profile, llm_prose_local, llm_compose_fn)
            sections_to_write.append(("## Executive Summary", content, section_key))
        else:
            counters["phase1_exec_summary_idempotent_skip"] += 1

    # ## Career Arc (from LinkedIn)
    if positions:
        pos_hash = ";".join(
            f"{p['company']}|{p['title']}|{p['started_on']}|{p['finished_on']}"
            for p in positions
        )
        section_key = _key("emanuel.md", "career-arc", pos_hash)
        if registry is None or not registry.was_applied(section_key):
            content = _render_career_arc(positions, llm_prose_local, llm_compose_fn)
            sections_to_write.append(
                ("## Career Arc (from LinkedIn)", content, section_key)
            )
        else:
            counters["phase1_career_arc_idempotent_skip"] += 1

    # ## Education (from LinkedIn)
    if education:
        edu_hash = ";".join(
            f"{e['school']}|{e['degree']}|{e['started_on']}|{e['finished_on']}"
            for e in education
        )
        section_key = _key("emanuel.md", "education", edu_hash)
        if registry is None or not registry.was_applied(section_key):
            content = _render_education(education, llm_prose_local, llm_compose_fn)
            sections_to_write.append(
                ("## Education (from LinkedIn)", content, section_key)
            )
        else:
            counters["phase1_education_idempotent_skip"] += 1

    # ## Skills (from LinkedIn)
    if skills:
        sk_hash = ";".join(skills)
        section_key = _key("emanuel.md", "skills", sk_hash)
        if registry is None or not registry.was_applied(section_key):
            content = _render_skills(skills)
            sections_to_write.append(
                ("## Skills (from LinkedIn)", content, section_key)
            )
        else:
            counters["phase1_skills_idempotent_skip"] += 1

    # ## Languages
    if languages:
        lang_hash = ";".join(languages)
        section_key = _key("emanuel.md", "languages", lang_hash)
        if registry is None or not registry.was_applied(section_key):
            content = _render_languages(languages)
            sections_to_write.append(
                ("## Languages", content, section_key)
            )
        else:
            counters["phase1_languages_idempotent_skip"] += 1

    if dry_run:
        counters["phase1_dry_run_skipped_writes"] += len(sections_to_write)
        return

    for section_header, content, section_key in sections_to_write:
        body = replace_or_append_section(body, section_header, content)
        if registry is not None:
            registry.mark_applied(section_key)
        counters["phase1_sections_written"] += 1

    if sections_to_write:
        write_page(operator_page, fm, body)
        counters["phase1_pages_touched"] += 1


# === Parsers (operator-self CSVs) ==========================================

def _read_profile(source_dir: Path) -> dict:
    """Return a dict with first_name, last_name, headline, summary,
    industry, location, twitter_handles, websites."""
    out = {
        "first_name": None,
        "last_name": None,
        "headline": None,
        "summary": None,
        "industry": None,
        "location": None,
        "twitter_handles": None,
        "websites": None,
    }
    for row in iter_rows(
        source_dir / "Profile.csv", ["First Name", "first name"]
    ):
        out["first_name"] = clean(row.get("First Name"))
        out["last_name"] = clean(row.get("Last Name"))
        out["headline"] = clean(row.get("Headline"))
        out["summary"] = clean(row.get("Summary"))
        out["industry"] = clean(row.get("Industry"))
        out["location"] = (
            clean(row.get("Geo Location")) or clean(row.get("Address"))
        )
        out["twitter_handles"] = clean(row.get("Twitter Handles"))
        out["websites"] = clean(row.get("Websites"))
        break
    if not out["summary"]:
        for row in iter_rows(
            source_dir / "Profile Summary.csv", ["Summary", "summary"]
        ):
            out["summary"] = clean(row.get("Summary"))
            break
    return out


def _read_positions(source_dir: Path):
    for row in iter_rows(
        source_dir / "Positions.csv", ["Company Name", "company name"]
    ):
        company = clean(row.get("Company Name"))
        if not company:
            continue
        yield {
            "company": company,
            "title": clean(row.get("Title")) or "",
            "started_on": clean(row.get("Started On")) or "",
            "finished_on": clean(row.get("Finished On")) or "",
            "location": clean(row.get("Location")) or "",
            "description": clean(row.get("Description")) or "",
        }


def _read_education(source_dir: Path):
    for row in iter_rows(
        source_dir / "Education.csv", ["School Name", "school name"]
    ):
        school = clean(row.get("School Name"))
        if not school:
            continue
        yield {
            "school": school,
            "degree": clean(row.get("Degree Name")) or "",
            "notes": clean(row.get("Notes")) or "",
            "activities": clean(row.get("Activities")) or "",
            "started_on": clean(row.get("Start Date")) or "",
            "finished_on": clean(row.get("End Date")) or "",
        }


def _read_skills(source_dir: Path):
    seen = set()
    for row in iter_rows(source_dir / "Skills.csv", ["Name", "name"]):
        s = clean(row.get("Name"))
        if s and s not in seen:
            seen.add(s)
            yield s


def _read_languages(source_dir: Path):
    seen = set()
    for row in iter_rows(source_dir / "Languages.csv", ["Name", "name"]):
        s = clean(row.get("Name"))
        if s and s not in seen:
            seen.add(s)
            yield s


# === Renderers =============================================================

def _render_exec_summary(profile: dict, use_llm: bool, llm_fn) -> str:
    summary = profile.get("summary") or ""
    if use_llm and llm_fn:
        prompt = (
            "Rewrite this LinkedIn summary as a tight executive summary "
            "(2-3 sentences, third-person, present tense). Stay grounded "
            "in the input; do not invent facts.\n\nInput:\n" + summary
        )
        try:
            composed = llm_fn(prompt)
            if composed:
                return composed
        except Exception:
            pass
    return summary


def _render_career_arc(positions: list, use_llm: bool, llm_fn) -> str:
    sorted_pos = list(positions)
    lines = []
    for p in sorted_pos:
        dates = " — ".join(d for d in (p["started_on"], p["finished_on"]) if d)
        if not dates:
            dates = "(undated)"
        header = (
            f"### {p['title']} at {p['company']}" if p["title"]
            else f"### {p['company']}"
        )
        lines.append(header)
        lines.append(f"*{dates}*" + (f" · {p['location']}" if p["location"] else ""))
        if p["description"]:
            if use_llm and llm_fn:
                prompt = (
                    f"Summarize this role description in 1-2 sentences "
                    f"(present tense, third-person, grounded in the input).\n\n"
                    f"Role: {p['title']} at {p['company']}\n"
                    f"Description: {p['description']}"
                )
                try:
                    composed = llm_fn(prompt)
                    if composed:
                        lines.append("")
                        lines.append(composed)
                    else:
                        lines.append("")
                        lines.append(p["description"])
                except Exception:
                    lines.append("")
                    lines.append(p["description"])
            else:
                lines.append("")
                lines.append(p["description"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_education(education: list, use_llm: bool, llm_fn) -> str:
    lines = []
    for e in education:
        title = e["school"]
        if e["degree"]:
            title = f"{e['degree']} — {title}"
        dates = " — ".join(d for d in (e["started_on"], e["finished_on"]) if d)
        if not dates:
            dates = "(undated)"
        lines.append(f"### {title}")
        lines.append(f"*{dates}*")
        if e["notes"]:
            lines.append("")
            lines.append(f"Notes: {e['notes']}")
        if e["activities"]:
            lines.append("")
            lines.append(f"Activities: {e['activities']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_skills(skills: list) -> str:
    lines = [f"- {s}" for s in skills]
    return "\n".join(lines).rstrip() + "\n"


def _render_languages(languages: list) -> str:
    lines = [f"- {lng}" for lng in languages]
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
