# <!-- CDD-CONTRACT: ~/gbrain/skills/linkedin-deepen/_shared/page_io.py -->
# Contract:   section-aware page reader/writer for linkedin-deepen.
#             Replace-or-append section content under skill-owned headers.
# Inputs:     page path, section header text, new section body string.
# Outputs:    page text mutated in-place; preserves frontmatter + body
#             content above the line + body content under non-owned
#             headers.
# Invariants:
#             - User-authored content above the line is preserved
#               byte-for-byte unless the matching section header is
#               in OWNED_HEADERS.
#             - Timeline entries are deduplicated by exact-substring match.
#             - Frontmatter `aliases:` list is parsed/rewritten preserving
#               other frontmatter fields.
# Idempotent: yes.

"""Section-aware page I/O for linkedin-deepen.

The skill writes inside named sections only (`## LinkedIn Profile Snapshot`,
`## Career Arc (from LinkedIn)`, etc.); body content under non-owned
headers is preserved.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Tuple


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_OWNED_SECTION_PREFIXES = (
    "## LinkedIn Profile Snapshot",
    "## Career Arc (from LinkedIn)",
    "## Education (from LinkedIn)",
    "## Skills (from LinkedIn)",
    "## Languages",
    "## Executive Summary",
    "## What They Engage On (LinkedIn signal)",
    "## What They Amplify (LinkedIn shares)",
    "## Watch List (LinkedIn follows)",
    "## Operator's Role Here",
    "## LinkedIn Connections Employed Here",
)


def read_page(path: Path) -> Tuple[Optional[str], str]:
    """Return (frontmatter_text, body_text). Frontmatter is None if absent."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return (None, "")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return (None, text)
    return (m.group(1), text[m.end():])


def write_page(path: Path, frontmatter_text: Optional[str], body: str) -> None:
    if frontmatter_text is None:
        path.write_text(body, encoding="utf-8")
        return
    out = f"---\n{frontmatter_text}\n---\n{body}"
    path.write_text(out, encoding="utf-8")


def replace_or_append_section(
    body: str, section_header: str, new_section_body: str,
) -> str:
    """Replace existing section (matching its header) OR append a new
    section at end-of-page.

    section_header MUST be one of OWNED_SECTION_PREFIXES (asserted).
    Returns new body string.
    """
    assert any(section_header.startswith(p) for p in _OWNED_SECTION_PREFIXES), \
        f"refusing to write non-owned section header: {section_header!r}"
    section_re = re.compile(
        rf"^{re.escape(section_header)}\s*$\n([\s\S]*?)(?=^## |\Z)",
        re.MULTILINE,
    )
    rendered = section_header + "\n\n" + new_section_body.rstrip() + "\n\n"
    m = section_re.search(body)
    if m:
        return body[:m.start()] + rendered + body[m.end():]
    sep = "" if body.endswith("\n\n") else ("\n" if body.endswith("\n") else "\n\n")
    return body.rstrip("\n") + "\n\n" + rendered


def append_timeline_entry(path: Path, entry_line: str) -> bool:
    """Append a Timeline entry to a page. Idempotent: returns False if
    the exact entry_line already present anywhere in the file.

    entry_line format: '- **<date-iso>** | <text>'
    """
    if not entry_line:
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if entry_line in text:
        return False
    timeline_re = re.compile(r"^## Timeline\s*$", re.MULTILINE)
    m = timeline_re.search(text)
    if m:
        next_section_re = re.compile(r"^## ", re.MULTILINE)
        end_match = next_section_re.search(text, m.end())
        insert_at = end_match.start() if end_match else len(text)
        before = text[:insert_at].rstrip("\n") + "\n"
        after = text[insert_at:]
        new_text = before + entry_line.rstrip() + "\n\n" + after
    else:
        new_text = (
            text.rstrip("\n") + "\n\n---\n\n## Timeline\n\n"
            + entry_line.rstrip() + "\n"
        )
    path.write_text(new_text, encoding="utf-8")
    return True


def add_frontmatter_field(
    path: Path, field_name: str, field_value,
) -> bool:
    """Add or update a frontmatter scalar field. Idempotent."""
    if not field_name:
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return False
    fm_body = m.group(1)
    fm_end = m.end()
    rest = text[fm_end:]
    rendered_value = _render_scalar(field_value)
    field_re = re.compile(
        rf"^{re.escape(field_name)}\s*:\s*(.+?)\s*$", re.MULTILINE
    )
    fm = field_re.search(fm_body)
    if fm:
        current = fm.group(1).strip()
        if current == rendered_value.strip():
            return False
        new_line = f"{field_name}: {rendered_value}"
        new_fm_body = fm_body[:fm.start()] + new_line + fm_body[fm.end():]
    else:
        new_fm_body = fm_body.rstrip() + f"\n{field_name}: {rendered_value}"
    new_text = f"---\n{new_fm_body}\n---\n" + rest
    path.write_text(new_text, encoding="utf-8")
    return True


def _render_scalar(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(f'"{_escape(str(x))}"' for x in v) + "]"
    s = str(v)
    return f'"{_escape(s)}"'


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')
