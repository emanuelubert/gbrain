# <!-- CDD-CONTRACT: ~/gbrain/skills/linkedin-bootstrap/parsers/learning.py -->
# Contract:   parse Learning.csv → Course records.
# Inputs:     Path to Learning.csv.
# Outputs:    iter[Course] with .name, .provider, .topic, .completed_on,
#             .duration_minutes.
# Edge:       Missing → empty iterator.
# Edge:       Duration parsing: LinkedIn ships duration as either a
#             total-minutes integer OR a "HH:MM:SS" string; parser
#             accepts both and emits int minutes (None on parse failure).
# Idempotent: pure (no side effects).

"""Phase 6 parser: Learning.csv → Course records."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from ._csv_utils import iter_rows


@dataclass
class Course:
    name: str
    provider: str   # always "LinkedIn Learning" for this CSV
    topic: Optional[str]
    completed_on: Optional[str]
    duration_minutes: Optional[int]


def parse(path: Path) -> Iterator[Course]:
    """Yield Course records from Learning.csv."""
    for row in iter_rows(path, ["Content Title", "Title",
                                "Course Title", "Last Engagement Date"]):
        # Tolerate column-name drift across LinkedIn export versions.
        name = _clean(row.get("Content Title") or row.get("Title")
                      or row.get("Course Title"))
        if not name:
            continue
        completed = _clean(row.get("Completed Date")
                           or row.get("Last Engagement Date")
                           or row.get("Completion Date"))
        provider = _clean(row.get("Content Provider")) or "LinkedIn Learning"
        topic = _clean(row.get("Topic") or row.get("Category"))
        dur_raw = row.get("Content Duration") or row.get("Duration")
        duration = _parse_duration_minutes(dur_raw)
        yield Course(
            name=name,
            provider=provider,
            topic=topic,
            completed_on=completed,
            duration_minutes=duration,
        )


def _clean(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _parse_duration_minutes(raw) -> Optional[int]:
    """Parse duration → integer minutes. Accepts:
      - int / str int: treated as total minutes
      - 'HH:MM:SS' / 'MM:SS' string: parsed to minutes
      - None / empty → None
    """
    if raw is None or raw == "":
        return None
    s = str(raw).strip()
    if not s:
        return None
    # All digits → minutes
    if s.isdigit():
        try:
            return int(s)
        except ValueError:
            return None
    # HH:MM:SS or MM:SS
    m = re.match(r"^(?:(\d+):)?(\d+):(\d+)$", s)
    if m:
        try:
            h = int(m.group(1) or 0)
            mm = int(m.group(2))
            ss = int(m.group(3))
            return h * 60 + mm + (1 if ss >= 30 else 0)
        except ValueError:
            return None
    return None
