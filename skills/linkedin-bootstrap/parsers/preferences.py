# <!-- CDD-CONTRACT: ~/gbrain/skills/linkedin-bootstrap/parsers/preferences.py -->
# Contract:   parse 5 preference / inference CSVs → PreferenceBundle.
# Inputs:     source_dir Path.
# Outputs:    PreferenceBundle dataclass with topical_interests[],
#             ad_categories[], inferences[], search_query_topics[].
# Edge:       Missing CSVs → empty corresponding fields; not an error.
# Edge:       Per browser-history-bootstrap precedent: aggregated /
#             categorized data ONLY; no raw query text exits this module.
# Idempotent: pure (no side effects).

"""Phase 7 parser: preference / inference CSVs.

Reads:
  SearchQueries.csv, Ad_Targeting.csv, Inferences_about_you.csv,
  Ads Clicked.csv, LAN Ads Engagement.csv

Output is destined for ~/.hermes/USER.md sub-sections (NOT brain).
Per browser-history-bootstrap precedent: this parser aggregates and
strips raw query text where possible; categorical signals only.
"""

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from ._csv_utils import iter_rows


@dataclass
class PreferenceBundle:
    # Top-N search query topics (raw query text — emitted for USER.md
    # at T1 storage; never to brain).
    search_query_topics: List[str] = field(default_factory=list)
    # Distinct ad-targeting categories (from Ad_Targeting.csv).
    ad_categories: List[str] = field(default_factory=list)
    # LinkedIn's inferences about you (from Inferences_about_you.csv).
    inferences: List[str] = field(default_factory=list)
    # Distinct vendors / company names from Ads Clicked.
    ad_clicked_vendors: List[str] = field(default_factory=list)


def parse(source_dir: Path) -> PreferenceBundle:
    bundle = PreferenceBundle()

    # SearchQueries.csv — Time, Search Query
    qcount = Counter()
    for row in iter_rows(source_dir / "SearchQueries.csv",
                         ["Time", "Search Query", "time", "search query"]):
        q = _clean(row.get("Search Query"))
        if q:
            qcount[q] += 1
    bundle.search_query_topics = [
        q for q, _ in qcount.most_common(50)
    ]

    # Ad_Targeting.csv — Category, Value (LinkedIn ships as one row per
    # targeting fact)
    seen_cats = []
    seen_cat_set = set()
    for row in iter_rows(source_dir / "Ad_Targeting.csv",
                         ["Category", "Value", "category", "value"]):
        cat = _clean(row.get("Category"))
        val = _clean(row.get("Value"))
        if cat and val:
            label = f"{cat}: {val}"
            if label not in seen_cat_set:
                seen_cats.append(label)
                seen_cat_set.add(label)
    bundle.ad_categories = seen_cats[:50]

    # Inferences_about_you.csv — Category, Type
    seen_inf = []
    seen_inf_set = set()
    for row in iter_rows(source_dir / "Inferences_about_you.csv",
                         ["Category", "Type", "category", "type"]):
        cat = _clean(row.get("Category"))
        t = _clean(row.get("Type"))
        label = "; ".join(p for p in (cat, t) if p)
        if label and label not in seen_inf_set:
            seen_inf.append(label)
            seen_inf_set.add(label)
    bundle.inferences = seen_inf[:50]

    # Ads Clicked.csv — Ad Title, Company, Click Date
    vcount = Counter()
    for row in iter_rows(source_dir / "Ads Clicked.csv",
                         ["Ad Title", "Company", "Click Date", "Date"]):
        c = _clean(row.get("Company"))
        if c:
            vcount[c] += 1
    bundle.ad_clicked_vendors = [
        v for v, _ in vcount.most_common(30)
    ]

    return bundle


def _clean(v):
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None
