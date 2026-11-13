"""ETL Agent UI (Streamlit).  Run from the project folder:

    pip install -r requirements-ui.txt
    streamlit run app.py

The UI is a THIN layer: every button calls the same library functions the
command line uses (automation.py), so there is no second copy of the logic.

Streamlit in four lines (read these first):
  * The whole script re-runs from top to bottom on every click.
  * st.session_state is memory that survives those re-runs.
  * A widget with key="x" stores its value in st.session_state["x"].
  * A button is True only for the one re-run right after the click, so
    results must be saved in session_state to stay on screen.
"""
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
for base in (ROOT / "src", ROOT):              # find the etl_agent package
    if (base / "etl_agent").is_dir():
        sys.path.insert(0, str(base))
        break

from etl_agent.automation import (build_target, load_dataset,  # noqa: E402
                                  run_checks, stop_if_sensitive)
from etl_agent.etl_checks import checks_passed  # noqa: E402
from etl_agent.mapping import TRANSFORMATIONS, load_mapping  # noqa: E402
from etl_agent.orchestrator import resolve_db_url  # noqa: E402
from etl_agent.sqlgen import (DIALECTS, create_table_sql,  # noqa: E402
                              generate_sql_checks, generate_sql_script,
                              get_dialect, select_expected_sql)

SAMPLE = ROOT / "sample_data"
PASS_TEXT = {"no_rows": "the query returns no rows",
             "zero": "the number returned is 0",
             "equal": "the two numbers returned are equal"}
ROW_COLOUR = {"PASS": "#e6f4ea", "FAIL": "#fce8e6",
              "ERROR": "#fce8e6", "SKIP": "#eeeeee"}


# ---- small helpers ---------------------------------------------------------
def workdir() -> Path:
    """One temporary folder per browser session, for uploads and outputs."""
    if "workdir" not in st.session_state:
        st.session_state.workdir = tempfile.mkdtemp(prefix="etl_agent_ui_")
    return Path(st.session_state.workdir)


def resolve_file(upload, typed_path):
    """An uploaded file wins; otherwise use the path the user typed."""
    if upload is not None:
        path = workdir() / Path(upload.name).name
        path.write_bytes(upload.getvalue())
        return str(path)
    return (typed_path or "").strip() or None


def load_sample():
    """Button callback: fill the form with the bundled sample data.
    Callbacks run BEFORE the next re-run, so setting widget keys is safe."""
    sample_source = str(SAMPLE / "customers_raw.csv")
    st.session_state.update(
        mapping_path=str(SAMPLE / "customer_mapping.csv"),
        source_table="CUSTOMERS_RAW", target_table="DIM_CUSTOMER",
        bld_source_path=sample_source, chk_source_path=sample_source)


def use_built_as_target():
    st.session_state.chk_target_mode = "File"
    st.session_state.chk_target_path = st.session_state.built["path"]


def dataset_input(role, mapping, prefix):
    """Source/target picker: a file (upload or path) OR a database table.
    `prefix` keeps widget keys unique when two tabs ask for the same thing."""
    mode = st.radio(f"{role.title()} comes from", ["File", "Database table"],
                    horizontal=True, key=f"{prefix}_{role}_mode")
    if mode == "File":
        up = st.file_uploader(f"{role.title()} file", type=["csv", "xlsx", "xml"],
                              key=f"{prefix}_{role}_up")
        path = st.text_input("...or type a file path", key=f"{prefix}_{role}_path")
        return resolve_file(up, path), None
    url = st.text_input(f"{role.title()} database URL (or env:NAME)",
                        type="password", key=f"{prefix}_{role}_url",
                        help="Passwords are hidden. env:NAME reads the URL "
                             "from an environment variable.")
    st.caption(f"Reads table {getattr(mapping, role + '_table')}")
    return None, (url or None)


