# <!-- CDD-CONTRACT: ~/gbrain/skills/linkedin-bootstrap/parsers/operator_identity.py -->
# Contract:   parse the 10 operator-identity CSVs into a normalized
#             OperatorIdentity dataclass.
# Inputs:     source_dir Path to extracted LinkedIn export.
# Outputs:    OperatorIdentity with .profile, .positions[], .education[],
#             .skills[], .languages[], .emails[], .phones[], .registration,
#             .verifications[].
# Edge:       Any CSV missing → field is empty/None; not a failure.
# Edge:       Profile.csv preamble (LinkedIn ships none on Profile, but
#             scan-for-header pattern handles uniformly).
# Idempotent: pure (no side effects); caller manages checksum-skip.

"""Phase 1 parser: operator identity CSVs.

Reads:
  Profile.csv, Profile Summary.csv, Positions.csv, Education.csv,
  Languages.csv, Skills.csv, Email Addresses.csv, PhoneNumbers.csv,
  Registration.csv, Verifications/Verifications.csv.

Returns:
  OperatorIdentity dataclass — normalized record of the operator's
  self-data as exported by LinkedIn.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from ._csv_utils import iter_rows


@dataclass
class Position:
    company: str
    title: str
    started_on: Optional[str]
    finished_on: Optional[str]
    location: Optional[str]
    description: Optional[str]


@dataclass
class Education:
    school: str
    degree: Optional[str]
    field_of_study: Optional[str]
    started_on: Optional[str]
    finished_on: Optional[str]


@dataclass
class OperatorIdentity:
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    headline: Optional[str] = None
    summary: Optional[str] = None
    industry: Optional[str] = None
    location: Optional[str] = None
    member_url: Optional[str] = None
    join_date: Optional[str] = None  # from Registration.csv
    positions: List[Position] = field(default_factory=list)
    education: List[Education] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    languages: List[str] = field(default_factory=list)
    emails: List[str] = field(default_factory=list)
    phones: List[str] = field(default_factory=list)
    verifications: List[str] = field(default_factory=list)


def parse(source_dir: Path) -> OperatorIdentity:
    """Parse all operator-identity CSVs in source_dir; return normalized
    OperatorIdentity. Missing CSVs result in empty fields."""
    ident = OperatorIdentity()

    # Profile.csv — columns: First Name, Last Name, Maiden Name, Address,
    # Birth Date, Headline, Summary, Industry, Zip Code, Geo Location,
    # Twitter Handles, Websites, Instant Messengers
    for row in iter_rows(source_dir / "Profile.csv",
                         ["First Name", "first name"]):
        ident.first_name = _clean(row.get("First Name"))
        ident.last_name = _clean(row.get("Last Name"))
        ident.headline = _clean(row.get("Headline"))
        ident.summary = _clean(row.get("Summary"))
        ident.industry = _clean(row.get("Industry"))
        ident.location = _clean(row.get("Geo Location")) or _clean(row.get("Address"))
        break  # Profile.csv has one row by definition

    # Profile Summary.csv — usually just `Summary` column with one row.
    if not ident.summary:
        for row in iter_rows(source_dir / "Profile Summary.csv",
                             ["Summary", "summary"]):
            ident.summary = _clean(row.get("Summary"))
            break

    # Positions.csv — columns: Company Name, Title, Description,
    # Location, Started On, Finished On
    for row in iter_rows(source_dir / "Positions.csv",
                         ["Company Name", "company name"]):
        company = _clean(row.get("Company Name"))
        if not company:
            continue
        ident.positions.append(Position(
            company=company,
            title=_clean(row.get("Title")) or "",
            started_on=_clean(row.get("Started On")),
            finished_on=_clean(row.get("Finished On")),
            location=_clean(row.get("Location")),
            description=_clean(row.get("Description")),
        ))

    # Education.csv — columns: School Name, Start Date, End Date,
    # Notes, Degree Name, Activities
    for row in iter_rows(source_dir / "Education.csv",
                         ["School Name", "school name"]):
        school = _clean(row.get("School Name"))
        if not school:
            continue
        ident.education.append(Education(
            school=school,
            degree=_clean(row.get("Degree Name")),
            field_of_study=_clean(row.get("Notes")),
            started_on=_clean(row.get("Start Date")),
            finished_on=_clean(row.get("End Date")),
        ))

    # Skills.csv — column: Name
    for row in iter_rows(source_dir / "Skills.csv", ["Name", "name"]):
        s = _clean(row.get("Name"))
        if s and s not in ident.skills:
            ident.skills.append(s)

    # Languages.csv — columns: Name, Proficiency
    for row in iter_rows(source_dir / "Languages.csv",
                         ["Name", "name"]):
        s = _clean(row.get("Name"))
        if s and s not in ident.languages:
            ident.languages.append(s)

    # Email Addresses.csv — columns: Email Address, Confirmed, Primary,
    # Updated On
    for row in iter_rows(source_dir / "Email Addresses.csv",
                         ["Email Address", "email address"]):
        e = _clean(row.get("Email Address"))
        if e and e not in ident.emails:
            ident.emails.append(e)

    # PhoneNumbers.csv — columns: Extension, Number, Type
    for row in iter_rows(source_dir / "PhoneNumbers.csv",
                         ["Number", "number", "Extension", "extension"]):
        n = _clean(row.get("Number"))
        if n and n not in ident.phones:
            ident.phones.append(n)

    # Registration.csv — columns: Registered At, Registration IP,
    # Subscription
    for row in iter_rows(source_dir / "Registration.csv",
                         ["Registered At", "registered at"]):
        ident.join_date = _clean(row.get("Registered At"))
        break

    # Verifications/Verifications.csv — columns: Verification Type,
    # Verified At, Status
    vpath = source_dir / "Verifications" / "Verifications.csv"
    for row in iter_rows(vpath, ["Verification Type", "verification type"]):
        v = _clean(row.get("Verification Type"))
        if v:
            status = _clean(row.get("Status"))
            ident.verifications.append(f"{v}: {status or 'unknown'}")

    return ident


def _clean(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None
