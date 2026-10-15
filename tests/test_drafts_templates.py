"""Draft templates: deterministic rendering, the fields used, and no network-capable code."""

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

import leadflow_ai
from leadflow_ai.drafts import (
    ELIGIBLE_LEAD_STATUSES,
    MAX_BODY_LENGTH,
    MAX_SUBJECT_LENGTH,
    TEMPLATE_VERSION,
    DraftTemplate,
    RenderedDraft,
    is_eligible,
    render_draft,
)
from leadflow_ai.lifecycle import LeadStatus

PACKAGE_DIR = Path(leadflow_ai.__file__).parent


@dataclass(frozen=True)
class FakeLead:
    full_name: str
    company: str | None
    # Fields a template must never use: the values below must not appear in any draft.
    email: str = "secret.address@example.com"
    phone: str = "+923001234567"
    notes: str = "Private note about CNIC 35202"
    source: str = "referral"
    status: str = "qualified"


AYESHA = FakeLead("Ayesha Khan", "Acme Traders")
BILAL = FakeLead("Bilal", None)


def test_introduction_with_company() -> None:
    assert render_draft(DraftTemplate.INTRODUCTION, AYESHA) == RenderedDraft(
        subject="Introduction for Acme Traders",
        body="Hello Ayesha Khan,\n\n"
        "Thank you for getting in touch on behalf of Acme Traders. I would welcome a short"
        " call to learn more about your needs and how we might help.\n\n"
        "Would a time next week suit you?\n\n"
        "Kind regards",
    )


def test_introduction_without_company_uses_the_name() -> None:
    rendered = render_draft(DraftTemplate.INTRODUCTION, BILAL)
    assert rendered.subject == "Introduction for Bilal"
    assert rendered.body.startswith("Hello Bilal,\n\nThank you for getting in touch. I would")


def test_follow_up_with_and_without_company() -> None:
    assert render_draft(DraftTemplate.FOLLOW_UP, AYESHA) == RenderedDraft(
        subject="Following up with Acme Traders",
        body="Hello Ayesha Khan,\n\n"
        "I am following up on my earlier message regarding Acme Traders. If now is not a good"
        " time, just let me know and I will not follow up again.\n\n"
        "Kind regards",
    )
    rendered = render_draft(DraftTemplate.FOLLOW_UP, BILAL)
    assert rendered.subject == "Following up with Bilal"
    assert "earlier message. If now" in rendered.body


def test_empty_company_is_treated_as_absent() -> None:
    assert render_draft(DraftTemplate.INTRODUCTION, FakeLead("Bilal", "")) == render_draft(
        DraftTemplate.INTRODUCTION, BILAL
    )


@pytest.mark.parametrize("template", list(DraftTemplate))
def test_rendering_is_deterministic(template: DraftTemplate) -> None:
    assert len({render_draft(template, AYESHA) for _ in range(50)}) == 1


@pytest.mark.parametrize("template", list(DraftTemplate))
def test_only_name_and_company_are_used(template: DraftTemplate) -> None:
    rendered = render_draft(template, AYESHA)
    text = rendered.subject + rendered.body
    for unused in ("secret.address", "example.com", "92300", "CNIC", "35202", "referral"):
        assert unused not in text
    # Changing any other field never changes the text.
    other = FakeLead("Ayesha Khan", "Acme Traders", "x@y.org", "+441234", "n", "event", "won")
    assert render_draft(template, other) == rendered


@pytest.mark.parametrize("template", list(DraftTemplate))
def test_braces_in_values_are_inserted_literally(template: DraftTemplate) -> None:
    rendered = render_draft(template, FakeLead("{full_name} {0}", "{company}"))
    assert "{full_name} {0}" in rendered.body
    assert "{company}" in rendered.subject


@pytest.mark.parametrize("template", list(DraftTemplate))
def test_longest_allowed_values_fit_the_stored_lengths(template: DraftTemplate) -> None:
    rendered = render_draft(template, FakeLead("N" * 100, "C" * 120))  # M1's maximum lengths
    assert len(rendered.subject) <= MAX_SUBJECT_LENGTH
    assert len(rendered.body) <= MAX_BODY_LENGTH


