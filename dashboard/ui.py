"""Theme and small layout helpers shared by every dashboard view.

Any text that can come from the API (lead names, emails, error messages) must go
through ``html.escape`` before it is placed in custom HTML, and through
``escape_markdown`` before it is placed in Markdown (alerts, captions).
"""

import html
import re
from enum import StrEnum

import streamlit as st

from dashboard.api_client import ApiError

NAVY = "#0B1F3B"
SURFACE = "#0F2A44"
BORDER = "#1E3A5F"
ORANGE = "#F97316"
SUCCESS = "#10B981"
WARNING = "#F59E0B"
DANGER = "#EF4444"
TEXT = "#F8FAFC"
MUTED = "#94A3B8"

_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~$:])")

_CSS = f"""
<style>
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
    st.markdown('<div class="lf-brand">LeadFlow <span>AI</span></div>', unsafe_allow_html=True)


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
    """Render an ``ApiError`` as an alert, with the API's error code when there is one."""
    text = escape_markdown(error.message)
    if error.code:
        text += f"  \nError code: `{error.code.replace('`', '')}`"  # code span: shown literally
    if error.is_transport_failure:
        st.warning(text, icon=":material/cloud_off:")
    else:
        st.error(text, icon=":material/error:")
