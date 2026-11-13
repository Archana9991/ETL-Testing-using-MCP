"""The logic behind the MCP tools.  No `mcp` import here on purpose.

Same rule as ui_helpers.py and lineage_ui.py: the thin outer layer
(mcp_server.py) only registers these functions as tools, so everything
here is plain Python that pytest can test without any MCP client.

Safety rules (they matter MORE here than in the UI, because a tool's
result is read by an AI app, and the AI is driven by whatever is typed
in a chat):

  1. FOLDER FENCE : files may only be read from / written under ONE
     folder, set with the environment variable ETL_AGENT_MCP_DIR
     (default: the folder the server was started in).
  2. NO PASSWORDS IN CHAT : databases are given as  env:NAME  only.  The
     real URL stays in an environment variable on your PC.
  3. NO ROWS BACK : tools return names, counts and verdicts, never data
     rows.  Check details (which can hold example values) are opt-in.
  4. GUARD STAYS ON : the sensitive-data guard cannot be switched off
     from a tool.
  5. NO DESTRUCTIVE ACTIONS : no tool drops or replaces database tables.
"""
import os
import re
from dataclasses import asdict
from pathlib import Path

from .automation import (build_target, load_dataset, run_checks,
                         stop_if_sensitive)
from .etl_checks import checks_passed
from .mapping import load_mapping
from .orchestrator import resolve_db_url
from .sqlgen import (create_table_sql, generate_sql_checks,
                     generate_sql_script, get_dialect, select_expected_sql)

DATA_SUFFIXES = {".csv", ".xlsx", ".xml"}
MAPPING_SUFFIXES = {".csv", ".xlsx", ".json"}


class ToolInputError(Exception):
    """A clear, human-readable problem with what the caller asked for."""


# ---------------------------------------------------------------------------
# Safety helpers
# ---------------------------------------------------------------------------
def allowed_root():
    """The one folder tools may read from and write under."""
    return Path(os.environ.get("ETL_AGENT_MCP_DIR") or Path.cwd()).resolve()


def output_dir():
    out = allowed_root() / "mcp_output"
    out.mkdir(parents=True, exist_ok=True)
    return out


def safe_path(text, suffixes, what):
    """Turn a file name into a full path, only if it is inside the fence.

    .resolve() follows '..' and shortcuts first, so 'sub\\..\\..\\x.csv'
    cannot sneak out of the folder.
    """
    if not text or not str(text).strip():
        raise ToolInputError(f"Give the {what} file name.")
    root = allowed_root()
    path = Path(str(text).strip().strip('"').strip("'"))
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.is_relative_to(root):
        raise ToolInputError(
            f"The {what} file must be inside the allowed folder: {root}")
    if path.suffix.lower() not in suffixes:
        raise ToolInputError(
            f"The {what} file must be one of: {', '.join(sorted(suffixes))}")
    if not path.is_file():
        raise ToolInputError(f"{what} file not found: {path.name} "
                             f"(looked in {root})")
    return str(path)


def db_ref(text, what):
    """Accept only  env:NAME  and return the real URL (or None if empty)."""
    value = (text or "").strip()
    if not value:
        return None
    if not (value.startswith("env:") and len(value) > 4):
        raise ToolInputError(
            f"{what}: give the database as env:NAME (the name of an "
            f"environment variable that holds the URL). Never put a "
            f"database URL or password in a chat.")
    try:
        return resolve_db_url(value)
    except Exception as exc:
        raise ToolInputError(f"{what}: could not use {value}: "
                             f"{str(exc)[:200]}") from exc


def _guard(df):
    """The sensitive-data guard.  Cannot be switched off from a tool."""
    lines = []
    if stop_if_sensitive(df, False, log=lines.append):
        raise ToolInputError("\n".join(lines))


def _load_mapping(mapping_file, source_table, target_table):
    path = safe_path(mapping_file, MAPPING_SUFFIXES, "mapping")
    try:
        return load_mapping(path, source_table or None, target_table or None)
    except (ValueError, OSError) as exc:
        raise ToolInputError(f"The mapping has problems:\n{exc}") from exc


def _dataset(role, mapping, file, db):
    """Read a source/target from a file OR a database table, then guard."""
    if file and db:
        raise ToolInputError(f"Give the {role} as a file OR a database, "
                             f"not both.")
    path = safe_path(file, DATA_SUFFIXES, role) if file else None
    url = db_ref(db, f"{role}_db")
    if not path and not url:
        raise ToolInputError(f"Give the {role} file name or {role}_db "
                             f"(env:NAME).")
    try:
        return load_dataset(path, url, getattr(mapping, f"{role}_table"))
    except Exception as exc:
        raise ToolInputError(f"Could not read the {role} data: "
                             f"{str(exc)[:300]}") from exc


def _dialect(name):
    try:
        return get_dialect(name)
    except (ValueError, KeyError) as exc:
        raise ToolInputError(f"Unknown dialect {name!r}: {exc}") from exc


