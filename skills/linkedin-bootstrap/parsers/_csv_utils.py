# <!-- CDD-CONTRACT: ~/gbrain/skills/linkedin-bootstrap/parsers/_csv_utils.py -->
# Contract:   shared CSV-reader utilities for LinkedIn export parsers.
# Inputs:     Path to CSV; expected first-column header names.
# Outputs:    iter[dict] of {column_name: cell_value} rows AFTER preamble strip.
# Edge:       LinkedIn ships some CSVs with a "Notes:" multi-line preamble
#             before the real header row. Detect real header by scanning
#             for a line whose first cell matches any expected name.
# Edge:       Empty CSV → empty iterator; not an error.
# Idempotent: pure (no side effects).

"""Shared CSV-reader utilities for LinkedIn export parsers.

LinkedIn ships some CSV files with a multi-line "Notes:" preamble
explaining e.g. "When exporting your connection data, you may notice
some emails are missing..." BEFORE the real header row. Standard
csv.DictReader assumes row 0 is the header, which leads to garbage
column names on these files.

This module's `iter_rows(path, header_signals)` scans for the real
header line by matching any row whose first cell matches one of the
provided `header_signals` (e.g., for Connections.csv:
`['First Name', 'first name']`). Once found, hands off to
csv.DictReader for the rest of the file.

Per D27: this module is pure. No log emissions of CSV body content.
"""

import csv
from pathlib import Path
from typing import Iterator


def iter_rows(path: Path, header_signals: list) -> Iterator[dict]:
    """Yield dict rows from a LinkedIn CSV, after stripping any
    multi-line preamble.

    Args:
        path: Path to CSV.
        header_signals: list of strings; the real header row is the
            first row whose first cell (after .strip()) matches any
            of these case-insensitively.

    Yields:
        Each subsequent row as a dict keyed by the header columns.

    Returns:
        Empty iterator if file is empty, header is not found, or the
        file is missing.
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
                # Pad short rows; truncate long ones to header length.
                if len(row) < n_cols:
                    row = row + [""] * (n_cols - len(row))
                elif len(row) > n_cols:
                    row = row[:n_cols]
                yield dict(zip(header, row))
    except (OSError, UnicodeDecodeError):
        return


def stable_csv_checksum(path: Path) -> str:
    """SHA-256 (16 hex chars) of file bytes, for per-CSV idempotency.
    Missing file → empty string."""
    import hashlib
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