def fetch(role, mapping, file, url):
    """Read a dataset, showing a friendly error instead of a traceback."""
    if not file and not url:
        st.error(f"Give a {role} file or a database URL first.")
        return None
    try:
        return load_dataset(file, resolve_db_url(url),
                            getattr(mapping, f"{role}_table"))
    except Exception as exc:
        st.error(f"Could not read the {role} data.")
        st.code(str(exc)[:400])
        return None


def blocked_by_guard(df) -> bool:
    """The Phase 3 sensitive-data guard, reused as is."""
    lines = []
    allow = st.session_state.get("allow_sensitive", False)
    if stop_if_sensitive(df, allow, log=lines.append):
        st.error(lines[0])
        st.code("\n".join(lines[1:]))
        return True
    return False


def colour_row(row):
    colour = ROW_COLOUR.get(row["status"], "")
    return [f"background-color: {colour}; color: black"] * len(row)


# ---- page ------------------------------------------------------------------
st.set_page_config(page_title="ETL Agent", layout="wide")
st.title("ETL Agent: ETL automation")
st.caption("Generate SQL from a mapping, build the target, and check a loaded ETL.")

with st.sidebar:
    st.header("Mapping")
    st.button("Load sample data", on_click=load_sample, key="sample_btn",
              disabled=not SAMPLE.exists())
    mapping_up = st.file_uploader("Mapping file", type=["csv", "xlsx", "json"],
                                  key="mapping_up")
    st.text_input("...or type a path to a mapping file", key="mapping_path")
    st.text_input("Source table", key="source_table")
    st.text_input("Target table", key="target_table")
    st.selectbox("SQL dialect", list(DIALECTS), key="dialect")
    st.checkbox("Allow sensitive-looking columns", key="allow_sensitive",
                help="Switches off the sensitive-data guard. Prefer a masked "
                     "or synthetic extract.")

mapping_file = resolve_file(mapping_up, st.session_state.get("mapping_path"))
if not mapping_file:
    st.info("Upload a mapping file in the sidebar, or press 'Load sample data'.")
    st.stop()
try:
    mapping = load_mapping(mapping_file,
                           st.session_state.get("source_table") or None,
                           st.session_state.get("target_table") or None)
except (ValueError, OSError) as exc:
    st.error("The mapping has problems. Fix them and reload.")
    st.code(str(exc))
    st.stop()

dialect = get_dialect(st.session_state.dialect)
tab_map, tab_sql, tab_build, tab_check = st.tabs(
    ["Mapping", "SQL generator", "Build target", "ETL checks"])

# ---- tab 1: the mapping -----------------------------------------------------
with tab_map:
    st.success(f"Mapping is valid: {mapping.source_table} -> {mapping.target_table}, "
               f"{len(mapping.columns)} columns, primary key "
               f"{mapping.primary_key or 'none'}.")
    rows = pd.DataFrame([asdict(c) for c in mapping.columns])
    rows["source_columns"] = rows["source_columns"].apply(" | ".join)
    st.dataframe(rows, hide_index=True)
    with st.expander("What each transformation rule does"):
        st.table(pd.DataFrame({"rule": list(TRANSFORMATIONS),
                               "what it does": list(TRANSFORMATIONS.values())}))

# ---- tab 2: SQL generator ---------------------------------------------------
with tab_sql:
    checks = generate_sql_checks(mapping, dialect)
    st.subheader("Expected SELECT")
    st.code(select_expected_sql(mapping, dialect), language="sql")
    st.subheader("Create the target table")
    st.code(create_table_sql(mapping, dialect), language="sql")
    st.subheader(f"ETL checks ({len(checks)})")
    for c in checks:
        with st.expander(f"{c.name}: {c.description}"):
            st.code(c.sql, language="sql")
            st.caption(f"PASS when {PASS_TEXT[c.expect]}.")
    st.download_button("Download checks.sql",
                       generate_sql_script(mapping, dialect, checks),
                       file_name="checks.sql", key="sql_download")