def _safe_stem(text):
    return re.sub(r"[^A-Za-z0-9_\-]+", "_", text or "").strip("_") or "x"


# ---------------------------------------------------------------------------
# Lineage tools
# ---------------------------------------------------------------------------
def _lineage_module():
    """sqlglot is imported only when a lineage tool is used, so the other
    tools still work if sqlglot is not installed."""
    try:
        from . import lineage
    except ImportError as exc:
        raise ToolInputError("SQL lineage needs the sqlglot package: "
                             "pip install sqlglot") from exc
    return lineage


def _trace(sql, dialect, tables):
    lineage = _lineage_module()
    from .lineage_ui import parse_schema_text
    try:
        schema = parse_schema_text(tables)
        return lineage, lineage.trace_query(sql, schema or None, dialect)
    except (ValueError, lineage.LineageError) as exc:
        raise ToolInputError(str(exc)) from exc


def _column_info(r):
    return {"output": r.output, "kind": r.kind,
            "sources": [str(s) for s in r.sources],
            "steps": list(r.steps), "warnings": list(r.warnings)}


def trace_sql_lineage(sql, dialect="ansi", tables=""):
    lineage, results = _trace(sql, dialect, tables)
    return {"dialect": dialect,
            "output_columns": [_column_info(r) for r in results],
            "source_tables": lineage.source_tables(results)}


def columns_from_table(sql, table, dialect="ansi", tables=""):
    lineage, results = _trace(sql, dialect, tables)
    return {"table": table,
            "output_columns": lineage.columns_from_table(results, table)}


# ---------------------------------------------------------------------------
# Mapping / SQL generation tools
# ---------------------------------------------------------------------------
def show_mapping(mapping_file, source_table="", target_table=""):
    m = _load_mapping(mapping_file, source_table, target_table)
    return {"source_table": m.source_table, "target_table": m.target_table,
            "primary_key": m.primary_key,
            "columns": [asdict(c) for c in m.columns]}


def generate_sql_package(mapping_file, dialect="ansi", source_table="",
                         target_table="", save=True):
    m = _load_mapping(mapping_file, source_table, target_table)
    d = _dialect(dialect)
    try:
        checks = generate_sql_checks(m, d)
        script = generate_sql_script(m, d, checks)
        package = {
            "dialect": dialect,
            "expected_select": select_expected_sql(m, d),
            "create_table": create_table_sql(m, d),
            "checks": [{"name": c.name, "description": c.description,
                        "sql": c.sql, "expect": c.expect} for c in checks]}
    except ValueError as exc:
        raise ToolInputError(f"Could not generate the SQL: {exc}") from exc
    package["saved_to"] = None
    if save:
        name = f"checks_{_safe_stem(m.target_table)}_{dialect}.sql"
        path = output_dir() / name
        path.write_text(script, encoding="utf-8")
        package["saved_to"] = str(path)
    return package


# ---------------------------------------------------------------------------
# Build target / ETL checks tools
# ---------------------------------------------------------------------------
def build_expected_target(mapping_file, source_file="", source_db="",
                          source_table="", target_table="", dialect="ansi"):
    m = _load_mapping(mapping_file, source_table, target_table)
    _dialect(dialect)
    df = _dataset("source", m, source_file, source_db)
    _guard(df)
    try:
        expected, path, _ = build_target(m, df, output_dir() / "build",
                                         None, dialect)
    except Exception as exc:
        raise ToolInputError(f"Could not build the target: "
                             f"{str(exc)[:300]}") from exc
    return {"rows": int(len(expected)), "columns": list(expected.columns),
            "saved_to": str(path)}


def run_etl_checks(mapping_file, source_file="", target_file="",
                   source_db="", target_db="", source_table="",
                   target_table="", dialect="ansi", run_sql=False,
                   include_details=False):
    m = _load_mapping(mapping_file, source_table, target_table)
    _dialect(dialect)
    if run_sql and not target_db:
        raise ToolInputError("run_sql needs target_db (env:NAME).")
    source = _dataset("source", m, source_file, source_db)
    target = _dataset("target", m, target_file, target_db)
    _guard(source)
    try:
        results, report = run_checks(
            m, source, target, output_dir() / "checks",
            sql_url=db_ref(target_db, "target_db") if run_sql else None,
            dialect=dialect)
    except ToolInputError:
        raise
    except Exception as exc:
        raise ToolInputError(f"Could not run the checks: "
                             f"{str(exc)[:300]}") from exc
    keep = ["check", "column", "status", "failing"]
    if include_details:
        keep.append("detail")      # may contain example values from the data
    keep = [c for c in keep if c in results.columns]   # be tolerant
    status = results["status"]
    return {
        "all_passed": bool(checks_passed(results)),
        "passed": int((status == "PASS").sum()),
        "failed": int(status.isin(["FAIL", "ERROR"]).sum()),
        "skipped": int((status == "SKIP").sum()),
        "checks": results[keep].to_dict(orient="records"),
        "details_included": bool(include_details),
        "report_html": str(report)}
