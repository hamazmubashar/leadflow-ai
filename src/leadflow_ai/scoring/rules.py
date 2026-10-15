"""Load and strictly validate versioned TOML scoring rules. Standard library only.

Every problem in a rules file is collected and reported together with its key
path. Messages describe the expected value and never repeat the file's content.
"""

import re
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from leadflow_ai.leads import LeadSource

SCHEMA_VERSION = 1
MAX_SCORE = 100
MAX_RULES_FILE_BYTES = 64 * 1024
MAX_FREE_EMAIL_DOMAINS = 500
DEFAULT_RULES_RESOURCE = "default_rules.toml"

SOURCES: tuple[str, ...] = tuple(source.value for source in LeadSource)
SIGNALS: tuple[str, ...] = ("has_email", "has_phone", "has_company", "non_free_email_domain")
TIERS: tuple[str, ...] = ("hot", "warm", "cold")

_RULES_VERSION = re.compile(r"[A-Za-z0-9._-]{1,64}")
_DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")


@dataclass(frozen=True, slots=True)
class ScoringRules:
    """A validated ruleset. Points are stored in a fixed order for stable output."""

    rules_version: str
    source_points: tuple[tuple[str, int], ...]  # in LeadSource order
    signal_points: tuple[tuple[str, int], ...]  # in SIGNALS order
    hot_min: int
    warm_min: int
    free_email_domains: frozenset[str]

    @property
    def max_score(self) -> int:
        return max(points for _, points in self.source_points) + sum(
            points for _, points in self.signal_points
        )


@dataclass(frozen=True, slots=True)
class RuleProblem:
    path: str
    message: str


class ScoringRulesError(ValueError):
    """The rules cannot be used. Holds every problem found."""

    def __init__(self, problems: list[RuleProblem]) -> None:
        self.problems: tuple[RuleProblem, ...] = tuple(problems)
        details = "; ".join(f"{p.path}: {p.message}" for p in self.problems)
        super().__init__(f"Invalid scoring rules ({len(self.problems)} problems): {details}")


def load_rules(path: Path | None = None) -> ScoringRules:
    """Load ``path``, or the packaged default rules when ``path`` is None."""
    if path is None:
        data = resources.files(__package__).joinpath(DEFAULT_RULES_RESOURCE).read_bytes()
        return parse_rules(data)
    if not path.is_file():
        raise ScoringRulesError([RuleProblem("file", "Rules file not found or not a regular file")])
    if path.stat().st_size > MAX_RULES_FILE_BYTES:
        raise ScoringRulesError([_too_large()])
    return parse_rules(path.read_bytes())


