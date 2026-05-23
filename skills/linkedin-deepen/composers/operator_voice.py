# <!-- CDD-CONTRACT: ~/gbrain/skills/linkedin-deepen/composers/operator_voice.py -->
# Contract:   Phase 5 — Comments_*.csv + Shares.csv → operator page
#             skill-owned sections (## What They Engage On / ## What
#             They Amplify) + Timeline entries.
# Inputs:     source_dir, operator_page, registry, counters.
# Outputs:    operator_page mutated; NO entity-page writes from these CSVs.
# Invariants:
#             - NO counterparty cross-link (operator scope lock S191-D119+1).
#             - NEVER opens messages.csv (SKIP_LIST).
#             - NEVER opens Reactions.csv (SKIP_LIST).
# Idempotent: yes.

"""Phase 5 composer: operator voice (Comments + Shares only)."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Optional

from _shared.csv_utils import iter_rows, clean
from _shared.page_io import (
    read_page,
    write_page,
    replace_or_append_section,
    append_timeline_entry,
)


_MAX_ITEMS_PER_SECTION = 50


def run(
    source_dir: Path,
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

    comments = list(_read_comments(source_dir))
    shares = list(_read_shares(source_dir))
    counters["phase5_comments_seen"] += len(comments)
    counters["phase5_shares_seen"] += len(shares)

    # Sections
    if comments:
        c_hash = _hash_items([(c["date"], c["text"][:80]) for c in comments])
        section_key = _key(str(operator_page), "engage-on", c_hash)
        if registry is not None and registry.was_applied(section_key):
            counters["phase5_engage_section_idempotent_skip"] += 1
        elif dry_run:
            counters["phase5_engage_section_dry_run_pending"] += 1
        else:
            try:
                fm, body = read_page(operator_page)
                if fm is not None:
                    content = _render_comments(comments)
                    body = replace_or_append_section(
                        body, "## What They Engage On (LinkedIn signal)",
                        content,
                    )
                    write_page(operator_page, fm, body)
                    if registry is not None:
                        registry.mark_applied(section_key)
                    counters["phase5_engage_section_written"] += 1
            except Exception:
                error_classes["phase5_engage_section_write_failed"] += 1

    if shares:
        s_hash = _hash_items([(s["date"], s["commentary"][:80]) for s in shares])
        section_key = _key(str(operator_page), "amplify", s_hash)
        if registry is not None and registry.was_applied(section_key):
            counters["phase5_amplify_section_idempotent_skip"] += 1
        elif dry_run:
            counters["phase5_amplify_section_dry_run_pending"] += 1
        else:
            try:
                fm, body = read_page(operator_page)
                if fm is not None:
                    content = _render_shares(shares)
                    body = replace_or_append_section(
                        body, "## What They Amplify (LinkedIn shares)",
                        content,
                    )
                    write_page(operator_page, fm, body)
                    if registry is not None:
                        registry.mark_applied(section_key)
                    counters["phase5_amplify_section_written"] += 1
            except Exception:
                error_classes["phase5_amplify_section_write_failed"] += 1


def _read_comments(source_dir: Path):
    for path in sorted(source_dir.glob("Comments_*.csv")):
        for row in iter_rows(path, ["Date", "Link", "Message", "date"]):
            date = clean(row.get("Date")) or ""
            text = clean(row.get("Message")) or ""
            link = clean(row.get("Link")) or ""
            if not text and not link:
                continue
            yield {"date": date, "text": text, "link": link}


def _read_shares(source_dir: Path):
    for row in iter_rows(
        source_dir / "Shares.csv",
        ["Date", "ShareLink", "ShareCommentary", "Visibility"],
    ):
        date = clean(row.get("Date")) or ""
        commentary = clean(row.get("ShareCommentary")) or ""
        share_link = clean(row.get("ShareLink")) or ""
        shared_url = clean(row.get("SharedUrl")) or ""
        if not commentary and not share_link and not shared_url:
            continue
        yield {
            "date": date,
            "commentary": commentary,
            "share_link": share_link,
            "shared_url": shared_url,
        }


def _render_comments(comments) -> str:
    items = sorted(comments, key=lambda x: x["date"] or "", reverse=True)[:_MAX_ITEMS_PER_SECTION]
    lines = []
    for c in items:
        d = c["date"] or "(undated)"
        text_short = c["text"][:200] + ("…" if len(c["text"]) > 200 else "")
        lines.append(f"- **{d}** · {text_short} (via LinkedIn)")
    if len(comments) > _MAX_ITEMS_PER_SECTION:
        lines.append(f"")
        lines.append(
            f"*+{len(comments) - _MAX_ITEMS_PER_SECTION} more comments — full set in source export.*"
        )
    return "\n".join(lines).rstrip() + "\n"


def _render_shares(shares) -> str:
    items = sorted(shares, key=lambda x: x["date"] or "", reverse=True)[:_MAX_ITEMS_PER_SECTION]
    lines = []
    for s in items:
        d = s["date"] or "(undated)"
        com = s["commentary"][:200] + ("…" if len(s["commentary"]) > 200 else "")
        if com:
            lines.append(f"- **{d}** · {com} (via LinkedIn)")
        else:
            url = s["shared_url"] or s["share_link"]
            lines.append(f"- **{d}** · shared: {url} (via LinkedIn)")
    if len(shares) > _MAX_ITEMS_PER_SECTION:
        lines.append("")
        lines.append(
            f"*+{len(shares) - _MAX_ITEMS_PER_SECTION} more shares — full set in source export.*"
        )
    return "\n".join(lines).rstrip() + "\n"


def _hash_items(items) -> str:
    import hashlib
    h = hashlib.sha256()
    for k, v in items:
        h.update((k or "").encode("utf-8"))
        h.update(b"\x1f")
        h.update((v or "").encode("utf-8"))
        h.update(b"\x1f\x1e")
    return h.hexdigest()[:16]


def _key(page: str, section: str, content_hash_input: str) -> str:
    import hashlib
    h = hashlib.sha256()
    h.update(page.encode("utf-8"))
    h.update(b"\x1f")
    h.update(section.encode("utf-8"))
    h.update(b"\x1f")
    h.update((content_hash_input or "").encode("utf-8"))
    return h.hexdigest()[:16]