# ---- tab 3: build target ----------------------------------------------------
with tab_build:
    st.write("Build the **expected** target: the source with every mapping rule applied.")
    file, url = dataset_input("source", mapping, "bld")
    load_db = st.checkbox(f"Also create (or REPLACE) table {mapping.target_table} "
                          "in a database", key="bld_load_db")
    db_url = None
    if load_db:
        st.warning(f"If {mapping.target_table} already exists it will be dropped "
                   "and recreated.")
        db_url = st.text_input("Target database URL (or env:NAME)", type="password",
                               key="bld_db_url")
    if st.button("Build expected target", key="build_btn", type="primary"):
        df = fetch("source", mapping, file, url)
        if load_db and not db_url:
            st.error("Enter the target database URL, or untick the database option.")
        elif df is not None and not blocked_by_guard(df):
            try:
                expected, path, loaded = build_target(
                    mapping, df, workdir() / "build",
                    resolve_db_url(db_url) if load_db else None,
                    st.session_state.dialect)
                st.session_state.built = {"df": expected, "path": str(path),
                                          "loaded": loaded if load_db else None}
            except Exception as exc:
                st.error("Could not build the target.")
                st.code(str(exc)[:600])
    built = st.session_state.get("built")
    if built:
        st.success(f"Expected target: {len(built['df'])} rows.")
        if built["loaded"] is not None:
            st.success(f"Table {mapping.target_table} created and loaded "
                       f"with {built['loaded']} rows.")
        st.dataframe(built["df"], hide_index=True)
        st.download_button("Download expected_target.csv",
                           built["df"].to_csv(index=False),
                           file_name="expected_target.csv", key="target_download")
        st.button("Use this file as the target in ETL checks",
                  on_click=use_built_as_target, key="use_built_btn")

# ---- tab 4: ETL checks ------------------------------------------------------
with tab_check:
    st.write("Judge a **loaded** target against the source and the mapping.")
    left, right = st.columns(2)
    with left:
        s_file, s_url = dataset_input("source", mapping, "chk")
    with right:
        t_file, t_url = dataset_input("target", mapping, "chk")
    run_sql = st.checkbox("Also run the generated SQL checks", key="chk_run_sql",
                          help="Needs a target database URL. The source and target "
                               "tables must both live in that database.")
    if st.button("Run ETL checks", key="checks_btn", type="primary"):
        if run_sql and not t_url:
            st.error("Running the SQL checks needs a target database URL.")
        else:
            source = fetch("source", mapping, s_file, s_url)
            target = fetch("target", mapping, t_file, t_url) if source is not None else None
            if source is not None and target is not None and not blocked_by_guard(source):
                try:
                    results, report = run_checks(
                        mapping, source, target, workdir() / "checks",
                        sql_url=resolve_db_url(t_url) if run_sql else None,
                        dialect=st.session_state.dialect)
                    st.session_state.checks = {
                        "results": results,
                        "html": Path(report).read_text(encoding="utf-8")}
                except Exception as exc:
                    st.error("Could not run the checks.")
                    st.code(str(exc)[:600])
    outcome = st.session_state.get("checks")
    if outcome:
        res = outcome["results"]
        n_pass = int((res["status"] == "PASS").sum())
        n_bad = int(res["status"].isin(["FAIL", "ERROR"]).sum())
        n_skip = int((res["status"] == "SKIP").sum())
        a, b, c = st.columns(3)
        a.metric("Passed", n_pass)
        b.metric("Failed", n_bad)
        c.metric("Skipped", n_skip)
        if checks_passed(res):
            st.success("All checks passed.")
        else:
            st.error(f"{n_bad} check(s) failed. Details below.")
        show = st.radio("Show", ["All", "Failed only"], horizontal=True, key="chk_filter")
        view = res if show == "All" else res[res["status"].isin(["FAIL", "ERROR"])]
        st.dataframe(view.style.apply(colour_row, axis=1), hide_index=True)
        st.download_button("Download results.csv", res.to_csv(index=False),
                           file_name="etl_checks.csv", key="results_download")
        st.download_button("Download HTML report", outcome["html"],
                           file_name="etl_checks.html", key="report_download")