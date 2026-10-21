"""Sign in (M11): the only page a tab sees until it has a session.

The password goes to the API once and is not stored; the form is cleared on submit.
A wrong password, an unknown user, and a locked or deactivated account all get the same
message, like the API.

Lockout: the API locks a username for 15 minutes after 3 consecutive failures and says
how long (``auth.locked``, ``retry_after_seconds``). This page then disables the form
and shows a countdown that updates every second. The API enforces the lock: refreshing
the page clears this tab's countdown, but the next attempt is refused and the countdown
comes back.

Layout: brand panel left (about 55%) and the sign-in card right on desktop; one column
on narrow screens. All styling is scoped to this page.
"""

import html
import time

import streamlit as st

from dashboard import session
from dashboard.api_client import ApiError
from dashboard.config import ConfigError
from dashboard.runtime import sign_in

PAGE_BG = "#08172C"
CARD_BG = "#0C2038"
INPUT_BG = "#122D49"
ORANGE = "#FF7417"
ORANGE_HOVER = "#FF8A3D"
TEXT = "#F5F8FC"
MUTED = "#9FB0C7"
LINE = "rgba(148, 163, 184, 0.20)"
LOCKOUT_HINT = "After 3 failed attempts in a row, sign-in is locked for 15 minutes."
LOCKED_TITLE = {
    "auth.locked": "Too many failed attempts. Sign-in is temporarily locked.",
    "auth.rate_limited": "Too many sign-in attempts from this computer. Please wait.",
}

_CSS = f"""
<style>
  .stApp, [data-testid="stAppViewContainer"] {{ background: {PAGE_BG}; }}
  [data-testid="stHeader"] {{ background: transparent; }}
  [data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"] {{ display: none; }}
  [data-testid="stMainBlockContainer"] {{
    max-width: 1240px; padding: max(3rem, calc(50vh - 360px)) 3rem 3rem;
  }}

  /* brand panel */
  .lf-brand-panel {{ padding: 0 2rem 0 0; }}
  .lf-logo {{ display: flex; align-items: center; gap: 0.8rem; margin-bottom: 2.75rem; }}
  .lf-logo .lf-mark {{
    width: 48px; height: 48px; border-radius: 12px; display: grid; place-items: center;
    background: {CARD_BG}; border: 1px solid rgba(255, 116, 23, 0.40);
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.30);
  }}
  .lf-logo .lf-name {{ font-size: 1.5rem; font-weight: 700; color: {TEXT}; }}
  .lf-logo .lf-name span {{ color: {ORANGE}; }}
  .lf-headline {{
    font-size: 3rem; line-height: 1.12; font-weight: 800; color: {TEXT};
    letter-spacing: -0.02em; margin: 0 0 1.25rem; max-width: 34rem;
  }}
  .lf-headline em {{ font-style: normal; color: {ORANGE}; }}
  .lf-lede {{ color: {MUTED}; font-size: 1.15rem; line-height: 1.65; margin: 0 0 2.25rem;
    max-width: 32rem; }}
  .lf-benefits {{ display: grid; gap: 0.9rem; max-width: 32rem; }}
  .lf-benefit {{
    display: flex; gap: 1rem; align-items: center; padding: 0.95rem 1.1rem;
    background: rgba(12, 32, 56, 0.70); border: 1px solid {LINE}; border-radius: 14px;
  }}
  .lf-benefit .lf-icon {{
    flex: none; width: 40px; height: 40px; border-radius: 10px; display: grid;
    place-items: center; background: rgba(255, 116, 23, 0.12);
  }}
  .lf-benefit b {{ display: block; color: {TEXT}; font-size: 1rem; font-weight: 600; }}
  .lf-benefit span {{ color: {MUTED}; font-size: 0.9rem; }}
  .lf-pipeline {{ margin-top: 2.25rem; max-width: 32rem; opacity: 0.9; }}

  /* sign-in card */
  [data-testid="stForm"] {{
    max-width: 480px; margin-left: auto; background: {CARD_BG}; border: 1px solid {LINE};
    border-radius: 20px; padding: 2.5rem 2.25rem 2.25rem;
    box-shadow: 0 24px 60px rgba(0, 0, 0, 0.40), 0 1px 0 rgba(255, 255, 255, 0.04) inset;
  }}
  .lf-card-head .lf-h1 {{ font-size: 1.85rem; font-weight: 700; color: {TEXT};
    letter-spacing: -0.01em; margin: 0 0 0.4rem; }}
  .lf-card-head p {{ color: {MUTED}; font-size: 1rem; margin: 0 0 1.75rem; }}
  [data-testid="stForm"] label p {{ color: {TEXT}; font-size: 0.95rem; font-weight: 600; }}
  [data-testid="stForm"] [data-baseweb="input"] {{
    min-height: 52px; background: {INPUT_BG}; border: 1px solid {LINE}; border-radius: 12px;
    transition: border-color 120ms ease, box-shadow 120ms ease;
  }}
  [data-testid="stForm"] [data-baseweb="input"] > div,
  [data-testid="stForm"] input {{ background: transparent; color: {TEXT}; font-size: 1rem; }}
  [data-testid="stForm"] input::placeholder {{ color: #7F93AD; }}
  [data-testid="stForm"] [data-baseweb="input"]:focus-within {{
    border-color: {ORANGE}; box-shadow: 0 0 0 3px rgba(255, 116, 23, 0.30);
  }}
  [data-testid="stForm"] [data-testid="stTextInput"] {{ margin-bottom: 0.35rem; }}
  [data-testid="stFormSubmitButton"], .stFormSubmitButton,
  [data-testid="stFormSubmitButton"] > div {{ width: 100%; }}
  [data-testid="stFormSubmitButton"] button {{
    width: 100%; min-height: 52px; margin-top: 0.75rem; border-radius: 12px; border: 0;
    background: {ORANGE}; color: #FFFFFF; font-weight: 700; font-size: 1.02rem;
  }}
  [data-testid="stFormSubmitButton"] button:hover {{ background: {ORANGE_HOVER}; color: #FFF; }}
  [data-testid="stFormSubmitButton"] button:focus-visible {{
    outline: 3px solid rgba(255, 116, 23, 0.60); outline-offset: 3px;
  }}
  [data-testid="stFormSubmitButton"] button:disabled {{
    background: #3A4A60; color: #B7C3D3; cursor: not-allowed;
  }}

  /* messages */
  .lf-alert {{
    display: flex; gap: 0.75rem; align-items: flex-start; border-radius: 12px;
    padding: 0.95rem 1.1rem; margin: 1.1rem 0 0 auto; max-width: 480px;
    font-size: 0.95rem; line-height: 1.5;
  }}
  .lf-alert svg {{ flex: none; margin-top: 2px; }}
  .lf-alert small {{ display: block; margin-top: 0.25rem; font-size: 0.86rem; opacity: 0.88; }}
  .lf-alert .lf-count {{ font-variant-numeric: tabular-nums; font-weight: 700; }}
  .lf-alert-error {{ background: rgba(239, 68, 68, 0.10);
    border: 1px solid rgba(239, 68, 68, 0.38); color: #FECACA; }}
  .lf-alert-info {{ background: rgba(56, 189, 248, 0.08);
    border: 1px solid rgba(56, 189, 248, 0.32); color: #D5ECF8; }}
  .lf-foot {{ color: {MUTED}; font-size: 0.85rem; text-align: center; max-width: 480px;
    margin: 1.25rem 0 0 auto; }}

  @media (max-width: 900px) {{
    [data-testid="stMainBlockContainer"] {{ padding: 2rem 1.25rem; }}
    .lf-brand-panel {{ padding: 0; }}
    .lf-logo {{ margin-bottom: 1.5rem; }}
    .lf-headline {{ font-size: 2rem; }}
    .lf-lede {{ font-size: 1rem; margin-bottom: 1.25rem; }}
    .lf-benefits, .lf-pipeline {{ display: none; }}
    [data-testid="stForm"], .lf-alert, .lf-foot {{ max-width: none; margin-left: 0; }}
    [data-testid="stForm"] {{ padding: 1.75rem 1.25rem 1.5rem; }}
  }}
</style>
"""

