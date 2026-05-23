# <!-- CDD-CONTRACT: ~/gbrain/skills/linkedin-bootstrap/dedup.py -->
# Contract:   LinkedIn-side dedup extension of the apple-contacts D49 alias
#             mechanism. Adds linkedin_url as a canonical signal +
#             two in-place page-modification helpers for alias-merge.
# Inputs:     existing brain pages directory + LinkedIn record fields.
# Outputs:    page-path-or-None for dedup matches; modified page text for
#             alias-merge.
# Invariants:
#             - Canonical precedence: linkedin_url > email > DIN-5007-2 name.
#             - URL-based dedup is exact-match on canonical LinkedIn URL
#               (trailing slash stripped; lowercase host).
#             - Email-based dedup uses _shared/idempotency frontmatter scan.
#             - add_alias_to_existing_page is idempotent: re-adding an
#               already-present alias is a no-op.
# Idempotent: yes.

"""LinkedIn dedup engine.

Extends apple-contacts-bootstrap's dedup.py mechanism with:
  - find_existing_person_by_linkedin_url(url, brain_root)
  - find_existing_person_by_email(email, brain_root)
  - find_existing_person_by_phone(phone, brain_root)
  - find_existing_person_by_name(name, brain_root)
  - add_alias_to_existing_page(page_path, alias_kind, alias_value)
  - add_frontmatter_field(page_path, field_name, field_value)
  - normalize_linkedin_url(url) — canonical form for exact-match
  - normalize_email(email)
  - normalize_phone(phone)
  - normalized_name_key(name) — re-exported from apple-contacts dedup.py

The page-modification helpers operate on raw text to preserve
hand-authored body content above the line (per Anti-Pattern: never
overwrite user-authored body).
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path
from typing import Optional, Tuple

# === _shared/ import bootstrap ==========================================
# Reuse apple-contacts dedup primitives (DIN 5007-2 + Union-Find +
# normalize_email + normalize_phone). Import from apple-contacts-bootstrap/dedup.py.
_SKILLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SKILLS_DIR))

try:
    # Try the apple-contacts-bootstrap sibling first.
    from apple_contacts_bootstrap_dedup import (  # type: ignore
        normalize_email as _shared_normalize_email,
        normalize_phone as _shared_normalize_phone,
        normalized_name_key as _shared_normalized_name_key,
    )
except ImportError:
    # The actual apple-contacts-bootstrap dedup.py is not importable as a
    # module (it's a sibling-script file, not a package), so re-implement
    # the small subset of normalization helpers here. This is a controlled
    # copy of the apple-contacts dedup.py public-API functions.
    _DIN_5007_2 = {
        "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
        "Ä": "ae", "Ö": "oe", "Ü": "ue",
    }

    def _shared_normalize_email(addr):
        if not addr:
            return ""
        return str(addr).strip().lower()

    def _shared_normalize_phone(num):
        if not num:
            return ""
        s = str(num).strip()
        digits = re.sub(r"[^0-9]", "", s)
        if not digits:
            return ""
        return ("+" + digits) if s.startswith("+") else digits

    def _shared_normalized_name_key(s):
        if not s:
            return ""
        for k, v in _DIN_5007_2.items():
            s = s.replace(k, v)
        s = unicodedata.normalize("NFKD", s)
        s = "".join(c for c in s if not unicodedata.combining(c))
        s = s.lower()
        s = re.sub(r"[^a-z0-9]+", "-", s)
        s = re.sub(r"-+", "-", s).strip("-")
        return s


# === Public normalization API ===========================================

def normalize_email(addr: Optional[str]) -> str:
    """Lowercase + trim. Empty / None → empty string."""
    return _shared_normalize_email(addr)


def normalize_phone(num: Optional[str]) -> str:
    """Strip non-digits; preserve leading + if present."""
    return _shared_normalize_phone(num)


def normalized_name_key(s: Optional[str]) -> str:
    """DIN 5007-2 + NFKD + kebab-case ASCII slug key."""
    return _shared_normalized_name_key(s)


_LINKEDIN_URL_PATTERN = re.compile(
    r"^(?:https?://)?(?:www\.)?linkedin\.com/in/([^/?#]+)/?",
    re.IGNORECASE,
)


def normalize_linkedin_url(url: Optional[str]) -> str:
    """Canonical form: 'https://www.linkedin.com/in/<slug>'.
    Strips protocol prefix variants + trailing slash + query string.
    Returns '' if URL doesn't match the LinkedIn /in/ pattern."""
    if not url:
        return ""
    m = _LINKEDIN_URL_PATTERN.match(str(url).strip())
    if not m:
        return ""
    slug = m.group(1).strip().lower()
    if not slug:
        return ""
    return f"https://www.linkedin.com/in/{slug}"


# === Dedup lookup against existing brain pages =========================

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_ALIASES_FIELD_RE = re.compile(
    r"^aliases\s*:\s*(\[.*?\]|\".*?\"|'.*?'|.+)$", re.MULTILINE
)


