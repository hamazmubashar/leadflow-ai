"""Sign-in page (E1): developer credit, the "Forgot password?" panel, and unchanged sign-in.

The page tests run the real view with Streamlit's AppTest. The API calls it makes
(``sign_in``, ``demo_info``) and the configured credit link are replaced with fakes.
"""

import html
import re
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from pydantic import ValidationError
from streamlit.testing.v1 import AppTest

from dashboard import session
from dashboard.api_client import ApiError, ApiErrorKind
from dashboard.config import DashboardSettings, load_settings
from dashboard.models import DemoInfo
from dashboard.sign_in_content import RESET_COMMAND, credit_html, recovery_html
from dashboard.views import sign_in as page

PROFILE = "https://github.com/example-person"


# --- the developer credit ------------------------------------------------------------------


def test_credit_without_a_url_is_plain_text() -> None:
    credit = credit_html(None)
    assert "Built by Hamaz Mubashar" in credit
    assert "AI Automation &amp; LLM Engineer" in credit
    assert "<a " not in credit
    assert "href" not in credit


def test_credit_links_the_name_to_the_configured_url() -> None:
    credit = credit_html(PROFILE)
    assert f'<a href="{PROFILE}" target="_blank" rel="noopener noreferrer">' in credit
    assert ">Hamaz Mubashar<" in credit
    assert "(opens in a new tab)" in credit  # for screen readers
    assert credit.count("<a ") == 1  # only the name is a link


def test_credit_escapes_the_url() -> None:
    credit = credit_html('https://example.com/"><script>x</script>')
    assert "<script>" not in credit
    assert "&quot;&gt;&lt;script&gt;" in credit


def test_developer_url_is_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    assert load_settings().developer_url is None
    monkeypatch.setenv("LEADFLOW_DEVELOPER_URL", "   ")
    assert load_settings().developer_url is None


def test_developer_url_is_read_and_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEADFLOW_DEVELOPER_URL", f"  {PROFILE}  ")
    assert load_settings().developer_url == PROFILE


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/me",  # not encrypted
        "javascript:alert(1)",
        "example.com/me",  # no scheme
        "https://",  # no host
        "https://user:secret@example.com/",
        "https://example.com/a b",
        "https://example.com/" + "x" * 300,
    ],
)
def test_unsafe_developer_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValidationError) as caught:
        DashboardSettings(developer_url=url)
    assert "developer_url" in str(caught.value)
    assert "secret" not in str(caught.value)  # the value is never echoed


# --- the password-help panel -------------------------------------------------------------


def test_recovery_explains_the_administrator_reset() -> None:
    panel = recovery_html(demo_available=False)
    assert "Reset your password" in panel
    assert "created by an administrator" in panel
    assert "no email reset link" in panel
    shown = re.sub(r"<[^>]+>", "", panel)  # the text a reader sees (and copies)
    assert html.escape(RESET_COMMAND) in shown
    assert "docker compose exec api" in shown
    assert "wait for the countdown" in panel
    assert "Explore the live demo" not in panel


def test_recovery_asks_for_nothing() -> None:
    panel = recovery_html(demo_available=True).lower()
    for tag in ("<input", "<form", "<textarea", "<select", "<button"):
        assert tag not in panel


def test_recovery_points_visitors_to_the_demo_when_it_is_on() -> None:
    assert "Explore the live demo" in recovery_html(demo_available=True)


def test_recovery_state() -> None:
    state: dict[str, Any] = {}
    assert not session.recovery_open(state)
    session.open_recovery(state)
    assert session.recovery_open(state)
    session.close_recovery(state)
    assert not session.recovery_open(state)
    session.close_recovery(state)  # closing twice is fine


def test_signing_in_or_out_closes_the_panel() -> None:
    state: dict[str, Any] = {}
    session.open_recovery(state)
    session.end(state, session.SIGNED_OUT)
    assert not session.recovery_open(state)
    assert session.take_notice(state) == session.SIGNED_OUT


# --- the page ------------------------------------------------------------------------------

DEMO = DemoInfo(lifetime_minutes=120, max_leads=300, max_import_rows=100)


def _run_page() -> None:
    from dashboard.views import sign_in

    sign_in.render()


