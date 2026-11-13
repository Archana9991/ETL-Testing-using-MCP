"""Column lineage: which output column comes from which table and column.

Why this exists
---------------
When you are handed a big SQL query, the first question is usually
"where does this column really come from?".  This module answers that
by READING the SQL with a parser, not by guessing.

How it works (the short version)
--------------------------------
1. PARSE   : sqlglot turns the SQL text into a tree (an "AST").
2. QUALIFY : sqlglot rewrites the tree so every column says which table
             (or alias) it belongs to.  `SELECT name FROM t` becomes
             `SELECT t.name FROM t`.  With a schema it also expands `*`.
3. SCOPES  : each SELECT / CTE / subquery is a "scope" that knows its own
             sources (real tables, or other scopes).
4. TRACE   : for every output column we look at the columns used in its
             expression.  If a column comes from a real table, we are
             done.  If it comes from another scope (a CTE or subquery)
             we trace INTO that scope and carry on.  That "calls itself
             again" idea is called recursion.

Design rule (same as llm_agent.py): code decides, Claude only explains.
The tracing is plain deterministic code.  Claude is optional and only
sees the lineage table (names), never the SQL text itself.
"""
import re
from dataclasses import dataclass, field

import pandas as pd
import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope

# Our dialect names -> the names sqlglot uses.  "ansi" = sqlglot default.
DIALECTS = {
    "ansi": None,
    "sqlite": "sqlite",
    "postgres": "postgres",
    "oracle": "oracle",
    "mssql": "tsql",
}

# sqlglot renamed Union's parent class in newer versions; this works in both.
_SET_OP = getattr(exp, "SetOperation", exp.Union)


class LineageError(Exception):
    """The SQL could not be read, or it is not something we can trace."""


@dataclass(frozen=True)
class SourceColumn:
    """One real column in one real table."""
    table: str
    column: str

    def __str__(self):
        return f"{self.table}.{self.column}"


@dataclass
class ColumnLineage:
    """The answer for ONE output column of the query."""
    output: str                                   # name in the result
    sources: list = field(default_factory=list)   # list[SourceColumn]
    kind: str = "derived"    # direct | derived | constant | unknown
    steps: list = field(default_factory=list)     # the expressions applied
    warnings: list = field(default_factory=list)  # things we could not resolve


