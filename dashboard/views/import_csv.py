"""CSV Import: upload one file; the API imports every row or none of them."""

import hashlib

import streamlit as st

from dashboard.api_client import ApiError
from dashboard.imports import (
    COLUMNS,
    MAX_FILE_BYTES,
    MAX_ROWS,
    TEMPLATE_CSV,
    ImportFailure,
    describe_import_error,
)
from dashboard.runtime import READ_ONLY_HELP, can_write, clear_lead_data, require_client
from dashboard.ui import ORANGE, SUCCESS, cell, escape_markdown, html_table, kpi_card, page_header

_RESULT_KEY = "import.result"  # (file digest, imported count) of the last success


def render() -> None:
    page_header(
        "CSV Import",
        "Upload a CSV file. Every row is validated; the file is imported all or nothing.",
    )
    client = require_client()

    rules_col, template_col = st.columns([3, 1], vertical_alignment="bottom")
    with rules_col:
        st.markdown(
            f"**Columns:** {', '.join(f'`{c}`' for c in COLUMNS)}  \n"
            "`full_name` plus `email` and/or `phone` are required; the rest are optional.  \n"
            f"**Limits:** {MAX_FILE_BYTES // 1024} KiB (1 MiB) per file · {MAX_ROWS:,} rows · "
            "UTF-8 (in Excel: *CSV UTF-8*)."
        )
    with template_col:
        st.download_button(
            "Download template",
            TEMPLATE_CSV.encode(),
            file_name="leadflow-template.csv",
            mime="text/csv",
            icon=":material/download:",
            width="stretch",
        )

    if not can_write():
        st.info(READ_ONLY_HELP, icon=":material/visibility:")
        _show_last_result()
        return
    upload = st.file_uploader("CSV file", type=["csv"], key="import.file")
    if upload is None:
        _show_last_result()
        return

    content = upload.getvalue()
    digest = hashlib.sha256(content).hexdigest()
    st.caption(f"{escape_markdown(upload.name)} · {len(content):,} bytes")
    last = st.session_state.get(_RESULT_KEY)
    if isinstance(last, tuple) and last[0] == digest:
        _success(last[1])
        st.info(
            "This exact file was already imported. Uploading it again would be rejected as "
            "duplicates, so there is nothing to do.",
            icon=":material/info:",
        )
        return

    if not st.button("Import leads", type="primary", icon=":material/upload:"):
        return
    try:
        with st.spinner("Importing…"):
            result = client.import_csv(content)
    except ApiError as error:
        _failure(describe_import_error(error))
        return
    clear_lead_data()
    st.session_state[_RESULT_KEY] = (digest, result.imported)
    _success(result.imported)


def _success(imported: int) -> None:
    left, right, _ = st.columns([1, 1, 2])
    with left:
        kpi_card("Imported", str(imported), "new leads, all rows valid", SUCCESS)
    with right:
        kpi_card("Rejected", "0", "an import is all or nothing", ORANGE)
    st.success(
        f"Import completed: {imported} leads added. They start as **New** and appear on the "
        "Leads page.",
        icon=":material/check_circle:",
    )


def _show_last_result() -> None:
    last = st.session_state.get(_RESULT_KEY)
    if isinstance(last, tuple):
        st.caption(f"Last import in this session: {last[1]} leads added.")


def _failure(failure: ImportFailure) -> None:
    if failure.outcome_unknown:
        st.warning(escape_markdown(failure.summary), icon=":material/help:")
        return
    st.error(escape_markdown(failure.summary), icon=":material/error:")
    if failure.rows:
        html_table(
            ["Row", "Field", "Problem", "Reference"],
            [
                [
                    cell(problem.row, muted=True),
                    cell(problem.field),
                    cell(problem.problem),
                    cell(problem.reference, muted=True),
                ]
                for problem in failure.rows
            ],
        )
    if failure.truncated:
        st.caption("Only the first 100 problems are listed; fix these and upload again.")