_LOGO = (
    '<svg width="24" height="22" viewBox="0 0 22 20" aria-hidden="true">'
    f'<rect x="0" y="12" width="5" height="8" rx="1.5" fill="{ORANGE}"/>'
    f'<rect x="8" y="6" width="5" height="14" rx="1.5" fill="{ORANGE}"/>'
    f'<rect x="16" y="0" width="5" height="20" rx="1.5" fill="{ORANGE}"/></svg>'
)


def _icon(path: str) -> str:
    return (
        '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true" '
        f'stroke="{ORANGE}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        f"{path}</svg>"
    )


_BENEFITS = (
    (
        _icon(
            '<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>'
        ),
        "Role-based access",
        "Admins make changes; viewers can see everything but change nothing.",
    ),
    (
        _icon('<path d="M20 6 9 17l-5-5"/>'),
        "Human-approved outreach",
        "Every AI-assisted draft waits for a person to approve it.",
    ),
    (
        _icon('<path d="M4 6h16M4 12h16M4 18h10"/>'),
        "Complete audit trail",
        "Each change records who made it and when.",
    ),
)

# A quiet pipeline motif: capture -> score -> draft -> approve.
_PIPELINE = (
    '<svg class="lf-pipeline" viewBox="0 0 480 72" role="img" '
    'aria-label="Lead pipeline: capture, score, draft, approve">'
    '<path d="M36 30 H444" stroke="rgba(148,163,184,0.28)" stroke-width="2" '
    'stroke-dasharray="4 6"/>'
    + "".join(
        f'<circle cx="{x}" cy="30" r="13" fill="#0C2038" '
        f'stroke="{ORANGE if i == 3 else "rgba(148,163,184,0.45)"}" stroke-width="2"/>'
        f'<circle cx="{x}" cy="30" r="4" fill="{ORANGE if i == 3 else "#5D7391"}"/>'
        f'<text x="{x}" y="64" text-anchor="middle" fill="#7F93AD" font-size="12" '
        f'font-family="sans-serif">{label}</text>'
        for i, (x, label) in enumerate(
            ((36, "Capture"), (172, "Score"), (308, "Draft"), (444, "Approve"))
        )
    )
    + "</svg>"
)
_ICON_ERROR = _icon(
    '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>'
).replace(ORANGE, "#F87171")
_ICON_CLOCK = _icon('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>').replace(
    ORANGE, "#F87171"
)
_ICON_INFO = _icon('<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.1"/>').replace(
    ORANGE, "#7DD3FC"
)


