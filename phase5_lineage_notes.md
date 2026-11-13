# Phase 5: SQL column lineage (learning notes)

## The problem
Someone hands you a 200-line SQL query. You need to know:
*"Where does the column `full_name` really come from?"*
That question is called **column lineage**.

## What we built
`src\etl_agent\lineage.py` reads a query and, for every output column, reports:

| Field | Meaning |
|---|---|
| `sources` | the real table.column(s) it comes from |
| `kind` | `direct` (plain copy), `derived` (calculated), `constant` (e.g. `'ACTIVE'`), `unknown` (could not tell) |
| `steps` | the expressions applied on the way, e.g. `UPPER(TRIM(first_name)) || last_name` |
| `warnings` | anything it could not resolve (it says so instead of guessing) |

## Why a parser and not just asking Claude
Your project already follows one rule: **Claude proposes, your code decides.**
Same here. A *parser* is a program that understands SQL grammar exactly, so
the same query always gives the same answer, and you can test it. Claude is
optional and only turns the answer into plain English.

## The four steps inside `trace_query`
1. **Parse**: `sqlglot.parse` turns SQL text into a tree (an *AST*, abstract
   syntax tree). `a + b` becomes a node "Add" with two children.
2. **Qualify**: rewrites `SELECT name FROM t` into `SELECT t.name FROM t`, so
   every column states its table. With a *schema* (a dict of table -> columns)
   it also expands `SELECT *` into real column names.
3. **Scopes**: every SELECT, CTE (`WITH x AS (...)`) and subquery is a *scope*.
   A scope's `sources` map an alias (`c`) to either a real table or another scope.
4. **Trace (recursion)**: for each output column, look at the columns inside its
   expression. Real table -> record it. Another scope -> run `_trace` on that
   scope and continue. A function that calls itself like this is *recursion*;
   it is what lets us follow a column through CTE inside CTE inside subquery.

Small ideas worth knowing:
- **`memo`** (a dict) remembers scopes already traced, so a CTE used 5 times is
  worked out once.
- **`@dataclass`** gives us tidy record types (`SourceColumn`, `ColumnLineage`)
  without writing boilerplate.
- **UNION** matches columns by *position* (1st with 1st), which is how SQL
  itself works, so that is how we merge.
- **Sensitive guard**: if a quoted value in the SQL looks like a 12-digit ID,
  PAN, card or SSN, we stop. Lineage only needs names, never real data.

## Install and run (PowerShell, from `Desktop\etl-agent`)
1. Add one line to `requirements.txt`: `sqlglot>=25`
2. Install it: `pip install sqlglot`
3. Rename the shared files:
   - `lineage.txt` -> `src\etl_agent\lineage.py`
   - `test_lineage.txt` -> `tests\test_lineage.py`
4. Run: `python -m pytest tests\test_lineage.py -v`
5. Then the full suite: `python -m pytest` (expect 151 + the new ones)

Quick try in Python:
```python
from etl_agent.lineage import trace_query, format_text
sql = "SELECT UPPER(c.first_name) || c.last_name AS full_name FROM customers c"
print(format_text(trace_query(sql, dialect="oracle")))
```

## Honest status: what I did and did NOT verify
- Verified here: both files compile, and the helper functions
  (`to_frame`, `sources_of`, `columns_from_table`, `format_text`,
  `ask_about_lineage`) run correctly.
- **Not verified:** the actual SQL tracing and the 20 tests. My sandbox has no
  network, so I could not install `sqlglot`. You will be the first to run them.
  If any fail, paste me the failure text; I expect small version-related
  tweaks, not a redesign. The test file skips itself if `sqlglot` is missing,
  so it cannot break your existing 151 tests.

## Known limits (v1)
- Only the SELECT list is traced. Columns used only in WHERE / JOIN / GROUP BY
  are not reported as "influencing" an output (that is *filter lineage*, a
  possible later addition).
- `SELECT *` needs a schema; without one you get an `unknown` plus a warning.
- Unqualified columns across a join need a schema, otherwise a warning.
- One statement at a time. Window functions, PIVOT, and views-of-views are not
  specially handled. Procedural code (PL/SQL, T-SQL blocks) is out of scope.
- `INSERT ... SELECT` traces the SELECT part; it does not yet map to the
  insert's target column names.

## Possible next steps
1. CLI command: `etl-agent lineage query.sql --dialect oracle --ask "..."`.
2. A "Lineage" tab in the Streamlit app (paste SQL, see a table).
3. Feed the lineage into your mapping checks (does the mapping file match what
   the SQL really does?).
