"""Theme and small layout helpers shared by every dashboard view.

Any text that can come from the API (lead names, emails, error messages) must go
through ``html.escape`` before it is placed in custom HTML, and through
``escape_markdown`` before it is placed in Markdown (alerts, captions).
"""

import html
import re
from enum import StrEnum

import streamlit as st

from dashboard.api_client import ApiError, ApiErrorKind

NAVY = "#0B1F3B"
SURFACE = "#0F2A44"
BORDER = "#1E3A5F"
ORANGE = "#F97316"
SUCCESS = "#10B981"
WARNING = "#F59E0B"
DANGER = "#EF4444"
TEXT = "#F8FAFC"
MUTED = "#94A3B8"
COLD = "#3987e5"  # cold tier: the validated chart blue

_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~$:])")

_CSS = f"""
<style>
  [data-testid="stMainBlockContainer"] {{ padding-top: 2.5rem; }}
  .lf-header h1 {{ margin: 0; font-size: 1.75rem; font-weight: 700; color: {TEXT}; }}
  .lf-header p {{ margin: 0.25rem 0 1.25rem; color: {MUTED}; }}
  .lf-card {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 12px;
    padding: 1rem 1.25rem; margin-bottom: 0.75rem;
  }}
  .lf-card .lf-label {{ color: {MUTED}; font-size: 0.85rem; margin-bottom: 0.35rem; }}
  .lf-card .lf-value {{ color: {TEXT}; font-size: 1.05rem; font-weight: 600; }}
  .lf-pill {{
    display: inline-block; padding: 0.15rem 0.6rem; border-radius: 999px;
    font-size: 0.8rem; font-weight: 600; border: 1px solid currentColor;
  }}
  .lf-kpi {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 12px;
    border-top: 3px solid var(--accent); padding: 1rem 1.25rem; margin-bottom: 1rem;
  }}
  .lf-kpi .lf-label {{ color: {MUTED}; font-size: 0.9rem; }}
  .lf-kpi .lf-big {{ color: {TEXT}; font-size: 2.2rem; font-weight: 700; line-height: 1.2; }}
  .lf-kpi .lf-sub {{ color: {MUTED}; font-size: 0.8rem; }}
  .lf-table {{ width: 100%; border-collapse: collapse; font-size: 0.88rem; }}
  .lf-table th {{
    text-align: left; color: {MUTED}; font-weight: 600; padding: 0.45rem 0.5rem;
    border-bottom: 1px solid {BORDER};
  }}
  .lf-table td {{
    color: {TEXT}; padding: 0.5rem; border-bottom: 1px solid {BORDER}; vertical-align: top;
  }}
  .lf-table td.lf-muted {{ color: {MUTED}; white-space: nowrap; }}
  .lf-table td .lf-small {{ color: {MUTED}; font-size: 0.78rem; }}
  .lf-brand {{ font-size: 1.3rem; font-weight: 700; color: {TEXT}; }}
  .lf-brand span {{ color: {ORANGE}; }}
</style>
"""


class Tone(StrEnum):
    SUCCESS = SUCCESS
    WARNING = WARNING
    DANGER = DANGER
    NEUTRAL = MUTED
    ACCENT = ORANGE


def escape_markdown(text: str) -> str:
    """Backslash-escape Markdown syntax so untrusted text renders literally."""
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text)


def apply_theme() -> None:
    """Inject the shared CSS. Colours for native widgets come from .streamlit/config.toml."""
    st.markdown(_CSS, unsafe_allow_html=True)


def brand() -> None:
    logo = (
        '<svg width="22" height="20" viewBox="0 0 22 20" aria-hidden="true" '
        'style="vertical-align:-2px;margin-right:8px">'
        f'<rect x="0" y="12" width="5" height="8" rx="1.5" fill="{ORANGE}"/>'
        f'<rect x="8" y="6" width="5" height="14" rx="1.5" fill="{ORANGE}"/>'
        f'<rect x="16" y="0" width="5" height="20" rx="1.5" fill="{ORANGE}"/></svg>'
    )
    st.markdown(
        f'<div class="lf-brand">{logo}LeadFlow <span>AI</span></div>', unsafe_allow_html=True
    )


