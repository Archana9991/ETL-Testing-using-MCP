"""Tests for the SQL lineage tab.

Part 1 tests the plain helpers (no Streamlit, no sqlglot needed).
Part 2 drives the real app with Streamlit's AppTest, like test_ui.py.
Part 2 skips itself if streamlit or sqlglot is missing.
"""
from importlib.util import find_spec
from pathlib import Path

import pandas as pd
import pytest

from etl_agent.lineage_ui import (DIALECT_NAMES, decode_sql, filter_frame,
                                  parse_schema_text)

# ---------------------------------------------------------------------------
# Part 1: plain helpers
# ---------------------------------------------------------------------------


def test_schema_text_becomes_a_dict():
    text = "customers: cust_id, first_name\n\n# note\nbranches: code branch_name"
    assert parse_schema_text(text) == {
        "customers": {"cust_id": "text", "first_name": "text"},
        "branches": {"code": "text", "branch_name": "text"}}


def test_schema_text_empty_gives_empty_dict():
    assert parse_schema_text("") == {}
    assert parse_schema_text(None) == {}


def test_schema_text_merges_a_table_written_twice():
    r = parse_schema_text("t: a\nt: b")
    assert list(r["t"]) == ["a", "b"]


@pytest.mark.parametrize("bad, fragment", [
    ("customers cust_id", "Line 1"),             # no colon
    ("ok: a\nmy table: a", "Line 2"),             # space in table name
    ("db.customers: a", "plain table name"),      # schema prefix
    ("customers:", "no columns"),
    ("customers: a, b-c", "plain column name"),
])
def test_schema_text_errors_name_the_problem(bad, fragment):
    with pytest.raises(ValueError, match=fragment):
        parse_schema_text(bad)


def test_decode_sql_handles_bom_and_windows_text():
    assert decode_sql(b"\xef\xbb\xbfSELECT 1") == "SELECT 1"
    assert decode_sql("SELECT 'caf\u00e9'".encode("cp1252")) == "SELECT 'caf\u00e9'"


def _frame():
    return pd.DataFrame({
        "output_column": ["a", "a", "b"],
        "source_table": ["customers", "branches", "branches"],
        "source_column": ["x", "y", "z"]})


def test_filter_frame_keeps_all_rows_of_matching_outputs():
    got = filter_frame(_frame(), "CUSTOMERS")
    assert list(got["output_column"]) == ["a", "a"]      # both sources of a
    assert len(filter_frame(_frame(), "")) == 3          # no filter
    assert len(filter_frame(_frame(), "db.branches")) == 3


def test_dialect_names_match_the_lineage_engine():
    pytest.importorskip("sqlglot")
    from etl_agent.lineage import DIALECTS
    assert DIALECT_NAMES == list(DIALECTS)


# ---------------------------------------------------------------------------
# Part 2: the real app
# ---------------------------------------------------------------------------
needs_ui = pytest.mark.skipif(
    not (find_spec("streamlit") and find_spec("sqlglot")),
    reason="needs streamlit and sqlglot installed")

ROOT = Path(__file__).resolve().parent.parent
APP = str(ROOT / "app.py")
SAMPLE = ROOT / "sample_data"
QUERY = "SELECT c.cust_id AS id FROM customers c"


def fresh():
    from streamlit.testing.v1 import AppTest
    return AppTest.from_file(APP, default_timeout=30)


def with_sample(at):
    at.session_state["mapping_path"] = str(SAMPLE / "customer_mapping.csv")
    at.session_state["source_table"] = "CUSTOMERS_RAW"
    at.session_state["target_table"] = "DIM_CUSTOMER"
    return at


def text(elements):
    return " ".join(str(e.value) for e in elements)


def trace(at, sql, schema=""):
    at.text_area(key="lin_sql").set_value(sql)
    at.text_area(key="lin_schema").set_value(schema)
    at.button(key="lin_go").click().run()
    return at


@needs_ui
def test_lineage_tab_works_before_any_mapping_is_loaded():
    at = fresh().run()
    assert not at.exception
    trace(at, QUERY)
    assert not at.exception
    from etl_agent.lineage import SourceColumn
    result = at.session_state["lineage"]["results"][0]
    assert result.sources == [SourceColumn("customers", "cust_id")]


@needs_ui
def test_lineage_tab_works_next_to_a_loaded_mapping():
    at = with_sample(fresh()).run()
    assert not at.exception
    trace(at, QUERY)
    assert not at.exception
    assert len(at.session_state["lineage"]["results"]) == 1


@needs_ui
def test_existing_tabs_still_draw_with_the_new_tab():
    at = with_sample(fresh()).run()
    assert "Mapping is valid" in text(at.success)


@needs_ui
def test_empty_sql_asks_for_input():
    at = fresh().run()
    at.button(key="lin_go").click().run()
    assert "Paste some SQL" in text(at.warning)
    assert "lineage" not in at.session_state


@needs_ui
def test_bad_sql_shows_a_friendly_error():
    at = trace(fresh().run(), "SELEC FROM WHERE ((")
    assert not at.exception
    assert "Could not read" in text(at.error)


@needs_ui
def test_schema_mistake_names_the_line():
    at = trace(fresh().run(), QUERY, schema="customers cust_id")
    assert not at.exception
    assert "Line 1" in text(at.error)


@needs_ui
def test_star_is_expanded_with_the_schema_box():
    at = trace(fresh().run(), "SELECT * FROM customers",
               schema="customers: cust_id, city")
    assert not at.exception
    assert len(at.session_state["lineage"]["results"]) == 2


@needs_ui
def test_sensitive_looking_value_is_blocked():
    at = trace(fresh().run(),
               "SELECT c.a FROM t c WHERE c.k = '000000000000'")
    assert not at.exception
    assert "STOPPED" in text(at.error)
    assert "lineage" not in at.session_state


@needs_ui
def test_ask_without_api_key_is_skipped_politely(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    at = trace(fresh().run(), QUERY)
    at.text_input(key="lin_question").set_value("where does id come from?")
    at.button(key="lin_ask_btn").click().run()
    assert not at.exception
    assert "ANTHROPIC_API_KEY" in text(at.warning)


@needs_ui
def test_ask_sends_the_lineage_table_but_not_the_sql(monkeypatch):
    prompts = []

    class FakeClient:
        def complete(self, system, user, max_tokens=1500):
            prompts.append(user)
            return "id comes from customers.cust_id"

    monkeypatch.setattr("etl_agent.llm_agent.ClaudeClient", FakeClient)
    at = trace(fresh().run(),
               QUERY + " WHERE c.city = 'Pune'")
    at.text_input(key="lin_question").set_value("where does id come from?")
    at.button(key="lin_ask_btn").click().run()
    assert not at.exception
    assert at.session_state["lineage_answer"] == "id comes from customers.cust_id"
    assert "cust_id" in prompts[0]
    assert "Pune" not in prompts[0]
