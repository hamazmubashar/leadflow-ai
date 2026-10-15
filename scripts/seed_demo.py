"""Give the fictional demo leads some history, through the API (demo use only).

Run after importing ``demo/demo-leads.csv`` on the CSV Import page, with the API running:

    uv run --group ui python scripts/seed_demo.py

It moves some demo leads through statuses and creates drafts that stay
**pending review**. It never approves or rejects anything: those decisions are left to
a person on the Drafts page. Safe to run twice: work already done is skipped.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard.api_client import ApiClient, ApiError
from dashboard.config import ConfigError, load_settings
from dashboard.models import DraftTemplate, LeadStatus

S = LeadStatus
# Each path is applied in order, starting from the lead's current status.
STATUS_PATHS: dict[str, list[tuple[LeadStatus, str]]] = {
    "Ayesha Rahman": [
        (S.CONTACTED, "Intro call booked"),
        (S.QUALIFIED, "Budget confirmed"),
        (S.WON, "Signed annual plan"),
    ],
    "Liam Gallagher": [
        (S.CONTACTED, "Replied to referral email"),
        (S.QUALIFIED, "Needs 3 seats"),
        (S.WON, "Closed after demo"),
    ],
    "Nadia Petrova": [(S.CONTACTED, "Discovery call"), (S.QUALIFIED, "Decision in two weeks")],
    "Sofia Marchetti": [(S.CONTACTED, "Followed up after expo"), (S.QUALIFIED, "Pilot agreed")],
    "Daniel Okafor": [(S.CONTACTED, "Demo scheduled")],
    "Fatima Malik": [(S.CONTACTED, "Sent overview deck")],
    "Zara Sheikh": [(S.CONTACTED, "Left a message")],
    "Hiroshi Tanaka": [(S.CONTACTED, "Pricing sent"), (S.LOST, "Chose another vendor")],
    "Carlos Mendes": [(S.LOST, "No budget this year")],
    "Bilal Qureshi": [(S.LOST, "Not a fit")],
}
DRAFTS: dict[str, DraftTemplate] = {
    "Daniel Okafor": DraftTemplate.INTRODUCTION,
    "Fatima Malik": DraftTemplate.INTRODUCTION,
    "Chloe Martin": DraftTemplate.INTRODUCTION,
    "Arjun Mehta": DraftTemplate.INTRODUCTION,
    "Sofia Marchetti": DraftTemplate.FOLLOW_UP,
    "Zara Sheikh": DraftTemplate.FOLLOW_UP,
}


def main() -> int:
    try:
        client = ApiClient(load_settings())
    except ConfigError as exc:
        print("Configuration problem:", "; ".join(exc.problems))
        return 1
    try:
        leads = {lead.full_name: lead.id for lead in client.list_leads(limit=100).items}
    except ApiError as error:
        print("Cannot read leads:", error.message)
        return 1
    missing = sorted((set(STATUS_PATHS) | set(DRAFTS)) - set(leads))
    if missing:
        print("Import demo/demo-leads.csv first. Missing:", ", ".join(missing))
        return 1

    for name, path in STATUS_PATHS.items():
        current = client.get_activity(leads[name], limit=1).status
        targets = [step[0] for step in path]
        start = targets.index(current) + 1 if current in targets else 0
        for status, note in path[start:]:
            try:
                client.change_status(leads[name], status=status, expected_status=current, note=note)
            except ApiError as error:
                print(f"skip   {name}: {error.code or error.message}")
                break
            print(f"status {name}: {current.value} -> {status.value}")
            current = status

    for name, template in DRAFTS.items():
        try:
            client.create_draft(leads[name], template)
        except ApiError as error:
            print(f"skip   {name}: {error.code or error.message}")
            continue
        print(f"draft  {name}: {template.value} (pending review)")

    print("Done. Review the pending drafts on the Drafts page: approve or reject them yourself.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