def page_header(title: str, subtitle: str = "") -> None:
    sub = f"<p>{html.escape(subtitle)}</p>" if subtitle else ""
    st.markdown(
        f'<div class="lf-header"><h1>{html.escape(title)}</h1>{sub}</div>',
        unsafe_allow_html=True,
    )


def pill(label: str, tone: Tone) -> str:
    """Return HTML for a status pill (render inside ``card`` or ``st.markdown(..., True)``)."""
    return f'<span class="lf-pill" style="color:{tone.value}">{html.escape(label)}</span>'


def card(label: str, value_html: str) -> None:
    """A labelled panel. ``value_html`` must already be escaped (e.g. built with ``pill``)."""
    st.markdown(
        f'<div class="lf-card"><div class="lf-label">{html.escape(label)}</div>'
        f'<div class="lf-value">{value_html}</div></div>',
        unsafe_allow_html=True,
    )


def text_card(label: str, value: str) -> None:
    """A labelled panel for plain, untrusted text."""
    card(label, html.escape(value))


def show_api_error(error: ApiError) -> None:
    """Render an ``ApiError`` as an alert, with the API's error code when there is one.

    A rejected session token has already signed this tab out (see ``runtime``); rerun
    straight to the sign-in page, which explains what happened.
    """
    if error.kind is ApiErrorKind.UNAUTHORIZED:
        st.rerun()
    text = escape_markdown(error.message)
    if error.code:
        text += f"  \nError code: `{error.code.replace('`', '')}`"  # code span: shown literally
    if error.is_transport_failure:
        st.warning(text, icon=":material/cloud_off:")
    else:
        st.error(text, icon=":material/error:")


def kpi_card(label: str, value: str, sub: str, accent: str) -> None:
    """A KPI tile: label, big value, one muted line. ``accent`` is a theme colour constant."""
    st.markdown(
        f'<div class="lf-kpi" style="--accent:{accent}">'
        f'<div class="lf-label">{html.escape(label)}</div>'
        f'<div class="lf-big">{html.escape(value)}</div>'
        f'<div class="lf-sub">{html.escape(sub)}</div></div>',
        unsafe_allow_html=True,
    )


def html_table(headers: list[str], rows: list[list[str]]) -> None:
    """A themed table. Cells are HTML: build them with ``cell``/``pill``, never raw API text."""
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(row) + "</tr>" for row in rows)
    st.markdown(
        f'<table class="lf-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>',
        unsafe_allow_html=True,
    )


def cell(text: str, *, muted: bool = False, small: str = "") -> str:
    """One escaped table cell, optionally with a muted second line."""
    second = f'<br><span class="lf-small">{html.escape(small)}</span>' if small else ""
    css = ' class="lf-muted"' if muted else ""
    return f"<td{css}>{html.escape(text)}{second}</td>"


def show_error_lines(title: str, lines: list[str]) -> None:
    """An error alert: a fixed title plus one escaped bullet per line."""
    bullets = "  \n".join(f"- {escape_markdown(line)}" for line in lines)
    st.error(f"{escape_markdown(title)}  \n{bullets}", icon=":material/error:")


def set_flash(key: str, message: str) -> None:
    """Store a success message to show once after the next rerun (plain text, escaped later)."""
    st.session_state[key] = message


def show_flash(key: str) -> None:
    message = st.session_state.pop(key, None)
    if isinstance(message, str):
        st.success(escape_markdown(message), icon=":material/check_circle:")


def status_tone(status: str) -> Tone:
    """Pill colour for a lead status value (identity only; the label always carries the name)."""
    return {
        "won": Tone.SUCCESS,
        "lost": Tone.DANGER,
        "qualified": Tone.ACCENT,
        "contacted": Tone.WARNING,
    }.get(status, Tone.NEUTRAL)


def tier_tone(tier: str) -> Tone:
    return {"hot": Tone.DANGER, "warm": Tone.WARNING}.get(tier, Tone.NEUTRAL)
