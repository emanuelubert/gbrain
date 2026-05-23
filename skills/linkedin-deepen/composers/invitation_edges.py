# <!-- CDD-CONTRACT: ~/gbrain/skills/linkedin-deepen/composers/invitation_edges.py -->
# Contract:   Phase 4a — direction-aware bidirectional invitation timeline
#             entries from Invitations.csv rows that carry a profile URL.
# Inputs:     source_dir, brain_root, operator_page, registry, counters.
# Outputs:    Timeline entries on counterparty (URL-matched) + operator
#             page.
# Invariants: bidirectional; counterparty page missing → log + skip; no
#             new pages.
# Idempotent: yes.

"""Phase 4a composer: invitation edges."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Optional

from _shared.csv_utils import iter_rows, clean, stable_row_hash
from _shared.dedup_proxy import find_existing_person_by_linkedin_url
from _shared.page_io import append_timeline_entry


def run(
    source_dir: Path,
    brain_root: Path,
    operator_page: Path,
    *,
    today_iso: str,
    dry_run: bool,
    registry=None,
    counters: Optional[Counter] = None,
    warnings: Optional[Counter] = None,
    error_classes: Optional[Counter] = None,
) -> None:
    counters = counters if counters is not None else Counter()
    warnings = warnings if warnings is not None else Counter()
    error_classes = error_classes if error_classes is not None else Counter()

    path = source_dir / "Invitations.csv"
    if not path.exists():
        counters["phase4a_csv_missing"] += 1
        return

    for row in iter_rows(
        path,
        ["From", "To", "Sent At", "Direction"],
    ):
        direction = (clean(row.get("Direction")) or "").lower()
        sent_at = clean(row.get("Sent At")) or ""
        message = clean(row.get("Message")) or ""
        inviter_url = clean(row.get("inviterProfileUrl"))
        invitee_url = clean(row.get("inviteeProfileUrl"))
        from_name = clean(row.get("From")) or ""
        to_name = clean(row.get("To")) or ""

        counters["phase4a_rows_seen"] += 1

        is_sent = "sent" in direction or "outgoing" in direction
        kind = "sent" if is_sent else "received"
        counterparty_url = invitee_url if is_sent else inviter_url
        counterparty_name = to_name if is_sent else from_name

        if not counterparty_url:
            counters[f"phase4a_invitation_{kind}_no_url"] += 1
            continue

        target = find_existing_person_by_linkedin_url(
            counterparty_url, brain_root,
        )
        if target is None:
            counters[f"phase4a_invitation_{kind}_no_target_page"] += 1
            continue

        row_hash = stable_row_hash(
            [str(target), kind, sent_at, counterparty_url, message]
        )
        edge_key = f"invitation-{kind}-{row_hash}"
        if registry is not None and registry.was_applied(edge_key):
            counters[f"phase4a_invitation_{kind}_idempotent_skip"] += 1
            continue

        date_label = sent_at or today_iso
        msg_suffix = ""
        if message:
            msg_short = message[:140] + ("…" if len(message) > 140 else "")
            msg_suffix = f' | message: "{msg_short}"'

        if is_sent:
            cp_line = (
                f"- **{date_label}** | Invitation received: "
                f"operator invited via LinkedIn{msg_suffix}"
            )
            op_line = (
                f"- **{date_label}** | Invitation sent: "
                f"to [[people/{_slug_from_path(target)}]] (via LinkedIn)"
                f"{msg_suffix}"
            )
        else:
            cp_line = (
                f"- **{date_label}** | Invitation sent: "
                f"invited operator via LinkedIn{msg_suffix}"
            )
            op_line = (
                f"- **{date_label}** | Invitation received: "
                f"from [[people/{_slug_from_path(target)}]] (via LinkedIn)"
                f"{msg_suffix}"
            )

        if dry_run:
            counters[f"phase4a_invitation_{kind}_dry_run_pending"] += 1
            continue

        try:
            wrote_cp = append_timeline_entry(target, cp_line)
            wrote_op = append_timeline_entry(operator_page, op_line)
            if wrote_cp:
                counters[f"phase4a_invitation_{kind}_cp_written"] += 1
            if wrote_op:
                counters[f"phase4a_invitation_{kind}_op_written"] += 1
            if wrote_cp or wrote_op:
                if registry is not None:
                    registry.mark_applied(edge_key)
                counters[f"phase4a_invitation_{kind}_edges_written"] += 1
        except Exception:
            error_classes[f"phase4a_invitation_{kind}_write_failed"] += 1


def _slug_from_path(p: Path) -> str:
    return p.stem