def test_template_version_and_eligibility() -> None:
    assert TEMPLATE_VERSION == "2026-10-15.1"
    assert [s.value for s in ELIGIBLE_LEAD_STATUSES] == ["new", "contacted", "qualified"]
    assert [s for s in LeadStatus if is_eligible(s)] == list(ELIGIBLE_LEAD_STATUSES)


# --- no network-capable code ----------------------------------------------------------------

# Every module M6 adds may only import from these top-level packages. Nothing here can
# open a socket, send mail or start a process.
ALLOWED_IMPORTS = {
    "drafts/__init__.py": {"leadflow_ai"},
    "drafts/templates.py": {"dataclasses", "enum", "typing", "leadflow_ai"},
    "services/drafts.py": {
        "collections",
        "dataclasses",
        "datetime",
        "typing",
        "uuid",
        "sqlalchemy",
        "leadflow_ai",
    },
    "api/drafts.py": {"datetime", "typing", "fastapi", "pydantic", "leadflow_ai"},
}
NETWORK_MODULES = {
    "smtplib",
    "socket",
    "ssl",
    "http",
    "urllib",
    "email",
    "httpx",
    "requests",
    "asyncio",
    "subprocess",
    "ftplib",
    "imaplib",
    "poplib",
    "xmlrpc",
}


def _imports(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
    return modules


@pytest.mark.parametrize("relative", sorted(ALLOWED_IMPORTS))
def test_m6_modules_import_only_allowed_packages(relative: str) -> None:
    modules = _imports(PACKAGE_DIR / relative)
    assert {m.split(".")[0] for m in modules} <= ALLOWED_IMPORTS[relative], relative
    assert not {m.split(".")[0] for m in modules} & NETWORK_MODULES
    if relative.startswith("drafts/"):  # the template package stays pure
        assert all(
            m.startswith(("leadflow_ai.drafts", "leadflow_ai.lifecycle"))
            for m in modules
            if m.startswith("leadflow_ai")
        ), modules


def test_every_m6_module_is_checked() -> None:
    found = {
        str(path.relative_to(PACKAGE_DIR))
        for pattern in ("drafts/*.py", "services/drafts.py", "api/drafts.py")
        for path in PACKAGE_DIR.glob(pattern)
    }
    assert found == set(ALLOWED_IMPORTS)


# M9: the single, approved exception. The local-model client may use urllib/http to
# reach a loopback-only URL (validated at startup; proxies and redirects refused; see
# tests/test_assist_ollama.py and tests/test_config_llm.py). Nothing else may.
NETWORK_EXCEPTIONS = {"assist/ollama.py": {"urllib", "http"}}
# Pure string parsing, no I/O: config.py uses it to validate the model URL.
PURE_SUBMODULES = {"urllib.parse"}


def _network_top_levels(path: Path) -> set[str]:
    modules = {m for m in _imports(path) if m not in PURE_SUBMODULES}
    return {m.split(".")[0] for m in modules} & NETWORK_MODULES


def test_no_module_in_the_package_imports_network_libraries() -> None:
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        relative = str(path.relative_to(PACKAGE_DIR))
        allowed = NETWORK_EXCEPTIONS.get(relative, set())
        assert not _network_top_levels(path) - allowed, path


def test_the_network_exception_is_exactly_the_local_model_client() -> None:
    for relative, allowed in NETWORK_EXCEPTIONS.items():
        # No stale or wider exception.
        assert _network_top_levels(PACKAGE_DIR / relative) == allowed, relative
    # Outside the assist package, only the AI-draft service uses it.
    importers = {
        str(path.relative_to(PACKAGE_DIR))
        for path in PACKAGE_DIR.rglob("*.py")
        if not str(path.relative_to(PACKAGE_DIR)).startswith("assist/")
        and any(m.startswith("leadflow_ai.assist") for m in _imports(path))
    }
    assert importers == {"services/ai_drafts.py"}
