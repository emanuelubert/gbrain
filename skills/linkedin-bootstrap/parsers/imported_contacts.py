# <!-- CDD-CONTRACT: ~/gbrain/skills/linkedin-bootstrap/parsers/imported_contacts.py -->
# Contract:   parse ImportedContacts.csv → ImportedContact records.
# Inputs:     Path to ImportedContacts.csv.
# Outputs:    iter[ImportedContact] with .first_name, .last_name, .email,
#             .phone, .company, .role.
# Edge:       Multi-line preamble stripped via _csv_utils.
# Edge:       Phone normalization deferred to caller (uses
#             dedup.normalize_phone for E.164-ish).
# Idempotent: pure (no side effects).

"""Phase 3 parser: ImportedContacts.csv → ImportedContact records.

These are phone-uploaded contacts LinkedIn captured at some point.
HIGH overlap with Apple Contacts. Per operator scope decision
S191-Q2: alias-merge ONLY against existing brain pages; NEVER create
new pages from this CSV.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from ._csv_utils import iter_rows


@dataclass
class ImportedContact:
    first_name: Optional[str]
    last_name: Optional[str]
    email: Optional[str]
    phone: Optional[str]
    company: Optional[str]
    role: Optional[str]


def parse(path: Path) -> Iterator[ImportedContact]:
    """Yield ImportedContact records. Empty / missing → empty iterator."""
    for row in iter_rows(path, ["First Name", "first name",
                                "FirstName", "Source"]):
        yield ImportedContact(
            first_name=_clean(row.get("First Name") or row.get("FirstName")),
            last_name=_clean(row.get("Last Name") or row.get("LastName")),
            email=_clean(row.get("Email") or row.get("Emails")
                         or row.get("Email Address")),
            phone=_clean(row.get("Phone Numbers") or row.get("Phone")
                         or row.get("PhoneNumber")),
            company=_clean(row.get("Company") or row.get("CompanyName")),
            role=_clean(row.get("Title") or row.get("Position")),
        )


def _clean(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None
