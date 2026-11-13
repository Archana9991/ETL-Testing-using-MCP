"""The "SQL lineage" tab of the web UI.

Same rule as ui_helpers.py: the logic is plain Python that pytest can
test quickly, and the screen code is a thin layer on top.

  * TOP of this file    : helpers with NO Streamlit code.
  * render() at the end : draws the tab.  Streamlit (and sqlglot) are
    imported INSIDE render(), so importing this file never fails, even
    if sqlglot is not installed.  The tab then shows a friendly hint.
"""
import re

import pandas as pd

# Must match the keys of lineage.DIALECTS (a test checks this).
DIALECT_NAMES = ["ansi", "sqlite", "postgres", "oracle", "mssql"]

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_$#]*")


def parse_schema_text(text):
    """Turn the schema box into the dict that lineage.trace_query wants.

    One table per line:

        customers: cust_id, first_name, city
        branches:  code, branch_name

    becomes {"customers": {"cust_id": "text", ...}, "branches": {...}}.
    Blank lines and lines starting with # are ignored.  A mistake raises
    ValueError that names the line, so the screen can show it plainly.
    The type is a placeholder ("text"): lineage only needs the NAMES.
    """
    schema = {}
    for number, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"Line {number}: write it as  "
                             f"table: column1, column2")
        table, _, rest = line.partition(":")
        table = table.strip()
        if not _NAME.fullmatch(table):
            raise ValueError(
                f"Line {number}: {table!r} is not a plain table name. "
                f"Use just the table name (no schema prefix, no spaces).")
        names = [n for n in re.split(r"[,\s]+", rest) if n]
        if not names:
            raise ValueError(f"Line {number}: no columns listed "
                             f"for {table}.")
        for name in names:
            if not _NAME.fullmatch(name):
                raise ValueError(f"Line {number}: {name!r} is not a "
                                 f"plain column name.")
        columns = schema.setdefault(table, {})
        for name in names:
            columns[name] = "text"
    return schema


def decode_sql(data):
    """Bytes of an uploaded .sql file -> text.

    Tries UTF-8 first (and drops a leading BOM that some editors add),
    then Windows-1252, which is what older Windows tools often save.
    """
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252")


def filter_frame(frame, table):
    """Keep every row of the output columns that depend on `table`.

    `frame` is lineage.to_frame(...).  We keep ALL rows of a matching
    output column (not only the rows from `table`), so you still see its
    other sources.  `table` is compared without schema prefix and
    ignoring case.  An empty `table` returns the frame unchanged.
    """
    if not table:
        return frame
    want = table.split(".")[-1].lower()
    last = frame["source_table"].str.split(".").str[-1].str.lower()
    outputs = frame.loc[last == want, "output_column"].unique()
    return frame[frame["output_column"].isin(outputs)]


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------
def render():
    """Draw the SQL lineage tab.  Call it inside a `with tab:` block."""
    import streamlit as st

    try:
        from .lineage import (LineageError, ask_about_lineage, source_tables,
                              to_frame, trace_query)
    except ImportError:
        st.warning("SQL lineage needs the sqlglot package. "
                   "Run:  pip install sqlglot")
        return

    st.write("Paste a SELECT (or INSERT ... SELECT) and see which source "
             "table and column each output column comes from.")
    st.caption("Only names are needed. Please do not paste real personal "
               "data inside quoted values.")

    left, right = st.columns([3, 2])
    with left:
        upload = st.file_uploader("SQL file", type=["sql", "txt"],
                                  key="lin_up")
        st.text_area("...or paste SQL here", height=260, key="lin_sql")
    with right:
        st.selectbox("SQL dialect", DIALECT_NAMES, key="lin_dialect")
        st.text_area(
            "Table columns (optional)", height=180, key="lin_schema",
            placeholder="customers: cust_id, first_name, city\n"
                        "branches: code, branch_name",
            help="One table per line. Needed to expand SELECT * and to "
                 "resolve a column with no table prefix when the query "
                 "joins several tables.")

    if st.button("Trace columns", key="lin_go", type="primary"):
        # An uploaded file wins over pasted text (same rule as the other tabs)
        sql = (decode_sql(upload.getvalue()) if upload is not None
               else st.session_state.get("lin_sql", ""))
        st.session_state.pop("lineage", None)
        st.session_state.pop("lineage_answer", None)
        if not sql.strip():
            st.warning("Paste some SQL or upload a .sql file first.")
        else:
            try:
                schema = parse_schema_text(st.session_state.get("lin_schema"))
                results = trace_query(sql, schema or None,
                                      st.session_state.lin_dialect)
                st.session_state.lineage = {"results": results}
            except (ValueError, LineageError) as exc:
                st.error(str(exc))

    state = st.session_state.get("lineage")
    if not state:
        return
    results = state["results"]
    frame = to_frame(results)

    a, b = st.columns(2)
    a.metric("Output columns", len(results))
    b.metric("Source tables", len(source_tables(results)))

    for r in results:
        for message in r.warnings:
            st.warning(f"{r.output}: {message}")

    tables = source_tables(results)
    picked = st.selectbox("Show only columns that come from table",
                          [""] + tables, key="lin_table",
                          format_func=lambda t: t or "(all tables)")
    st.dataframe(filter_frame(frame, picked), hide_index=True)
    st.download_button("Download lineage.csv", frame.to_csv(index=False),
                       file_name="lineage.csv", key="lin_download")

    st.subheader("Ask a question (optional)")
    st.caption("Uses Claude and needs ANTHROPIC_API_KEY. It sends only the "
               "table above (names and expressions), never your SQL text.")
    question = st.text_input(
        "Question", key="lin_question",
        placeholder="Which columns are derived from customers?")
    if st.button("Ask Claude", key="lin_ask_btn"):
        if not question.strip():
            st.warning("Type a question first.")
        else:
            from .llm_agent import ClaudeClient, LLMUnavailable
            try:
                answer = ask_about_lineage(question, results, ClaudeClient())
                st.session_state.lineage_answer = answer
            except LLMUnavailable as exc:
                st.session_state.pop("lineage_answer", None)
                st.warning(f"AI answer skipped: {exc}")
    if st.session_state.get("lineage_answer"):
        st.write(st.session_state.lineage_answer)