def _read_frontmatter(page_path: Path) -> Optional[str]:
    try:
        with page_path.open("r", encoding="utf-8") as f:
            head = f.read(8192)
    except OSError:
        return None
    m = _FRONTMATTER_RE.match(head)
    if not m:
        return None
    return m.group(1)


def find_existing_person_by_linkedin_url(
    url: str, brain_root: Path,
) -> Optional[Path]:
    """Scan `brain_root/people/**/*.md` for a page with this LinkedIn
    URL in its frontmatter (either as `linkedin_url:` field or in
    `aliases:` list). Returns first-match Path or None."""
    canon = normalize_linkedin_url(url)
    if not canon:
        return None
    people_dir = brain_root / "people"
    if not people_dir.exists():
        return None
    # Scan for both the canonical form AND the raw URL variants
    # (with/without protocol, trailing slash) for robustness.
    raw = url.strip()
    for page_path in people_dir.rglob("*.md"):
        fm = _read_frontmatter(page_path)
        if not fm:
            continue
        if canon in fm or raw in fm:
            return page_path
    return None


def find_existing_person_by_email(
    email: str, brain_root: Path,
) -> Optional[Path]:
    """Scan people/**/*.md for a page mentioning this email in
    frontmatter (most commonly `email:` field or inside `aliases:`).
    Returns first-match Path or None."""
    norm = normalize_email(email)
    if not norm:
        return None
    people_dir = brain_root / "people"
    if not people_dir.exists():
        return None
    for page_path in people_dir.rglob("*.md"):
        fm = _read_frontmatter(page_path)
        if not fm:
            continue
        if norm in fm.lower():
            return page_path
    return None


def find_existing_person_by_phone(
    phone: str, brain_root: Path,
) -> Optional[Path]:
    """Scan people/**/*.md for a page mentioning this normalized phone
    in frontmatter. Returns first-match Path or None."""
    norm = normalize_phone(phone)
    if not norm or len(norm) < 7:
        return None
    people_dir = brain_root / "people"
    if not people_dir.exists():
        return None
    digits_only = re.sub(r"[^0-9]", "", norm)
    for page_path in people_dir.rglob("*.md"):
        fm = _read_frontmatter(page_path)
        if not fm:
            continue
        # Match either the +-prefixed or digits-only form
        if norm in fm or digits_only in fm:
            return page_path
    return None


def find_existing_person_by_name(
    name: str, brain_root: Path,
) -> Optional[Path]:
    """Slug-based lookup by DIN 5007-2 normalized name key.
    Returns first-match Path or None. Used as the LAST resort after
    URL + email + phone dedup signals failed.
    """
    key = normalized_name_key(name)
    if not key:
        return None
    people_dir = brain_root / "people"
    if not people_dir.exists():
        return None
    # Direct slug match: people/*/<key>.md
    for sub in people_dir.iterdir():
        if not sub.is_dir():
            continue
        candidate = sub / f"{key}.md"
        if candidate.exists():
            return candidate
    # Top-level (people/<key>.md)
    candidate = people_dir / f"{key}.md"
    if candidate.exists():
        return candidate
    return None


def find_existing_institution_by_name(
    name: str, brain_root: Path,
) -> Optional[Path]:
    """Slug-based lookup in institutions/."""
    key = normalized_name_key(name)
    if not key:
        return None
    inst_dir = brain_root / "institutions"
    if not inst_dir.exists():
        return None
    candidate = inst_dir / f"{key}.md"
    if candidate.exists():
        return candidate
    # Some institutions live in nested dirs; do a shallow rglob.
    for p in inst_dir.rglob(f"{key}.md"):
        return p
    return None


# === In-place page-modification helpers ================================

def add_alias_to_existing_page(
    page_path: Path, alias_value: str,
) -> bool:
    """Add an alias to the page's frontmatter `aliases:` list (creating
    the field if absent). Idempotent: returns False if alias was
    already present, True if it was added.

    Preserves all body content above and below the line. Only the
    `aliases:` field in the frontmatter is rewritten.
    """
    if not alias_value:
        return False
    try:
        text = page_path.read_text(encoding="utf-8")
    except OSError:
        return False

    fm_match = _FRONTMATTER_RE.match(text)
    if not fm_match:
        return False
    fm_body = fm_match.group(1)
    fm_end = fm_match.end()
    rest = text[fm_end:]

    # Check if already present in any field. Cheap substring check.
    if alias_value in fm_body:
        return False

    # Locate aliases: field
    aliases_match = _ALIASES_FIELD_RE.search(fm_body)
    if aliases_match:
        line_text = aliases_match.group(0)
        value_text = aliases_match.group(1).strip()
        # Parse existing list (best-effort; YAML list inline or empty).
        items = _parse_inline_yaml_list(value_text)
        if alias_value in items:
            return False
        items.append(alias_value)
        new_value = _render_inline_yaml_list(items)
        new_line = f"aliases: {new_value}"
        new_fm_body = fm_body[:aliases_match.start()] + new_line \
            + fm_body[aliases_match.end():]
    else:
        # Add an aliases line at the end of frontmatter.
        new_fm_body = fm_body.rstrip() + f"\naliases: [\"{_escape(alias_value)}\"]"

    new_text = f"---\n{new_fm_body}\n---\n" + rest
    page_path.write_text(new_text, encoding="utf-8")
    return True


