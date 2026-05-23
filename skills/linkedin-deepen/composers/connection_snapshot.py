# <!-- CDD-CONTRACT: ~/gbrain/skills/linkedin-deepen/composers/connection_snapshot.py -->
# Contract:   Phase 2 — per Connections.csv row, find target page via
#             linkedin_url alias and write ## LinkedIn Profile Snapshot
#             section. NO new pages created.
# Inputs:     source_dir, brain_root, llm_prose flag, registry, counters,
#             max_workers (default 1 — single-threaded preserves v0.1.0
#             behavior; max_workers=2 uses ThreadPoolExecutor for
#             dual-instance LM Studio round-robin to actually parallelize).
# Outputs:    Per-connection pages mutated; pages_touched + sections_added
#             counters incremented.
# Invariants:
#             - No new pages created (only existing pages writeable).
#             - LinkedIn-Member placeholder rows skipped silently.
#             - Idempotent via per-(page, section, row-hash) registry.
#             - Section-exists early skip: if target page already has a
#               linkedin-deepen-authored Snapshot section (provenance tag
#               matches PROVENANCE_PREFIX), skip without LLM call. Catches
#               kill-mid-run + restart on the ~360 already-touched pages.
#             - Thread safety: shared counter / registry mutations guarded
#               by registry's internal lock + a module-level Counter lock.
# Idempotent: yes. Section-exists check + registry hash both protect.

"""Phase 2 composer: per-connection LinkedIn Profile Snapshot."""

from __future__ import annotations

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from _shared.csv_utils import iter_rows, clean, stable_row_hash
from _shared.dedup_proxy import find_existing_person_by_linkedin_url
from _shared.page_io import (
    read_page,
    write_page,
    replace_or_append_section,
)


# Stable byte-prefix from _shared/llm_prose.py provenance footer.
# Format (v0.1.0+): `*[LLM-composed from LinkedIn structured data; role=...; model=...; ts=...]*`
PROVENANCE_PREFIX = "*[LLM-composed from LinkedIn structured data"
SNAPSHOT_HEADER = "## LinkedIn Profile Snapshot"


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
    max_workers: int = 1,
) -> None:
    counters = counters if counters is not None else Counter()
    warnings = warnings if warnings is not None else Counter()
    error_classes = error_classes if error_classes is not None else Counter()

    path = source_dir / "Connections.csv"
    if not path.exists():
        counters["phase2_csv_missing"] += 1
        return

    counters["phase2_concurrent_max_workers"] = max(int(max_workers), 1)
    counter_lock = threading.Lock()
    touched_pages: set = set()

    # Single-threaded pre-filter: build list of qualifying tasks.
    tasks = []
    for row in iter_rows(
        path,
        ["First Name", "first name", "Email Address", "email address"],
    ):
        first = clean(row.get("First Name"))
        last = clean(row.get("Last Name"))
        url = clean(row.get("URL"))
        company = clean(row.get("Company"))
        position = clean(row.get("Position"))
        connected_on = clean(row.get("Connected On"))

        counters["phase2_rows_seen"] += 1

        if first == "LinkedIn" and last == "Member":
            counters["phase2_linkedin_member_skipped"] += 1
            continue
        if not url:
            counters["phase2_no_url_skipped"] += 1
            continue

        target = find_existing_person_by_linkedin_url(url, brain_root)
        if target is None:
            counters["phase2_no_target_page"] += 1
            continue

        row_hash = stable_row_hash([url, position, company, connected_on])
        section_key = _key(str(target), "snapshot", row_hash)

        # Registry hash check (cheap; in-memory).
        if registry is not None and registry.was_applied(section_key):
            counters["phase2_idempotent_skip"] += 1
            continue

        if dry_run:
            counters["phase2_dry_run_pending"] += 1
            continue

        tasks.append({
            "first": first, "last": last, "url": url, "company": company,
            "position": position, "connected_on": connected_on,
            "target": target, "section_key": section_key,
        })

    if not tasks:
        return

    def _work(task) -> str:
        """Per-row worker. Returns target path string on success-or-skip."""
        target = task["target"]
        try:
            fm, body = read_page(target)
            if fm is None:
                with counter_lock:
                    warnings["phase2_no_frontmatter"] += 1
                return ""

            # Section-exists early skip (saves LLM call on already-deepened pages).
            existing_section = _extract_section(body, SNAPSHOT_HEADER)
            if existing_section is not None:
                if PROVENANCE_PREFIX in existing_section:
                    with counter_lock:
                        counters["phase2_provenance_skip"] += 1
                    if registry is not None:
                        registry.mark_applied(task["section_key"])
                    return str(target)
                # Section present but no provenance — user-authored; preserve.
                with counter_lock:
                    counters["phase2_user_authored_skip"] += 1
                if registry is not None:
                    registry.mark_applied(task["section_key"])
                return str(target)

            # Compose + write.
            content = _render_snapshot(
                task["first"], task["last"], task["url"],
                task["company"], task["position"], task["connected_on"],
                use_llm=llm_prose_local, llm_fn=llm_compose_fn,
            )
            body = replace_or_append_section(body, SNAPSHOT_HEADER, content)
            write_page(target, fm, body)
            if registry is not None:
                registry.mark_applied(task["section_key"])
            with counter_lock:
                counters["phase2_sections_written"] += 1
            return str(target)
        except Exception:
            with counter_lock:
                error_classes["phase2_write_failed"] += 1
            return ""

    workers = max(int(max_workers), 1)
    if workers == 1:
        for task in tasks:
            result = _work(task)
            if result:
                touched_pages.add(result)
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_work, task) for task in tasks]
            for fut in as_completed(futures):
                result = fut.result()
                if result:
                    touched_pages.add(result)

    counters["phase2_pages_touched"] += len(touched_pages)


