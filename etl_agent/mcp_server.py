"""The ETL Agent MCP server.  Start it with:

    python -m etl_agent.mcp_server

This file is only a THIN layer.  It does three things:
  1. creates the server object (FastMCP),
  2. registers each function in mcp_tools.py as a "tool",
  3. runs it over stdio (the AI app starts this program and talks to it
     through its standard input/output).

How tools get described to the AI: the function NAME, the argument names
and types, and the DOCSTRING.  So the docstrings below are written for
the AI to read: they say when to use the tool and what comes back.

Important stdio rule: anything printed to normal output would corrupt
the conversation with the AI app.  So every call runs with print()
redirected to stderr (the log), which is safe.
"""
import contextlib
import sys

from mcp.server.fastmcp import FastMCP

from . import mcp_tools as tools

mcp = FastMCP(
    "etl-agent",
    instructions=(
        "Tools for testing ETL loads. Files must be inside the allowed "
        "folder. Databases are given only as env:NAME. Results contain "
        "names, counts and verdicts, never data rows."))


def _run(func, **kwargs):
    with contextlib.redirect_stdout(sys.stderr):
        return func(**kwargs)


# ---- lineage ---------------------------------------------------------------
@mcp.tool()
def trace_sql_lineage(sql: str, dialect: str = "ansi",
                      tables: str = "") -> dict:
    """Trace each output column of a SQL query back to its source tables
    and columns, and say how it was derived (direct copy, expression,
    constant). Use this to answer 'where does column X come from?'.

    sql: one SELECT, INSERT ... SELECT or CREATE TABLE ... AS SELECT.
    dialect: ansi, sqlite, postgres, oracle or mssql.
    tables: optional, one table per line like 'customers: cust_id, city'.
      Needed to expand SELECT * or to resolve unqualified columns in joins.
    Returns output columns with sources, steps and warnings. Do not pass
    queries containing real personal data in quoted values."""
    return _run(tools.trace_sql_lineage, sql=sql, dialect=dialect,
                tables=tables)


@mcp.tool()
def columns_from_table(sql: str, table: str, dialect: str = "ansi",
                       tables: str = "") -> dict:
    """List the output columns of a SQL query that depend on one source
    table. Use for 'which columns come from table T?'. Arguments are the
    same as trace_sql_lineage plus the table name."""
    return _run(tools.columns_from_table, sql=sql, table=table,
                dialect=dialect, tables=tables)


# ---- mapping and SQL generation ---------------------------------------------
@mcp.tool()
def show_mapping(mapping_file: str, source_table: str = "",
                 target_table: str = "") -> dict:
    """Validate an ETL mapping file (csv, xlsx or json) and summarise it:
    tables, primary key, and each target column with its source columns,
    transformation rule and data type. mapping_file is a file name inside
    the allowed folder. Table names are optional if the file has them."""
    return _run(tools.show_mapping, mapping_file=mapping_file,
                source_table=source_table, target_table=target_table)


@mcp.tool()
def generate_sql_package(mapping_file: str, dialect: str = "ansi",
                         source_table: str = "", target_table: str = "",
                         save: bool = True) -> dict:
    """Generate SQL from a mapping file: the expected SELECT, the CREATE
    TABLE for the target, and the list of SQL checks (each with name,
    description, SQL and what counts as PASS). dialect: ansi, sqlite,
    postgres, oracle or mssql. With save=true a checks .sql script is
    also written under the allowed folder (mcp_output) and its path is
    returned."""
    return _run(tools.generate_sql_package, mapping_file=mapping_file,
                dialect=dialect, source_table=source_table,
                target_table=target_table, save=save)


# ---- build target and checks ------------------------------------------------
@mcp.tool()
def build_expected_target(mapping_file: str, source_file: str = "",
                          source_db: str = "", source_table: str = "",
                          target_table: str = "",
                          dialect: str = "ansi") -> dict:
    """Build the EXPECTED target data: the source with every mapping rule
    applied. Give the source as a file (csv, xlsx, xml) inside the allowed
    folder, OR as source_db (env:NAME) which reads the mapping's source
    table. Saves expected_target.csv under mcp_output and returns only the
    row count, column names and the saved path (never the rows). Stops if
    the source looks like it holds sensitive data. Never changes a
    database."""
    return _run(tools.build_expected_target, mapping_file=mapping_file,
                source_file=source_file, source_db=source_db,
                source_table=source_table, target_table=target_table,
                dialect=dialect)


@mcp.tool()
def run_etl_checks(mapping_file: str, source_file: str = "",
                   target_file: str = "", source_db: str = "",
                   target_db: str = "", source_table: str = "",
                   target_table: str = "", dialect: str = "ansi",
                   run_sql: bool = False,
                   include_details: bool = False) -> dict:
    """Judge a LOADED target against the source and the mapping (row
    counts, missing columns, duplicate keys, nulls, data types, lengths,
    sums, transformations). Source and target are each a file inside the
    allowed folder OR a database given as env:NAME. run_sql=true also runs
    the generated SQL checks and needs target_db (source and target tables
    must be in that one database). Returns counts and one row per check
    (check, column, status, failing). include_details=true adds the detail
    text, which can contain example values from the data, so leave it
    false unless the user asks for it. Also saves an HTML report."""
    return _run(tools.run_etl_checks, mapping_file=mapping_file,
                source_file=source_file, target_file=target_file,
                source_db=source_db, target_db=target_db,
                source_table=source_table, target_table=target_table,
                dialect=dialect, run_sql=run_sql,
                include_details=include_details)


def main():
    mcp.run()          # default transport: stdio


if __name__ == "__main__":
    main()
