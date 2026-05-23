# <!-- CDD-CONTRACT: ~/gbrain/skills/linkedin-bootstrap/parsers/network_signals.py -->
# Contract:   parse 5 network-signals CSVs → NetworkSignal records.
# Inputs:     source_dir Path.
# Outputs:    iter[NetworkSignal] with .kind, .counterparty_name,
#             .counterparty_url, .counterparty_company, .signal_date,
#             .signal_text (free text from CSV row).
# Edge:       Missing CSVs in source_dir → yielded count of 0 from that CSV;
#             not an error.
# Idempotent: pure (no side effects).

"""Phase 4 parser: Endorsements (given/received), Invitations,
Company Follows, Member Follows → NetworkSignal records."""

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from ._csv_utils import iter_rows


@dataclass
class NetworkSignal:
    kind: str   # "endorsement_given" | "endorsement_received" |
                # "invitation_sent" | "invitation_received" |
                # "company_follow" | "member_follow"
    counterparty_name: Optional[str]
    counterparty_url: Optional[str]
    counterparty_company: Optional[str]
    signal_date: Optional[str]
    signal_text: Optional[str]


def parse(source_dir: Path) -> Iterator[NetworkSignal]:
    """Yield NetworkSignal records across all 5 network-signals CSVs."""

    # Endorsement_Given_Info.csv — Endorsee Public Url, Endorsee First Name,
    # Endorsee Last Name, Skill Name, Endorsement Date
    for row in iter_rows(source_dir / "Endorsement_Given_Info.csv",
                         ["Endorsee Public Url", "Endorsement Date",
                          "Endorsee First Name"]):
        first = _clean(row.get("Endorsee First Name"))
        last = _clean(row.get("Endorsee Last Name"))
        name = _join(first, last)
        yield NetworkSignal(
            kind="endorsement_given",
            counterparty_name=name,
            counterparty_url=_clean(row.get("Endorsee Public Url")),
            counterparty_company=None,
            signal_date=_clean(row.get("Endorsement Date")),
            signal_text=_clean(row.get("Skill Name")),
        )

    # Endorsement_Received_Info.csv — Endorser Public Url, Endorser First
    # Name, Endorser Last Name, Skill Name, Endorsement Date
    for row in iter_rows(source_dir / "Endorsement_Received_Info.csv",
                         ["Endorser Public Url", "Endorsement Date",
                          "Endorser First Name"]):
        first = _clean(row.get("Endorser First Name"))
        last = _clean(row.get("Endorser Last Name"))
        name = _join(first, last)
        yield NetworkSignal(
            kind="endorsement_received",
            counterparty_name=name,
            counterparty_url=_clean(row.get("Endorser Public Url")),
            counterparty_company=None,
            signal_date=_clean(row.get("Endorsement Date")),
            signal_text=_clean(row.get("Skill Name")),
        )

    # Invitations.csv — From, To, Sent At, Message, Direction
    for row in iter_rows(source_dir / "Invitations.csv",
                         ["From", "To", "Sent At", "Direction"]):
        direction = (_clean(row.get("Direction")) or "").lower()
        kind = "invitation_sent" if "sent" in direction or "outgoing" in direction \
            else "invitation_received"
        counterparty = _clean(row.get("To") if "sent" in direction
                              else row.get("From"))
        yield NetworkSignal(
            kind=kind,
            counterparty_name=counterparty,
            counterparty_url=None,
            counterparty_company=None,
            signal_date=_clean(row.get("Sent At")),
            signal_text=_clean(row.get("Message")),
        )

    # Company Follows.csv — Organization, Followed On
    for row in iter_rows(source_dir / "Company Follows.csv",
                         ["Organization", "organization",
                          "Followed On", "followed on"]):
        org = _clean(row.get("Organization"))
        if not org:
            continue
        yield NetworkSignal(
            kind="company_follow",
            counterparty_name=None,
            counterparty_url=None,
            counterparty_company=org,
            signal_date=_clean(row.get("Followed On")),
            signal_text=None,
        )

    # Member_Follows.csv — FullName, Date, Status
    for row in iter_rows(source_dir / "Member_Follows.csv",
                         ["FullName", "Date", "Full Name"]):
        name = _clean(row.get("FullName") or row.get("Full Name"))
        if not name:
            continue
        yield NetworkSignal(
            kind="member_follow",
            counterparty_name=name,
            counterparty_url=None,
            counterparty_company=None,
            signal_date=_clean(row.get("Date")),
            signal_text=_clean(row.get("Status")),
        )


def _clean(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _join(first: Optional[str], last: Optional[str]) -> Optional[str]:
    parts = [p for p in (first, last) if p]
    if not parts:
        return None
    return " ".join(parts)
