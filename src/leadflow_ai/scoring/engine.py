"""Pure, deterministic lead scoring.

A score is the source points plus the points of every matched signal. It depends
only on the lead's fields and the rules: no clock, randomness or I/O. Names,
phone country codes, notes and timestamps are never used. Scores are
configurable prioritization heuristics, not predictions of conversion.
"""

from dataclasses import dataclass
from typing import Literal, Protocol

from leadflow_ai.scoring.rules import ScoringRules

Tier = Literal["hot", "warm", "cold"]


class ScorableLead(Protocol):
    """The only lead fields scoring may read (satisfied by LeadRecord and M1 Lead)."""

    @property
    def email(self) -> str | None: ...
    @property
    def phone(self) -> str | None: ...
    @property
    def company(self) -> str | None: ...
    @property
    def source(self) -> str: ...


@dataclass(frozen=True, slots=True)
class Factor:
    """One rule's contribution. ``rule`` is a fixed rule name, never a lead value."""

    rule: str
    matched: bool
    points: int


@dataclass(frozen=True, slots=True)
class ScoreResult:
    score: int
    max_score: int
    tier: Tier
    rules_version: str
    factors: tuple[Factor, ...]  # source factor first, then signals in rules order


def score_lead(lead: ScorableLead, rules: ScoringRules) -> ScoreResult:
    """Score ``lead`` with ``rules``. Raises ValueError for a source the rules lack."""
    source = str(lead.source)
    source_points = dict(rules.source_points)
    if source not in source_points:
        raise ValueError("Lead source is not covered by the scoring rules")

    matched = {
        "has_email": bool(lead.email),
        "has_phone": bool(lead.phone),
        "has_company": bool(lead.company),
        "non_free_email_domain": _non_free_domain(lead.email, rules.free_email_domains),
    }
    factors = [Factor(f"source.{source}", True, source_points[source])]
    factors += [
        Factor(name, matched[name], points if matched[name] else 0)
        for name, points in rules.signal_points
    ]
    score = sum(factor.points for factor in factors)
    return ScoreResult(
        score=score,
        max_score=rules.max_score,
        tier=_tier(score, rules),
        rules_version=rules.rules_version,
        factors=tuple(factors),
    )


def _non_free_domain(email: str | None, free_domains: frozenset[str]) -> bool:
    # Stored emails are already lowercased by M1; only the domain part is inspected.
    if not email or "@" not in email:
        return False
    return email.rsplit("@", 1)[1] not in free_domains


def _tier(score: int, rules: ScoringRules) -> Tier:
    if score >= rules.hot_min:
        return "hot"
    if score >= rules.warm_min:
        return "warm"
    return "cold"
