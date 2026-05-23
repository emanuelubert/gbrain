# <!-- CDD-CONTRACT: ~/gbrain/skills/linkedin-deepen/_shared/dedup_proxy.py -->
# Contract:   thin wrapper around linkedin-bootstrap's dedup.py primitives.
#             Loads the sibling skill's dedup module via importlib so we
#             reuse the canonical apple-contacts D49 alias mechanism +
#             linkedin_url canonical signal without re-implementing.
# Inputs:     LinkedIn URL / email / phone / name; brain_root Path.
# Outputs:    Path-or-None for existing-page lookups; bool for alias-merge.
# Edge:       If linkedin-bootstrap is not importable (skill missing),
#             provide minimal local fallbacks (URL normalize + frontmatter
#             scan).
# Idempotent: pure (no side effects); page-modify helpers are idempotent.

"""Dedup primitive proxy.

Loads linkedin-bootstrap/dedup.py via importlib so this skill stays
self-contained but doesn't duplicate the dedup engine.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Optional


_LINKEDIN_BOOTSTRAP_DEDUP = (
    Path.home() / "gbrain" / "skills" / "linkedin-bootstrap" / "dedup.py"
)


def _load_dedup_module():
    """Load linkedin-bootstrap/dedup.py as a module (importlib).
    Returns the module or None if not present."""
    if not _LINKEDIN_BOOTSTRAP_DEDUP.exists():
        return None
    # The dedup.py module does `sys.path.insert(0, parent.parent)` and
    # imports from `_shared/`. We respect that path setup by running it
    # in its own location context.
    saved = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location(
            "linkedin_bootstrap_dedup",
            _LINKEDIN_BOOTSTRAP_DEDUP,
        )
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.path.insert(
            0, str(_LINKEDIN_BOOTSTRAP_DEDUP.parent.parent)
        )
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None
    finally:
        sys.path[:] = saved


_DEDUP = _load_dedup_module()


# === Public API ============================================================

def find_existing_person_by_linkedin_url(
    url: str, brain_root: Path,
) -> Optional[Path]:
    if _DEDUP is not None:
        return _DEDUP.find_existing_person_by_linkedin_url(url, brain_root)
    return _fallback_find_person_by_linkedin_url(url, brain_root)


def find_existing_person_by_name(
    name: str, brain_root: Path,
) -> Optional[Path]:
    if _DEDUP is not None:
        return _DEDUP.find_existing_person_by_name(name, brain_root)
    return _fallback_find_person_by_name(name, brain_root)


def find_existing_institution_by_name(
    name: str, brain_root: Path,
) -> Optional[Path]:
    if _DEDUP is not None:
        return _DEDUP.find_existing_institution_by_name(name, brain_root)
    return _fallback_find_institution_by_name(name, brain_root)


def normalize_linkedin_url(url: str) -> str:
    if _DEDUP is not None:
        return _DEDUP.normalize_linkedin_url(url)
    return _fallback_normalize_linkedin_url(url)


def normalized_name_key(s: str) -> str:
    if _DEDUP is not None:
        return _DEDUP.normalized_name_key(s)
    return _fallback_normalize_key(s)


def normalize_email(e: str) -> str:
    if _DEDUP is not None:
        return _DEDUP.normalize_email(e)
    return (e or "").strip().lower()


# === Local fallbacks (used only if linkedin-bootstrap missing) =============

_LINKEDIN_URL_PATTERN = re.compile(
    r"^(?:https?://)?(?:www\.)?linkedin\.com/in/([^/?#]+)/?",
    re.IGNORECASE,
)
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _fallback_normalize_linkedin_url(url: str) -> str:
    if not url:
        return ""
    m = _LINKEDIN_URL_PATTERN.match(str(url).strip())
    if not m:
        return ""
    slug = m.group(1).strip().lower()
    if not slug:
        return ""
    return f"https://www.linkedin.com/in/{slug}"


def _fallback_normalize_key(s: str) -> str:
    if not s:
        return ""
    import unicodedata
    s = s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def _fallback_find_person_by_linkedin_url(url, brain_root):
    canon = _fallback_normalize_linkedin_url(url)
    if not canon:
        return None
    people = brain_root / "people"
    if not people.exists():
        return None
    raw = (url or "").strip()
    for p in people.rglob("*.md"):
        try:
            head = p.read_text(encoding="utf-8")[:8192]
        except OSError:
            continue
        m = _FRONTMATTER_RE.match(head)
        if not m:
            continue
        fm = m.group(1)
        if canon in fm or (raw and raw in fm):
            return p
    return None


def _fallback_find_person_by_name(name, brain_root):
    key = _fallback_normalize_key(name)
    if not key:
        return None
    people = brain_root / "people"
    if not people.exists():
        return None
    for sub in people.iterdir():
        if sub.is_dir():
            candidate = sub / f"{key}.md"
            if candidate.exists():
                return candidate
    candidate = people / f"{key}.md"
    if candidate.exists():
        return candidate
    return None


def _fallback_find_institution_by_name(name, brain_root):
    key = _fallback_normalize_key(name)
    if not key:
        return None
    inst = brain_root / "institutions"
    if not inst.exists():
        return None
    candidate = inst / f"{key}.md"
    if candidate.exists():
        return candidate
    for p in inst.rglob(f"{key}.md"):
        return p
    return None
