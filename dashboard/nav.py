"""The app's pages, registered by ``app.py`` on every run so views can link to each other."""

import streamlit as st
from streamlit.navigation.page import StreamlitPage

PAGES: dict[str, StreamlitPage] = {}


def link(key: str, label: str) -> None:
    """A link to another page; silently omitted if that page isn't registered."""
    page = PAGES.get(key)
    if page is not None:
        st.page_link(page, label=label, icon=":material/arrow_forward:")