def parse_rules(data: bytes) -> ScoringRules:
    """Validate TOML ``data`` and return the rules, or raise ``ScoringRulesError``."""
    if len(data) > MAX_RULES_FILE_BYTES:
        raise ScoringRulesError([_too_large()])
    try:
        document = tomllib.loads(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise ScoringRulesError([RuleProblem("file", "Rules file must be UTF-8")]) from None
    except tomllib.TOMLDecodeError as exc:
        raise ScoringRulesError([RuleProblem("file", f"Not valid TOML: {exc}")]) from None
    return _Validator().validate(document)


def _too_large() -> RuleProblem:
    return RuleProblem("file", f"Rules file must be at most {MAX_RULES_FILE_BYTES} bytes")


class _Validator:
    def __init__(self) -> None:
        self.problems: list[RuleProblem] = []

    def problem(self, path: str, message: str) -> None:
        self.problems.append(RuleProblem(path, message))

    def validate(self, document: dict[str, Any]) -> ScoringRules:
        self.keys(
            document,
            "",
            ("schema_version", "rules_version", "tiers", "points", "free_email_domains"),
        )

        schema = document.get("schema_version")
        if "schema_version" in document and not (type(schema) is int and schema == SCHEMA_VERSION):
            self.problem("schema_version", f"Must be the integer {SCHEMA_VERSION}")

        version = document.get("rules_version")
        if "rules_version" in document and not (
            isinstance(version, str) and _RULES_VERSION.fullmatch(version)
        ):
            self.problem(
                "rules_version", "Must be 1-64 characters of letters, digits, '.', '_' or '-'"
            )

        points = self.table(document, "points", ("source", "signals"))
        source_points = self.points_table(points, "points.source", SOURCES)
        signal_points = self.points_table(points, "points.signals", SIGNALS)
        hot, warm = self.tiers(document)
        domains = self.domains(document)

        max_score = None
        if source_points is not None and signal_points is not None:
            max_score = max(p for _, p in source_points) + sum(p for _, p in signal_points)
            if max_score > MAX_SCORE:
                self.problem(
                    "points",
                    f"Highest source score plus all signal points must be at most {MAX_SCORE}",
                )
        if max_score is not None and hot is not None and hot > max_score:
            self.problem("tiers.hot", "Must not exceed the maximum possible score")

        # Every None below has already recorded a problem; the checks also narrow types.
        if (
            self.problems
            or source_points is None
            or signal_points is None
            or hot is None
            or warm is None
            or domains is None
        ):
            raise ScoringRulesError(self.problems)
        return ScoringRules(
            rules_version=str(version),
            source_points=source_points,
            signal_points=signal_points,
            hot_min=hot,
            warm_min=warm,
            free_email_domains=domains,
        )

    def keys(self, table: dict[str, Any], path: str, expected: tuple[str, ...]) -> None:
        prefix = f"{path}." if path else ""
        for key in table:
            if key not in expected:
                self.problem(f"{prefix}{key}", "Unknown key")
        for key in expected:
            if key not in table:
                self.problem(f"{prefix}{key}", "Missing required key")

    def table(self, parent: dict[str, Any], key: str, expected: tuple[str, ...]) -> dict[str, Any]:
        value = parent.get(key)
        if key in parent and not isinstance(value, dict):
            self.problem(key, "Must be a table")
            return {}
        if isinstance(value, dict):
            self.keys(value, key, expected)
            return value
        return {}

    def integer(self, value: object, path: str, low: int, high: int) -> int | None:
        # type() check: bool is a subclass of int and must not count as a number here.
        if type(value) is not int:
            self.problem(path, "Must be a whole number (not a boolean, float or string)")
            return None
        if not low <= value <= high:
            self.problem(path, f"Must be between {low} and {high}")
            return None
        return value

    def points_table(
        self, points: dict[str, Any], path: str, names: tuple[str, ...]
    ) -> tuple[tuple[str, int], ...] | None:
        key = path.split(".")[1]
        table = points.get(key)
        if key in points and not isinstance(table, dict):
            self.problem(path, "Must be a table")
            return None
        if not isinstance(table, dict):
            return None
        self.keys(table, path, names)
        values = [
            (name, self.integer(table[name], f"{path}.{name}", 0, MAX_SCORE))
            for name in names
            if name in table
        ]
        if len(values) != len(names) or any(points_ is None for _, points_ in values):
            return None
        return tuple((name, points_) for name, points_ in values if points_ is not None)

    def tiers(self, document: dict[str, Any]) -> tuple[int | None, int | None]:
        tiers = self.table(document, "tiers", TIERS)
        hot = self.integer(tiers["hot"], "tiers.hot", 1, MAX_SCORE) if "hot" in tiers else None
        warm = self.integer(tiers["warm"], "tiers.warm", 1, MAX_SCORE) if "warm" in tiers else None
        if "cold" in tiers and not (type(tiers["cold"]) is int and tiers["cold"] == 0):
            self.problem("tiers.cold", "Must be the integer 0 (cold is every score below warm)")
        if hot is not None and warm is not None and not hot > warm:
            self.problem("tiers", "Must satisfy hot > warm > cold")
        return hot, warm

    def domains(self, document: dict[str, Any]) -> frozenset[str] | None:
        table = self.table(document, "free_email_domains", ("domains",))
        if "domains" not in table:
            return None
        domains = table["domains"]
        path = "free_email_domains.domains"
        if not isinstance(domains, list):
            self.problem(path, "Must be a list of domain names")
            return None
        if len(domains) > MAX_FREE_EMAIL_DOMAINS:
            self.problem(path, f"Must contain at most {MAX_FREE_EMAIL_DOMAINS} domains")
            return None
        valid = True
        for index, domain in enumerate(domains):
            if not (isinstance(domain, str) and _DOMAIN.fullmatch(domain) and len(domain) <= 253):
                self.problem(
                    f"{path}[{index}]", "Must be a lowercase domain name such as gmail.com"
                )
                valid = False
        if valid and len(set(domains)) != len(domains):
            self.problem(path, "Must not contain duplicates")
            valid = False
        return frozenset(domains) if valid else None
