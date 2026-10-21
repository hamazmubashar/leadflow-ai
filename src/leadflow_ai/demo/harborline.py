"""Harborline Home & Living: the fictional company behind the public demo (M12).

Harborline sells home goods (bedding, tableware, lighting, decor) to trade buyers:
boutiques, hotels, cafes, interior designers and property developers. Every name,
company, email (``example.com``/``.org``/``.net``) and phone number (UK drama range
``+44 7700 900xxx``) is invented.
"""

from importlib import resources

from leadflow_ai.drafts import DraftTemplate
from leadflow_ai.lifecycle import LeadStatus

S = LeadStatus

# 24 fictional trade buyers, packaged with the code (see ``leads_csv``).
LEADS_RESOURCE = "harborline-leads.csv"


def leads_csv() -> bytes:
    """The demo leads as a UTF-8 CSV file, in the same format as a user's import."""
    return resources.files(__package__).joinpath(LEADS_RESOURCE).read_bytes()


# Status history: (target status, note) steps applied in order from "new".
STATUS_PATHS: dict[str, list[tuple[LeadStatus, str]]] = {
    "Marcus Reid": [(S.CONTACTED, "Sent the trade price list"), (S.QUALIFIED, "Budget agreed")],
    "Priya Kapoor": [(S.CONTACTED, "Follow-up call booked")],
    "Amelia Clarke": [
        (S.CONTACTED, "Sent stoneware samples"),
        (S.QUALIFIED, "Approved samples"),
        (S.WON, "First order placed"),
    ],
    "Grace Whitfield": [(S.CONTACTED, "Catalogue sent")],
    "Henry Walsh": [(S.CONTACTED, "Quoted linen sets"), (S.QUALIFIED, "Comparing quotes")],
    "Leo Fraser": [
        (S.CONTACTED, "Visited one restaurant"),
        (S.QUALIFIED, "Tasting-menu glassware chosen"),
        (S.WON, "Glassware order signed"),
    ],
    "Arthur Price": [(S.CONTACTED, "Checked in"), (S.LOST, "Staying with current supplier")],
    "Samuel King": [(S.LOST, "Needs a framework supplier")],
    "William Cole": [(S.CONTACTED, "Shared lighting lookbook")],
}

# Drafts waiting for review (created after the status history).
DRAFTS: dict[str, DraftTemplate] = {
    "Olivia Bennett": DraftTemplate.INTRODUCTION,
    "Tom Hargreaves": DraftTemplate.INTRODUCTION,
    "Mia Thompson": DraftTemplate.INTRODUCTION,
    "Priya Kapoor": DraftTemplate.FOLLOW_UP,
    "Grace Whitfield": DraftTemplate.FOLLOW_UP,
}

# Drafts that were already approved (each records a simulated outbox item).
APPROVED: dict[str, DraftTemplate] = {
    "Marcus Reid": DraftTemplate.FOLLOW_UP,
    "Henry Walsh": DraftTemplate.FOLLOW_UP,
}
