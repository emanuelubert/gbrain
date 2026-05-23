# <!-- CDD-CONTRACT: ~/gbrain/skills/linkedin-deepen/_shared/csv_utils.py -->
# Contract:   shared CSV-reader utilities for LinkedIn export parsers.
# Inputs:     Path to CSV; expected first-column header signal names.
# Outputs:    iter[dict] of {column: cell} rows AFTER any preamble strip.
# Edge:       LinkedIn ships some CSVs with a multi-line "Notes:" preamble
#             before the real header row. Detect real header by scanning
#             for a row whose first cell matches any signal name.
# Edge:       Empty / missing CSV → empty iterator; not an error.
# Idempotent: pure (no side effects).

"""Shared CSV-reader utilities for linkedin-deepen.

Mirrors linkedin-bootstrap's parsers/_csv_utils.py but lives in the
linkedin-deepen package so the skill is self-contained.
"""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path
from typing import Iterator, List


def iter_rows(path: Path, header_signals: List[str]) -> Iterator[dict]:
    """Yield dict rows from a LinkedIn CSV, after stripping any
    multi-line preamble.

    Args:
        path: Path to CSV.
        header_signals: strings; real header row is first row whose
            first cell matches any of these case-insensitively.

    Yields:
        Each subsequent row as a dict keyed by the header columns.
    """
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            header = None
            signals_lc = [s.strip().lower() for s in header_signals]
            for row in reader:
                if not row:
                    continue
                first_lc = (row[0] or "").strip().lower()
                if first_lc in signals_lc:
                    header = row
                    break
            if header is None:
                return
            n_cols = len(header)
            for row in reader:
                if not row:
                    continue
                if len(row) < n_cols:
                    row = row + [""] * (n_cols - len(row))
                elif len(row) > n_cols:
                    row = row[:n_cols]
                yield dict(zip(header, row))
    except (OSError, UnicodeDecodeError):
        return


def stable_csv_checksum(path: Path) -> str:
    """SHA-256 (16 hex chars) of file bytes for per-CSV idempotency.
    Missing file → empty string."""
    if not path.exists():
        return ""
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()[:16]


def stable_row_hash(parts) -> str:
    """SHA-256 (16 hex chars) of joined parts; row-level idempotency key."""
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p or "").encode("utf-8"))
        h.update(b"\x1f")  # ASCII unit separator
    return h.hexdigest()[:16]


def clean(v):
    """Strip + None if empty. Returns Optional[str]."""
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None
