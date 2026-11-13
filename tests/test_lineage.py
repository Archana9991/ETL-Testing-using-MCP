"""Tests for column lineage.

These need the sqlglot package.  If it is missing the whole file is
skipped (not failed), so your other 151 tests are unaffected.
"""
import pytest

pytest.importorskip("sqlglot")

from etl_agent.lineage import (  # noqa: E402
    LineageError, SourceColumn, ask_about_lineage, columns_from_table,
    format_text, source_tables, sources_of, to_frame, trace_query)


def src(*pairs):
    """Shorthand: src(("customers", "city")) -> [SourceColumn(...)]"""
    return [SourceColumn(t, c) for t, c in pairs]


def by_name(results):
    return {r.output.lower(): r for r in results}


def test_direct_column_with_alias():
    r = trace_query("SELECT c.cust_id AS id FROM customers c")
    assert len(r) == 1
    assert r[0].output.lower() == "id"
    assert r[0].sources == src(("customers", "cust_id"))
    assert r[0].kind == "direct"


def test_derived_column_uses_several_source_columns():
    sql = ("SELECT UPPER(TRIM(c.first_name)) || ' ' || c.last_name "
           "AS full_name FROM customers c")
    r = trace_query(sql)[0]
    assert r.kind == "derived"
    assert set(r.sources) == set(src(("customers", "first_name"),
                                     ("customers", "last_name")))
    assert "UPPER" in r.steps[0].upper()


def test_join_tracks_each_column_to_its_own_table():
    sql = ("SELECT c.cust_id, b.branch_name "
           "FROM customers c JOIN branches b ON c.branch_code = b.code")
    r = by_name(trace_query(sql))
    assert r["cust_id"].sources == src(("customers", "cust_id"))
    assert r["branch_name"].sources == src(("branches", "branch_name"))


def test_cte_is_traced_through_to_the_real_table():
    sql = ("WITH x AS (SELECT cust_id, salary * 12 AS annual "
           "FROM customers) SELECT annual AS pay FROM x")
    r = trace_query(sql)[0]
    assert r.sources == src(("customers", "salary"))
    assert r.kind == "derived"
    assert any("12" in step for step in r.steps)


def test_subquery_in_from_is_traced():
    sql = ("SELECT t.total FROM (SELECT SUM(o.amount) AS total "
           "FROM orders o) t")
    r = trace_query(sql)[0]
    assert r.sources == src(("orders", "amount"))
    assert r.kind == "derived"


def test_union_merges_columns_by_position():
    sql = ("SELECT a.x AS v FROM a UNION ALL SELECT b.y AS v FROM b")
    r = trace_query(sql)
    assert len(r) == 1
    assert set(r[0].sources) == set(src(("a", "x"), ("b", "y")))


def test_star_is_expanded_when_schema_is_given():
    schema = {"customers": {"cust_id": "int", "city": "text"}}
    r = trace_query("SELECT * FROM customers", schema=schema)
    assert [x.output.lower() for x in r] == ["cust_id", "city"]
    assert r[1].sources == src(("customers", "city"))


def test_star_without_schema_gives_a_warning_not_a_wrong_answer():
    r = trace_query("SELECT * FROM customers")
    assert r[0].kind == "unknown"
    assert r[0].warnings


def test_constant_and_count_star():
    r = by_name(trace_query(
        "SELECT 'ACTIVE' AS status, COUNT(*) AS n FROM customers"))
    assert r["status"].kind == "constant"
    assert r["status"].sources == []
    assert r["n"].kind == "derived"
    assert r["n"].warnings


def test_insert_select_traces_the_select_part():
    sql = ("INSERT INTO target (id) SELECT c.cust_id FROM customers c")
    r = trace_query(sql)
    assert r[0].sources == src(("customers", "cust_id"))


def test_ambiguous_column_is_reported():
    sql = "SELECT city FROM customers c JOIN branches b ON c.a = b.a"
    r = trace_query(sql)[0]
    assert r.warnings          # we could not know which table 'city' is in


def test_schema_resolves_ambiguous_unqualified_column():
    schema = {"customers": {"cust_id": "int", "a": "int"},
              "branches": {"a": "int", "city": "text"}}
    sql = "SELECT city FROM customers c JOIN branches b ON c.a = b.a"
    r = trace_query(sql, schema=schema)[0]
    assert r.sources == src(("branches", "city"))


def test_bad_sql_gives_a_clear_error():
    with pytest.raises(LineageError):
        trace_query("SELEC FROM WHERE ((")


def test_non_select_gives_a_clear_error():
    with pytest.raises(LineageError):
        trace_query("DROP TABLE customers")


def test_two_statements_are_refused():
    with pytest.raises(LineageError):
        trace_query("SELECT 1 AS a; SELECT 2 AS b")


def test_unknown_dialect_is_refused():
    with pytest.raises(LineageError):
        trace_query("SELECT 1 AS a", dialect="klingon")


def test_sensitive_looking_literal_is_blocked():
    # All zeros: clearly fake, but it has the SHAPE of a 12-digit ID.
    with pytest.raises(LineageError, match="STOPPED"):
        trace_query("SELECT c.a FROM t c WHERE c.k = '000000000000'")


def lowered(sources):
    """Compare names ignoring case (Oracle reports unquoted names UPPER)."""
    return [(s.table.lower(), s.column.lower()) for s in sources]


def test_oracle_and_mssql_dialects_parse():
    r = trace_query("SELECT NVL(c.city, 'NA') AS city FROM customers c",
                    dialect="oracle")
    assert lowered(r[0].sources) == [("customers", "city")]
    r = trace_query("SELECT ISNULL(c.city, 'NA') AS city FROM customers c",
                    dialect="mssql")
    assert lowered(r[0].sources) == [("customers", "city")]


def test_oracle_reports_unquoted_names_in_uppercase():
    # Oracle folds unquoted names to UPPERCASE; the lineage keeps that.
    r = trace_query("SELECT c.city FROM customers c", dialect="oracle")
    assert r[0].sources == src(("CUSTOMERS", "CITY"))


def test_helper_functions():
    sql = ("SELECT c.cust_id, b.branch_name "
           "FROM customers c JOIN branches b ON c.branch_code = b.code")
    r = trace_query(sql)
    assert sources_of(r, "BRANCH_NAME") == src(("branches", "branch_name"))
    assert [n.lower() for n in columns_from_table(r, "customers")] == \
        ["cust_id"]
    assert set(source_tables(r)) == {"customers", "branches"}
    assert list(to_frame(r)["source_table"]) == ["customers", "branches"]
    assert "cust_id" in format_text(r)
    with pytest.raises(LineageError):
        sources_of(r, "nope")


class FakeLLM:
    def __init__(self, reply="ok"):
        self.reply, self.prompts = reply, []

    def complete(self, system, user, max_tokens=1500):
        self.prompts.append(user)
        return self.reply


def test_llm_sees_lineage_table_but_not_the_sql_text():
    sql = "SELECT c.cust_id AS id FROM customers c WHERE c.city = 'Pune'"
    r = trace_query(sql)
    llm = FakeLLM("id comes from customers.cust_id")
    answer = ask_about_lineage("where does id come from?", r, llm)
    assert answer == "id comes from customers.cust_id"
    assert "cust_id" in llm.prompts[0]
    assert "Pune" not in llm.prompts[0]      # the WHERE literal never leaves