def _extract_section(body: str, header: str) -> Optional[str]:
    """Return the body of `header`'s section (text between `header` and the
    next H2 / EOF), or None if the header isn't present."""
    if header not in body:
        return None
    # Find the section start.
    idx = body.find(header)
    if idx == -1:
        return None
    # Section content begins after the header line.
    after = body[idx + len(header):]
    # Find the next H2 (start-of-line "## ") or EOF.
    nl = 0
    while True:
        nl = after.find("\n## ", nl)
        if nl == -1:
            return after
        # Must be a real H2 (newline-prefixed), not "##" embedded.
        return after[:nl]


def _render_snapshot(
    first, last, url, company, position, connected_on,
    *, use_llm: bool, llm_fn,
) -> str:
    full_name = " ".join(p for p in (first, last) if p)
    bullets = []
    if position:
        bullets.append(f"- **Position:** {position}")
    if company:
        bullets.append(f"- **Company:** {company}")
    if url:
        bullets.append(f"- **LinkedIn:** {url}")
    if connected_on:
        bullets.append(f"- **Connected on:** {connected_on}")
    structured = "\n".join(bullets) if bullets else "- (no data)"

    if use_llm and llm_fn and (position or company):
        prompt = (
            f"Write a 1-sentence professional summary of this person, "
            f"grounded ONLY in the input. Third-person, present tense, "
            f"factual. Do not invent skills or beliefs.\n\n"
            f"Name: {full_name}\n"
            f"Position: {position or ''}\n"
            f"Company: {company or ''}\n"
            f"Connected on LinkedIn: {connected_on or 'unknown'}"
        )
        try:
            composed = llm_fn(prompt)
            if composed:
                return composed.rstrip() + "\n\n" + structured + "\n"
        except Exception:
            pass

    return structured + "\n"


def _key(page: str, section: str, row_hash: str) -> str:
    import hashlib
    h = hashlib.sha256()
    h.update(page.encode("utf-8"))
    h.update(b"\x1f")
    h.update(section.encode("utf-8"))
    h.update(b"\x1f")
    h.update(row_hash.encode("utf-8"))
    return h.hexdigest()[:16]