def _brand_panel() -> None:
    benefits = "".join(
        f'<div class="lf-benefit"><div class="lf-icon">{icon}</div>'
        f"<div><b>{html.escape(title)}</b><span>{html.escape(text)}</span></div></div>"
        for icon, title, text in _BENEFITS
    )
    st.markdown(
        '<div class="lf-brand-panel">'
        f'<div class="lf-logo"><div class="lf-mark">{_LOGO}</div>'
        '<div class="lf-name">LeadFlow <span>AI</span></div></div>'
        '<div class="lf-headline" role="heading" aria-level="2">Every lead captured, '
        "scored and <em>followed up.</em></div>"
        '<p class="lf-lede">AI-powered lead management that keeps your pipeline organized '
        "and every outreach decision under human control.</p>"
        f'<div class="lf-benefits">{benefits}</div>{_PIPELINE}</div>',
        unsafe_allow_html=True,
    )


def _alert(kind: str, message: str, hint: str = "", *, icon: str = "") -> None:
    """A styled alert. ``message`` and ``hint`` are escaped; never put credentials in them."""
    icon = icon or (_ICON_ERROR if kind == "error" else _ICON_INFO)
    role = "alert" if kind == "error" else "status"
    extra = f"<small>{html.escape(hint)}</small>" if hint else ""
    st.markdown(
        f'<div class="lf-alert lf-alert-{kind}" role="{role}">{icon}'
        f"<div>{html.escape(message)}{extra}</div></div>",
        unsafe_allow_html=True,
    )


def render() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)
    locked_for = session.sign_in_locked_for(st.session_state, now=time.time())
    brand_col, form_col = st.columns([1.2, 1], gap="large", vertical_alignment="center")
    with brand_col:
        _brand_panel()
    with form_col:
        with st.form("sign_in", clear_on_submit=True, border=False):
            st.markdown(
                '<div class="lf-card-head"><div class="lf-h1" role="heading" aria-level="1">'
                "Welcome back</div><p>Sign in to your LeadFlow AI workspace.</p></div>",
                unsafe_allow_html=True,
            )
            username = st.text_input(
                "Username",
                max_chars=64,
                autocomplete="username",
                placeholder="Enter your username",
                disabled=locked_for is not None,
            )
            password = st.text_input(
                "Password",
                type="password",
                max_chars=128,
                autocomplete="current-password",
                placeholder="Enter your password",
                disabled=locked_for is not None,
            )
            submitted = st.form_submit_button(
                "Sign in", type="primary", disabled=locked_for is not None
            )
        feedback = st.container()
        st.markdown(
            '<div class="lf-foot">Sessions last 8 hours · Refreshing the page signs you out</div>',
            unsafe_allow_html=True,
        )
    with feedback:
        if locked_for is not None:
            _countdown()
            return  # no submission while locked, whatever triggered the rerun
        notice = session.take_notice(st.session_state)
        if notice and not submitted:
            _alert("info", notice)
        if submitted:
            _submit(username, password)


@st.fragment(run_every=1.0)
def _countdown() -> None:
    """Re-renders every second on its own; re-enables the form when the lock ends."""
    left = session.sign_in_locked_for(st.session_state, now=time.time())
    if left is None:
        st.rerun()  # whole page: the form comes back enabled
    title = LOCKED_TITLE.get(session.lock_reason(st.session_state), LOCKED_TITLE["auth.locked"])
    st.markdown(
        f'<div class="lf-alert lf-alert-error" role="alert">{_ICON_CLOCK}'
        f"<div>{html.escape(title)}<small>Try again in "
        f'<span class="lf-count">{session.countdown(left)}</span></small></div></div>',
        unsafe_allow_html=True,
    )


def _submit(username: str, password: str) -> None:
    if not username.strip() or not password:
        _alert("error", "Enter your username and password.")
        return
    try:
        with st.spinner("Signing in…"):
            sign_in(username, password)
    except ConfigError as exc:
        _alert("error", "The dashboard configuration is incomplete: " + "; ".join(exc.problems))
        return
    except ApiError as error:
        if error.code in session.LOCK_CODES:
            seconds = (error.detail or {}).get("retry_after_seconds")
            session.lock_sign_in(
                st.session_state,
                seconds if isinstance(seconds, int) else session.DEFAULT_LOCK_SECONDS,
                error.code or "auth.locked",
                now=time.time(),
            )
            st.rerun()  # redraw at once with the form disabled and the countdown running
        if error.code == "auth.invalid_credentials":
            _alert("error", error.message, LOCKOUT_HINT)
        else:
            _alert("error", error.message)
        return
    st.rerun()