class FakeApi:
    """Stands in for the API calls the page makes; records sign-in attempts."""

    def __init__(self) -> None:
        self.attempts: list[str] = []
        self.error: ApiError | None = None
        self.demo: DemoInfo | None = None
        self.url: str | None = None

    def sign_in(self, username: str, password: str) -> None:
        self.attempts.append(username)
        if self.error is not None:
            raise self.error


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeApi]:
    fake = FakeApi()
    monkeypatch.setattr(page, "sign_in", fake.sign_in)
    monkeypatch.setattr(page, "demo_info", lambda: fake.demo)
    monkeypatch.setattr(page, "developer_url", lambda: fake.url)
    yield fake


def _open(api: FakeApi) -> AppTest:
    app = AppTest.from_function(_run_page, default_timeout=10)
    app.run()
    assert not app.exception
    return app


def _button(app: AppTest, label: str) -> Any:
    matches = [b for b in app.button if b.label == label]
    assert len(matches) == 1, f"expected one {label!r} button"
    return matches[0]


def _html(app: AppTest) -> str:
    return "\n".join(m.value for m in app.markdown)


def _has(app: AppTest, label: str) -> Callable[[], bool]:
    return lambda: any(b.label == label for b in app.button)


def test_page_shows_the_form_the_link_and_a_plain_credit(api: FakeApi) -> None:
    app = _open(api)
    assert [t.label for t in app.text_input] == ["Username", "Password"]
    assert _has(app, "Forgot password?")()
    assert "Built by Hamaz Mubashar" in _html(app)
    assert "<a href" not in _html(app).split('class="lf-credit"')[1]


def test_page_links_the_credit_when_a_url_is_set(api: FakeApi) -> None:
    api.url = PROFILE
    app = _open(api)
    assert f'<a href="{PROFILE}"' in _html(app)


def test_forgot_password_opens_the_panel_and_back_returns(api: FakeApi) -> None:
    app = _open(api)
    _button(app, "Forgot password?").click().run()
    assert "Reset your password" in _html(app)
    assert len(app.text_input) == 0  # the panel asks for nothing
    assert not _has(app, "Forgot password?")()
    assert "Built by Hamaz Mubashar" in _html(app)  # the credit stays

    _button(app, "Back to sign in").click().run()
    assert [t.label for t in app.text_input] == ["Username", "Password"]
    assert "Reset your password" not in _html(app)
    assert api.attempts == []  # nothing was sent to the API


def test_panel_mentions_the_demo_only_when_it_is_on(api: FakeApi) -> None:
    app = _open(api)
    _button(app, "Forgot password?").click().run()
    assert "Just looking around?" not in _html(app)

    api.demo = DEMO
    app = _open(api)
    assert _has(app, "Explore the live demo")()
    _button(app, "Forgot password?").click().run()
    assert "Just looking around?" in _html(app)


def test_wrong_password_still_shows_the_same_message_and_hint(api: FakeApi) -> None:
    api.error = ApiError(
        ApiErrorKind.UNAUTHORIZED,
        "Invalid username or password",
        status_code=401,
        code="auth.invalid_credentials",
    )
    app = _open(api)
    app.text_input[0].input("hamaz")
    app.text_input[1].input("not-the-password")
    app.button[0].click().run()  # the form's Sign in button comes first
    assert api.attempts == ["hamaz"]
    assert "Invalid username or password" in _html(app)
    assert page.LOCKOUT_HINT in _html(app)


def test_lockout_still_disables_the_form_and_the_panel_still_opens(api: FakeApi) -> None:
    api.error = ApiError(
        ApiErrorKind.HTTP_ERROR,
        "Too many failed attempts",
        status_code=429,
        code="auth.locked",
        detail={"retry_after_seconds": 900},
    )
    app = _open(api)
    app.text_input[0].input("hamaz")
    app.text_input[1].input("wrong-again")
    app.button[0].click().run()
    assert all(t.disabled for t in app.text_input)
    assert "Try again in" in _html(app)

    _button(app, "Forgot password?").click().run()
    assert "Reset your password" in _html(app)
    _button(app, "Back to sign in").click().run()
    assert all(t.disabled for t in app.text_input)  # going back doesn't lift the lock
    assert api.attempts == ["hamaz"]
