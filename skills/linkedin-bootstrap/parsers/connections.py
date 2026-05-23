# <!-- CDD-CONTRACT: ~/gbrain/skills/linkedin-bootstrap/parsers/connections.py -->
# Contract:   parse Connections.csv (LinkedIn export); skip multi-line
#             preamble; yield Connection records.
# Inputs:     Path to Connections.csv.
# Outputs:    iter[Connection] with .first_name, .last_name, .url, .email,
#             .company, .position, .connected_on.
# Edge:       LinkedIn-Member placeholder rows skipped (first="LinkedIn",
#             last="Member"); rows with no email AND no URL AND no name
#             yielded with is_empty=True so caller can route to review-queue.
# Edge:       The "Notes:" preamble block before the real header row is
#             stripped by _csv_utils.iter_rows scanning for "First Name,".
# Idempotent: pure (no side effects); caller manages checksum-skip.

"""Phase 2 parser: Connections.csv → Connection records."""

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from ._csv_utils import iter_rows


@dataclass
class Connection:
    first_name: Optional[str]
    last_name: Optional[str]
    url: Optional[str]            # LinkedIn member URL
    email: Optional[str]
    company: Optional[str]
    position: Optional[str]       # job title at company
    connected_on: Optional[str]   # ISO date string from LinkedIn
    is_linkedin_member_placeholder: bool = False
    is_empty: bool = False


def parse(path: Path) -> Iterator[Connection]:
    """Yield Connection records from Connections.csv. Empty file →
    empty iterator. Missing file → empty iterator."""
    for row in iter_rows(path, ["First Name", "first name",
                                "Email Address", "email address"]):
        first = _clean(row.get("First Name"))
        last = _clean(row.get("Last Name"))
        url = _clean(row.get("URL"))
        email = _clean(row.get("Email Address"))
        company = _clean(row.get("Company"))
        position = _clean(row.get("Position"))
        connected_on = _clean(row.get("Connected On"))

        is_member_placeholder = (first == "LinkedIn" and last == "Member")
        is_empty = not any([first, last, url, email])

        yield Connection(
            first_name=first,
            last_name=last,
            url=url,
            email=email,
            company=company,
            position=position,
            connected_on=connected_on,
            is_linkedin_member_placeholder=is_member_placeholder,
            is_empty=is_empty,
        )


def _clean(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None
