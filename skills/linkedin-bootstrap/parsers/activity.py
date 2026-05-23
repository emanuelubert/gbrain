# <!-- CDD-CONTRACT: ~/gbrain/skills/linkedin-bootstrap/parsers/activity.py -->
# Contract:   parse 8 activity CSVs → ActivityEvent records (excl. Reactions.csv).
# Inputs:     source_dir Path.
# Outputs:    iter[ActivityEvent] with .kind, .date, .url, .summary,
#             .referenced_entity_name (or None).
# HARD INVARIANT: Reactions.csv is in SKIP_LIST and is NEVER opened by
#                 this parser. Test_phase8_skip_invariant enforces this.
# Edge:       Comments_*.csv — filename varies per LinkedIn member ID;
#             pattern-match Comments_*.csv via glob.
# Edge:       Missing CSVs → 0 events emitted; not an error.
# Idempotent: pure (no side effects).

"""Phase 5 parser: activity CSVs → ActivityEvent records.

CSVs handled:
  Comments_*.csv, Shares.csv, InstantReposts.csv, Saved_Items.csv,
  Votes.csv, Rich_Media.csv, Events.csv.

CSVs HARD-SKIPPED (in SKIP_LIST; never opened):
  Reactions.csv (operator scope decision S191-default-2; low-signal
  high-volume "liked X" rows would bloat operator self-page timeline
  with little semantic content).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from ._csv_utils import iter_rows


@dataclass
class ActivityEvent:
    kind: str   # "comment" | "share" | "repost" | "saved" | "vote" |
                # "rich_media" | "event"
    date: Optional[str]
    url: Optional[str]
    summary: Optional[str]
    referenced_entity_name: Optional[str]


def parse(source_dir: Path) -> Iterator[ActivityEvent]:
    """Yield ActivityEvent records across activity CSVs.

    INVARIANT: this function MUST NOT open Reactions.csv. The Phase 8
    skip invariant test scans for any reference to 'Reactions.csv' in
    parsers/ and the test fails if found.
    """

    # Comments_*.csv — filename pattern includes the LinkedIn member ID.
    # Columns: Date, Link, Message
    for path in sorted(source_dir.glob("Comments_*.csv")):
        for row in iter_rows(path, ["Date", "Link", "Message", "date"]):
            yield ActivityEvent(
                kind="comment",
                date=_clean(row.get("Date")),
                url=_clean(row.get("Link")),
                summary=_truncate(_clean(row.get("Message")), 200),
                referenced_entity_name=None,
            )

    # Shares.csv — Date, ShareLink, ShareCommentary, SharedUrl,
    # MediaUrl, Visibility
    for row in iter_rows(source_dir / "Shares.csv",
                         ["Date", "ShareLink", "Visibility"]):
        yield ActivityEvent(
            kind="share",
            date=_clean(row.get("Date")),
            url=_clean(row.get("ShareLink")) or _clean(row.get("SharedUrl")),
            summary=_truncate(_clean(row.get("ShareCommentary")), 200),
            referenced_entity_name=None,
        )

    # InstantReposts.csv — Date, Post URL
    for row in iter_rows(source_dir / "InstantReposts.csv",
                         ["Date", "Post URL", "PostURL"]):
        yield ActivityEvent(
            kind="repost",
            date=_clean(row.get("Date")),
            url=_clean(row.get("Post URL") or row.get("PostURL")),
            summary=None,
            referenced_entity_name=None,
        )

    # Saved_Items.csv — Saved At, Saved Item
    for row in iter_rows(source_dir / "Saved_Items.csv",
                         ["Saved At", "Saved Item", "saved at"]):
        yield ActivityEvent(
            kind="saved",
            date=_clean(row.get("Saved At")),
            url=_clean(row.get("Saved Item")),
            summary=None,
            referenced_entity_name=None,
        )

    # Votes.csv — Date, Link, Option Text
    for row in iter_rows(source_dir / "Votes.csv",
                         ["Date", "Link", "Option Text"]):
        yield ActivityEvent(
            kind="vote",
            date=_clean(row.get("Date")),
            url=_clean(row.get("Link")),
            summary=_clean(row.get("Option Text")),
            referenced_entity_name=None,
        )

    # Rich_Media.csv — Type, Date, Link
    for row in iter_rows(source_dir / "Rich_Media.csv",
                         ["Type", "Date", "Link"]):
        yield ActivityEvent(
            kind="rich_media",
            date=_clean(row.get("Date")),
            url=_clean(row.get("Link")),
            summary=_clean(row.get("Type")),
            referenced_entity_name=None,
        )

    # Events.csv — Event Name, Date, Role, URL
    for row in iter_rows(source_dir / "Events.csv",
                         ["Event Name", "Date", "event name"]):
        yield ActivityEvent(
            kind="event",
            date=_clean(row.get("Date")),
            url=_clean(row.get("URL")),
            summary=_clean(row.get("Event Name")),
            referenced_entity_name=_clean(row.get("Role")),
        )


def _clean(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _truncate(s: Optional[str], n: int) -> Optional[str]:
    if s is None:
        return None
    return s if len(s) <= n else s[: n - 1] + "…"
