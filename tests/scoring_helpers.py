"""Helpers for scoring tests: the default rules as a dict, and a tiny TOML writer."""

import copy
import json
import tomllib
from importlib import resources
from typing import Any

DEFAULT_RULES_BYTES = (
    resources.files("leadflow_ai.scoring").joinpath("default_rules.toml").read_bytes()
)
DEFAULT_DOC: dict[str, Any] = tomllib.loads(DEFAULT_RULES_BYTES.decode("utf-8"))


def default_doc() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_DOC)


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    raise TypeError(type(value))


def to_toml(doc: dict[str, Any], prefix: str = "") -> str:
    """Serialize nested dicts of scalars/lists to TOML (enough for these tests)."""
    lines = [f"{key} = {_value(v)}" for key, v in doc.items() if not isinstance(v, dict)]
    for key, v in doc.items():
        if isinstance(v, dict):
            name = f"{prefix}{key}"
            lines.append(f"\n[{name}]")
            lines.append(to_toml(v, f"{name}."))
    return "\n".join(lines)


def toml_bytes(doc: dict[str, Any]) -> bytes:
    return to_toml(doc).encode("utf-8")