def add_frontmatter_field(
    page_path: Path, field_name: str, field_value,
) -> bool:
    """Add or update a frontmatter field. Idempotent: returns False if
    the value was already the same; True if added or changed.

    Preserves body content. Only the frontmatter scalar field changes.
    """
    if not field_name:
        return False
    try:
        text = page_path.read_text(encoding="utf-8")
    except OSError:
        return False

    fm_match = _FRONTMATTER_RE.match(text)
    if not fm_match:
        return False
    fm_body = fm_match.group(1)
    fm_end = fm_match.end()
    rest = text[fm_end:]

    rendered_value = _render_scalar(field_value)
    field_re = re.compile(
        rf"^{re.escape(field_name)}\s*:\s*(.+?)\s*$", re.MULTILINE
    )
    m = field_re.search(fm_body)
    if m:
        current = m.group(1).strip()
        if current == rendered_value.strip():
            return False
        new_line = f"{field_name}: {rendered_value}"
        new_fm_body = fm_body[:m.start()] + new_line + fm_body[m.end():]
    else:
        new_fm_body = fm_body.rstrip() + f"\n{field_name}: {rendered_value}"

    new_text = f"---\n{new_fm_body}\n---\n" + rest
    page_path.write_text(new_text, encoding="utf-8")
    return True


def append_timeline_entry(
    page_path: Path, entry_text: str,
) -> bool:
    """Append a Timeline entry to the page (under the `## Timeline`
    section below the line). Creates the Timeline section if absent.
    Idempotent: returns False if the exact entry_text is already
    present in the Timeline section; True if added.

    entry_text format: '- **YYYY-MM-DD** | <text>'
    """
    if not entry_text:
        return False
    try:
        text = page_path.read_text(encoding="utf-8")
    except OSError:
        return False

    if entry_text in text:
        return False

    # Find ## Timeline section
    timeline_re = re.compile(r"^## Timeline\s*$", re.MULTILINE)
    m = timeline_re.search(text)
    if m:
        # Insert at end of Timeline section (before next ## or EOF).
        next_section_re = re.compile(r"^## ", re.MULTILINE)
        end_match = next_section_re.search(text, m.end())
        insert_at = end_match.start() if end_match else len(text)
        # Trim trailing whitespace before insert point.
        before = text[:insert_at].rstrip("\n") + "\n"
        after = text[insert_at:]
        new_text = before + entry_text.rstrip() + "\n\n" + after
    else:
        # Append a new Timeline section at EOF.
        new_text = text.rstrip("\n") + "\n\n---\n\n## Timeline\n\n" \
            + entry_text.rstrip() + "\n"

    page_path.write_text(new_text, encoding="utf-8")
    return True


# === Helpers ===========================================================

def _parse_inline_yaml_list(value_text: str) -> list:
    """Best-effort parse of an inline YAML list. Handles:
      []  → []
      ["a", "b"]  → ["a", "b"]
      [a, b]  → ["a", "b"]
      "single"  → ["single"]
    Returns the list (possibly empty)."""
    s = value_text.strip()
    if not s or s == "[]":
        return []
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        items = []
        # Split on commas not inside quotes
        for part in _split_top_level(inner, ","):
            part = part.strip()
            if part.startswith('"') and part.endswith('"'):
                part = part[1:-1]
            elif part.startswith("'") and part.endswith("'"):
                part = part[1:-1]
            if part:
                items.append(part)
        return items
    # Single scalar
    if (s.startswith('"') and s.endswith('"')) or \
       (s.startswith("'") and s.endswith("'")):
        return [s[1:-1]]
    return [s]


def _split_top_level(s: str, sep: str) -> list:
    """Split on `sep` not inside quotes."""
    out = []
    buf = []
    in_q = None
    for ch in s:
        if in_q:
            buf.append(ch)
            if ch == in_q:
                in_q = None
        elif ch in ("'", '"'):
            in_q = ch
            buf.append(ch)
        elif ch == sep:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return out


def _render_inline_yaml_list(items: list) -> str:
    if not items:
        return "[]"
    return "[" + ", ".join(f'"{_escape(i)}"' for i in items) + "]"


def _render_scalar(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return _render_inline_yaml_list([str(x) for x in v])
    s = str(v)
    return f'"{_escape(s)}"'


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')
