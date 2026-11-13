"""Tests for the MCP tool logic (mcp_tools.py) and the real server.

Parts 1-2 call the tool functions directly: no MCP client needed.
Part 3 starts the real server and talks to it like an AI app would; it
skips itself if the `mcp` package is not installed.
"""
import asyncio
import shutil
import sqlite3
import sys
from importlib.util import find_spec
from pathlib import Path

import pandas as pd
import pytest

from etl_agent import mcp_tools as t
from etl_agent.mcp_tools import ToolInputError

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "sample_data"
TABLES = {"source_table": "CUSTOMERS_RAW", "target_table": "DIM_CUSTOMER"}

needs_sqlglot = pytest.mark.skipif(not find_spec("sqlglot"),
                                   reason="needs sqlglot")


@pytest.fixture
def fence(tmp_path, monkeypatch):
    """A temporary allowed folder holding copies of the sample files."""
    monkeypatch.setenv("ETL_AGENT_MCP_DIR", str(tmp_path))
    for name in ("customer_mapping.csv", "customers_raw.csv"):
        shutil.copy(SAMPLE / name, tmp_path / name)
    return tmp_path


# ---------------------------------------------------------------------------
# Part 1: the safety rules
# ---------------------------------------------------------------------------
def test_files_outside_the_fence_are_refused(fence, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "x.csv"
    outside.write_text("A\n1\n")
    with pytest.raises(ToolInputError, match="inside the allowed folder"):
        t.safe_path(str(outside), t.DATA_SUFFIXES, "source")


def test_dot_dot_cannot_escape_the_fence(fence):
    sub = fence / "sub"
    sub.mkdir()
    (fence.parent / "secret.csv").write_text("A\n1\n")
    with pytest.raises(ToolInputError, match="inside the allowed folder"):
        t.safe_path("sub/../../secret.csv", t.DATA_SUFFIXES, "source")


def test_wrong_file_type_and_missing_file_are_refused(fence):
    (fence / "notes.exe").write_text("x")
    with pytest.raises(ToolInputError, match="must be one of"):
        t.safe_path("notes.exe", t.DATA_SUFFIXES, "source")
    with pytest.raises(ToolInputError, match="not found"):
        t.safe_path("nope.csv", t.DATA_SUFFIXES, "source")
    with pytest.raises(ToolInputError, match="Give the"):
        t.safe_path("  ", t.DATA_SUFFIXES, "source")


def test_database_must_be_an_env_reference(fence):
    assert t.db_ref("", "source_db") is None
    with pytest.raises(ToolInputError, match="env:NAME"):
        t.db_ref("sqlite:///x.db", "source_db")
    with pytest.raises(ToolInputError, match="env:NAME"):
        t.db_ref("postgresql://user:secret@host/db", "target_db")


def test_missing_env_variable_gives_a_friendly_error(fence, monkeypatch):
    monkeypatch.delenv("ETL_NO_SUCH_DB", raising=False)
    with pytest.raises(ToolInputError, match="source_db"):
        t.db_ref("env:ETL_NO_SUCH_DB", "source_db")


def test_sensitive_source_is_blocked_and_cannot_be_allowed(fence):
    risky = pd.read_csv(fence / "customers_raw.csv", dtype=str,
                        keep_default_na=False)
    risky["AADHAAR_NO"] = "x"
    risky.to_csv(fence / "risky.csv", index=False)
    with pytest.raises(ToolInputError, match="(?i)sensitive"):
        t.build_expected_target("customer_mapping.csv", "risky.csv",
                                **TABLES)


# ---------------------------------------------------------------------------
# Part 2: the tools
# ---------------------------------------------------------------------------
def test_show_mapping_summarises_the_mapping(fence):
    r = t.show_mapping("customer_mapping.csv", **TABLES)
    assert r["source_table"] == "CUSTOMERS_RAW"
    assert r["target_table"] == "DIM_CUSTOMER"
    assert len(r["columns"]) == 8
    assert {"target_column", "transformation"} <= set(r["columns"][0])


def test_bad_mapping_gives_a_clear_error(fence):
    (fence / "bad.csv").write_text("TARGET_COLUMN,TRANSFORMATION\nA,MAGIC\n")
    with pytest.raises(ToolInputError, match="MAGIC"):
        t.show_mapping("bad.csv", source_table="S", target_table="T")


@pytest.mark.parametrize("dialect", ["ansi", "sqlite", "postgres",
                                     "oracle", "mssql"])
def test_generate_sql_package_for_every_dialect(fence, dialect):
    r = t.generate_sql_package("customer_mapping.csv", dialect, **TABLES)
    assert r["checks"] and r["expected_select"] and r["create_table"]
    assert Path(r["saved_to"]).is_file()
    assert Path(r["saved_to"]).parent == fence / "mcp_output"


def test_generate_sql_package_oracle_wording_and_no_save(fence):
    r = t.generate_sql_package("customer_mapping.csv", "oracle",
                               save=False, **TABLES)
    assert "VARCHAR2" in r["create_table"]
    assert r["saved_to"] is None
    assert not (fence / "mcp_output").exists()


def test_unknown_dialect_is_refused(fence):
    with pytest.raises(ToolInputError, match="dialect"):
        t.generate_sql_package("customer_mapping.csv", "klingon", **TABLES)


def test_build_expected_target_returns_no_rows(fence):
    r = t.build_expected_target("customer_mapping.csv",
                                "customers_raw.csv", **TABLES)
    assert r["rows"] == 4
    assert Path(r["saved_to"]).is_file()
    raw = pd.read_csv(fence / "customers_raw.csv", dtype=str,
                      keep_default_na=False)
    for value in raw["FIRST_NAME"]:                # names must not leak back
        assert value not in str(r)


def test_build_from_a_database_given_as_env_reference(fence, monkeypatch):
    db = fence / "src.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE CUSTOMERS_RAW (CUST_ID INTEGER, FIRST_NAME "
                 "TEXT, LAST_NAME TEXT, GENDER TEXT, CITY TEXT, SALARY "
                 "REAL, STATE TEXT, BRANCH_CODE TEXT)")
    conn.execute("INSERT INTO CUSTOMERS_RAW VALUES "
                 "(1,'Asha','Rao','F','Pune',10.5,'MH','MH01')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("ETL_TEST_DB", f"sqlite:///{db.as_posix()}")
    r = t.build_expected_target("customer_mapping.csv",
                                source_db="env:ETL_TEST_DB", **TABLES)
    assert r["rows"] == 1


def test_file_and_database_together_is_refused(fence):
    with pytest.raises(ToolInputError, match="OR"):
        t.build_expected_target("customer_mapping.csv", "customers_raw.csv",
                                source_db="env:X", **TABLES)


def test_checks_pass_then_catch_a_planted_bug(fence):
    built = t.build_expected_target("customer_mapping.csv",
                                    "customers_raw.csv", **TABLES)
    good = t.run_etl_checks("customer_mapping.csv", "customers_raw.csv",
                            built["saved_to"], **TABLES)
    assert good["all_passed"] is True and good["failed"] == 0
    assert Path(good["report_html"]).is_file()

    bad = pd.read_csv(built["saved_to"], dtype=str, keep_default_na=False)
    bad.loc[0, "CITY"] = "Pune"
    bad.to_csv(fence / "bad_target.csv", index=False)
    r = t.run_etl_checks("customer_mapping.csv", "customers_raw.csv",
                         "bad_target.csv", **TABLES)
    assert r["all_passed"] is False and r["failed"] >= 1


def test_check_details_are_hidden_unless_asked_for(fence):
    built = t.build_expected_target("customer_mapping.csv",
                                    "customers_raw.csv", **TABLES)
    plain = t.run_etl_checks("customer_mapping.csv", "customers_raw.csv",
                             built["saved_to"], **TABLES)
    assert plain["details_included"] is False
    assert all("detail" not in row for row in plain["checks"])
    more = t.run_etl_checks("customer_mapping.csv", "customers_raw.csv",
                            built["saved_to"], include_details=True,
                            **TABLES)
    assert more["details_included"] is True
    assert all("detail" in row for row in more["checks"])


def test_run_sql_without_a_target_database_is_refused(fence):
    with pytest.raises(ToolInputError, match="target_db"):
        t.run_etl_checks("customer_mapping.csv", "customers_raw.csv",
                         "customers_raw.csv", run_sql=True, **TABLES)


@needs_sqlglot
def test_lineage_tool_output_shape(fence):
    sql = ("SELECT c.cust_id AS id, UPPER(c.first_name) AS n "
           "FROM customers c")
    r = t.trace_sql_lineage(sql)
    by = {c["output"].lower(): c for c in r["output_columns"]}
    assert by["id"]["sources"] == ["customers.cust_id"]
    assert by["id"]["kind"] == "direct"
    assert by["n"]["kind"] == "derived"
    assert r["source_tables"] == ["customers"]


@needs_sqlglot
def test_lineage_columns_from_table_and_errors(fence):
    sql = ("SELECT c.cust_id, b.branch_name FROM customers c "
           "JOIN branches b ON c.branch_code = b.code")
    r = t.columns_from_table(sql, "branches")
    assert [c.lower() for c in r["output_columns"]] == ["branch_name"]
    with pytest.raises(ToolInputError, match="Could not read"):
        t.trace_sql_lineage("SELEC FROM WHERE ((")
    with pytest.raises(ToolInputError, match="Line 1"):
        t.trace_sql_lineage("SELECT 1 AS a", tables="customers cust_id")


# ---------------------------------------------------------------------------
# Part 3: the real server, talked to like an AI app would
# ---------------------------------------------------------------------------
EXPECTED_TOOLS = {"trace_sql_lineage", "columns_from_table", "show_mapping",
                  "generate_sql_package", "build_expected_target",
                  "run_etl_checks"}


async def _talk_to_server(folder):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    # Make sure the new Python process can find the etl_agent package.
    src = ROOT / "src"
    paths = [str(p) for p in (src, ROOT) if p.is_dir()]
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "etl_agent.mcp_server"],
        env={"ETL_AGENT_MCP_DIR": str(folder),
             "PYTHONPATH": ";".join(paths) if sys.platform == "win32"
             else ":".join(paths)})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            shown = await session.call_tool(
                "show_mapping", {"mapping_file": "customer_mapping.csv",
                                 **TABLES})
            refused = await session.call_tool(
                "show_mapping", {"mapping_file": "..\\..\\x.csv"})
            return listed.tools, shown, refused


@pytest.mark.skipif(not find_spec("mcp"), reason="needs the mcp package")
def test_real_server_lists_tools_and_answers_calls(fence):
    tools, shown, refused = asyncio.run(
        asyncio.wait_for(_talk_to_server(fence), timeout=60))
    assert EXPECTED_TOOLS <= {tool.name for tool in tools}
    assert all(tool.description for tool in tools)      # the AI reads these
    assert not shown.isError
    assert "DIM_CUSTOMER" in str(shown.content)
    assert refused.isError                               # fence works too