# ---------------------------------------------------------------------------
# Safety: never process sensitive IDs that are pasted into SQL literals
# ---------------------------------------------------------------------------
_SENSITIVE_LITERALS = [
    ("12-digit ID number", re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")),
    ("PAN-like code", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    ("card-like number", re.compile(r"\b\d{13,16}\b")),
    ("SSN-like number", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
]


def _check_literals(tree):
    """Stop if a quoted value in the SQL looks like a sensitive ID."""
    for lit in tree.find_all(exp.Literal):
        if not lit.is_string:
            continue
        for label, pattern in _SENSITIVE_LITERALS:
            if pattern.search(lit.name):
                raise LineageError(
                    f"STOPPED: the SQL contains a quoted value that looks "
                    f"like a {label}. Remove real personal data from the "
                    f"query and try again.")


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------
def _table_name(table):
    """db.schema.table as one string (alias is ignored on purpose)."""
    parts = [table.catalog, table.db, table.name]
    return ".".join(p for p in parts if p)


def _lookup(scope, name):
    """Find what `name` (a table alias) refers to, from this scope.

    Looks in the scope's own sources first, then inside scalar subqueries
    used in the SELECT list.  Returns a Table, a Scope, or None.
    """
    if name in scope.sources:
        return scope.sources[name]
    for sub in scope.subquery_scopes:
        found = _lookup(sub, name)
        if found is not None:
            return found
    return None


def _dedupe(items):
    seen, out = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _is_star_projection(node):
    return isinstance(node, exp.Star) or (
        isinstance(node, exp.Column) and isinstance(node.this, exp.Star))


def _set_operation_scopes(scope):
    """If this scope is a UNION/INTERSECT/EXCEPT, return its side scopes.

    Returns None for a normal SELECT.  We look at what the expression IS
    (isinstance) rather than relying on a scope attribute, because
    sqlglot has renamed those attributes between versions.
    """
    if not isinstance(scope.expression, _SET_OP):
        return None
    for attr in ("union_scopes", "set_operation_scopes"):
        sides = getattr(scope, attr, None)
        if sides:
            return list(sides)
    # Last resort: build a scope for each side ourselves.
    sides = [build_scope(scope.expression.left),
             build_scope(scope.expression.right)]
    if any(side is None for side in sides):
        raise LineageError("Could not analyse a UNION in the query.")
    return sides


def _trace(scope, memo, dialect):
    """Return a list[ColumnLineage], one per output column of `scope`.

    `memo` remembers scopes we already traced, so a CTE used five times
    is only worked out once.
    """
    key = id(scope)
    if key in memo:
        return memo[key]

    # UNION / INTERSECT / EXCEPT: trace each side, then merge by POSITION
    # (SQL matches the 1st column with the 1st column, and so on).
    side_scopes = _set_operation_scopes(scope)
    if side_scopes is not None:
        sides = [_trace(s, memo, dialect) for s in side_scopes]
        merged = []
        for position, first in enumerate(sides[0]):
            sources, steps, warnings, kinds = [], [], [], []
            for side in sides:
                if position >= len(side):
                    warnings.append("the sides of the UNION have a "
                                    "different number of columns")
                    continue
                sources += side[position].sources
                steps += side[position].steps
                warnings += side[position].warnings
                kinds.append(side[position].kind)
            kind = "direct" if set(kinds) == {"direct"} else "derived"
            merged.append(ColumnLineage(first.output, _dedupe(sources), kind,
                                        _dedupe(steps), _dedupe(warnings)))
        memo[key] = merged
        return merged

    results = []
    for proj in scope.expression.selects:
        name = proj.alias_or_name
        inner = proj.unalias()
        sources, steps, warnings = [], [], []
        upstream_kinds = []

        if _is_star_projection(inner):
            results.append(ColumnLineage(
                name, [], "unknown", [],
                ["'*' could not be expanded. Pass a schema listing the "
                 "table columns so '*' can be resolved."]))
            continue

        columns = list(inner.find_all(exp.Column))
        for col in columns:
            if col.table:
                src = _lookup(scope, col.table)
            elif len(scope.sources) == 1:
                src = next(iter(scope.sources.values()))
            else:
                src = None

            if isinstance(src, exp.Table):
                sources.append(SourceColumn(_table_name(src), col.name))
            elif isinstance(src, Scope):
                rows = _trace(src, memo, dialect)
                match = next((r for r in rows
                              if r.output.lower() == col.name.lower()), None)
                if match is None:
                    warnings.append(f"column {col.name!r} not found in "
                                    f"the CTE/subquery {col.table!r}")
                else:
                    sources += match.sources
                    steps += match.steps
                    warnings += match.warnings
                    upstream_kinds.append(match.kind)
            else:
                warnings.append(
                    f"could not tell which table column {col.sql()!r} "
                    f"belongs to. Qualify it (table.column) or pass a "
                    f"schema.")

        is_direct = (isinstance(inner, exp.Column)
                     and all(k == "direct" for k in upstream_kinds)
                     and not warnings)
        if is_direct:
            kind = "direct"
        elif not columns and inner.find(exp.Star):
            kind = "derived"          # e.g. COUNT(*): uses rows, not a column
            warnings.append("uses '*' (for example COUNT(*)), so it has no "
                            "single source column")
        elif not columns:
            kind = "constant"         # e.g. 'ACTIVE' or 1
        else:
            kind = "derived"

        if kind != "direct":
            steps.append(inner.sql(dialect=dialect))

        results.append(ColumnLineage(name, _dedupe(sources), kind,
                                     _dedupe(steps), _dedupe(warnings)))
    memo[key] = results
    return results


def trace_query(sql, schema=None, dialect="ansi"):
    """Trace every output column of a query back to source columns.

    sql     : one SELECT (or INSERT ... SELECT / CREATE TABLE ... AS SELECT).
    schema  : optional {"table": {"column": "type"}}.  Needed to expand
              SELECT * and to resolve unqualified columns across joins.
    dialect : ansi, sqlite, postgres, oracle or mssql.
    Returns a list of ColumnLineage.
    """
    if dialect not in DIALECTS:
        raise LineageError(f"Unknown dialect {dialect!r}. "
                           f"Choose one of: {', '.join(DIALECTS)}")
    read = DIALECTS[dialect]

    try:
        statements = [s for s in sqlglot.parse(sql, read=read) if s]
    except SqlglotError as exc:
        raise LineageError(f"Could not read the SQL: {exc}") from exc
    if len(statements) != 1:
        raise LineageError("Give exactly one SQL statement at a time "
                           f"(found {len(statements)}).")
    tree = statements[0]
    _check_literals(tree)

    # For INSERT ... SELECT and CREATE TABLE ... AS SELECT, the part we
    # trace is the SELECT inside.
    query = tree.expression if isinstance(tree, (exp.Create, exp.Insert)) \
        else tree
    if query is None or query.find(exp.Select) is None:
        raise LineageError("No SELECT found, so there is nothing to trace.")

    try:
        qualified = qualify(query, schema=schema, dialect=read,
                            validate_qualify_columns=False,
                            quote_identifiers=False, identify=False)
        root = build_scope(qualified)
    except SqlglotError as exc:
        raise LineageError(f"Could not analyse the query: {exc}") from exc
    if root is None:
        raise LineageError("Could not analyse the query structure.")

    return _trace(root, {}, read)


# ---------------------------------------------------------------------------
# Answering questions from the result
# ---------------------------------------------------------------------------
def to_frame(results):
    """One row per (output column, source column), as a DataFrame."""
    rows = []
    for r in results:
        transformation = " -> ".join(r.steps)
        warning = "; ".join(r.warnings)
        if not r.sources:
            rows.append({"output_column": r.output, "source_table": "",
                         "source_column": "", "kind": r.kind,
                         "transformation": transformation,
                         "warnings": warning})
        for s in r.sources:
            rows.append({"output_column": r.output, "source_table": s.table,
                         "source_column": s.column, "kind": r.kind,
                         "transformation": transformation,
                         "warnings": warning})
    return pd.DataFrame(rows, columns=[
        "output_column", "source_table", "source_column", "kind",
        "transformation", "warnings"])


def sources_of(results, column):
    """Which source columns feed this output column?"""
    for r in results:
        if r.output.lower() == column.lower():
            return list(r.sources)
    raise LineageError(f"No output column named {column!r}. "
                       f"Available: {', '.join(r.output for r in results)}")


def columns_from_table(results, table):
    """Which output columns depend on this table?"""
    want = table.lower()
    return [r.output for r in results
            if any(s.table.lower().split(".")[-1] == want.split(".")[-1]
                   for s in r.sources)]


def source_tables(results):
    """Every real table the query reads (for the columns it outputs)."""
    return _dedupe([s.table for r in results for s in r.sources])


def format_text(results):
    """A readable summary for the terminal."""
    lines = []
    for r in results:
        origin = ", ".join(str(s) for s in r.sources) or "(none)"
        lines.append(f"{r.output}  [{r.kind}]  <-  {origin}")
        if r.steps:
            lines.append("    how: " + " -> ".join(r.steps))
        for w in r.warnings:
            lines.append("    WARNING: " + w)
    return "\n".join(lines)


LINEAGE_SYSTEM = (
    "You answer questions about SQL column lineage. You are given a "
    "lineage table that was produced by a SQL parser. Answer ONLY from "
    "that table. If the table does not contain the answer, say so. "
    "Be short and concrete; name tables and columns exactly.")


def ask_about_lineage(question, results, llm):
    """Optional: let Claude answer a plain-English question.

    Only the lineage table is sent (table/column names and expressions),
    never the original SQL text.  `llm` is anything with .complete(),
    for example ClaudeClient (tests pass a fake).
    """
    table = to_frame(results).to_csv(index=False)
    user = f"Lineage table (CSV):\n{table}\nQuestion: {question}"
    return llm.complete(LINEAGE_SYSTEM, user)
