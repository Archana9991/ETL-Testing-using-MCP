# Phase 6: the ETL Agent as an MCP server (learning notes)

## What MCP is, in plain terms
MCP (Model Context Protocol) is a standard way for an AI app (a "client",
for example Claude Desktop) to use tools that live in another program (a
"server"). You write ordinary functions, register them as tools, and the AI
app can call them during a chat: *"trace the lineage of this query"*,
*"run the ETL checks on these two files"*.

Three words to remember:
- **Tool**: one function the AI may call. It is described to the AI by its
  name, its argument names/types, and its docstring.
- **Server**: your program (`etl_agent.mcp_server`). It does the real work.
- **Client**: the AI app. It decides *when* to call a tool; your code decides
  *what the tool does*.

## Why it fits your project
Your rule has been "code decides, Claude explains". An MCP server is that rule
turned around: the AI app talks to the user, and your tested code does every
calculation. It also removes the API-key problem: the AI app you already use
does the talking, so your code never calls Claude and needs no key.

## What we built
| File | Role |
|---|---|
| `src\etl_agent\mcp_tools.py` | The logic: plain Python, no `mcp` import, easy to test |
| `src\etl_agent\mcp_server.py` | Thin layer: registers the functions as tools (FastMCP) and runs over stdio |
| `tests\test_mcp_tools.py` | Tests for the safety rules, every tool, and the real server |

Same layering as `ui_helpers.py` / `lineage_ui.py`: the outer layer is thin,
the logic is testable. CLI, Streamlit tab and MCP server all share one engine.

## The six tools
| Tool | Answers |
|---|---|
| `trace_sql_lineage` | Where does each output column come from, and how was it derived? |
| `columns_from_table` | Which output columns depend on table T? |
| `show_mapping` | Is the mapping valid? What are its columns, rules and key? |
| `generate_sql_package` | Expected SELECT, CREATE TABLE and SQL checks for a dialect (saves checks.sql) |
| `build_expected_target` | Build the expected target (returns count/columns/path, never rows) |
| `run_etl_checks` | Judge a loaded target against source and mapping |

## Safety design (the important part)
A tool's result is read by an AI, and the AI is steered by whatever is typed
in the chat. So the server is stricter than the UI:

1. **Folder fence.** Files are only read from, and written under, ONE folder:
   the environment variable `ETL_AGENT_MCP_DIR` (default: the folder the server
   starts in). `.resolve()` expands `..` and shortcuts first, so
   `sub\..\..\secret.csv` cannot escape. Outputs go to `mcp_output\` inside it.
2. **No passwords in chat.** Databases are accepted only as `env:NAME`. The real
   URL stays in an environment variable on your PC. A raw `postgresql://user:pw@...`
   is refused.
3. **No rows back.** Results are names, counts and verdicts. The check `detail`
   text can contain example values from your data, so it is OFF unless
   `include_details=true` is passed.
4. **Guard stays on.** The sensitive-data guard cannot be switched off from a
   tool (there is no "allow sensitive" argument).
5. **Nothing destructive.** No tool drops or replaces a database table. (The UI
   can; the MCP server deliberately cannot.)
6. **Lineage literal check.** SQL containing quoted values that look like a
   12-digit ID, PAN, card or SSN number is refused.

## Why print() is redirected
The server talks to the AI app through its standard input/output ("stdio").
Anything printed to normal output would be mixed into that conversation and
break it. Your `ETLAgent` logs with `print`, so every tool call runs inside
`contextlib.redirect_stdout(sys.stderr)`: prints go to the log instead.

## Install
From `Desktop\etl-agent`:
1. Save the files (rename from `.txt`):
   - `mcp_tools.txt`  -> `etl_agent\mcp_tools.py`
   - `mcp_server.txt` -> `etl_agent\mcp_server.py`
   - `test_mcp_tools.txt` -> `tests\test_mcp_tools.py`
   (use the same package folder where `lineage.py` lives)
2. `pip install mcp sqlglot` and add `mcp>=1.2` to `requirements.txt`.
3. `python -m pytest tests\test_mcp_tools.py -q`

## Connect to Claude Desktop (Windows)
1. Find the Python that has your packages installed:
   `python -c "import sys; print(sys.executable)"`
2. In Claude Desktop: Settings -> Developer -> Edit Config. This opens
   `%APPDATA%\Claude\claude_desktop_config.json`.
3. Add the server (use YOUR paths; they must be absolute; `\\` in JSON):
```json
{
  "mcpServers": {
    "etl-agent": {
      "command": "C:\\path\\to\\python.exe",
      "args": ["-m", "etl_agent.mcp_server"],
      "env": {
        "PYTHONPATH": "C:\\Users\\189830\\Desktop\\etl-agent",
        "ETL_AGENT_MCP_DIR": "C:\\Users\\189830\\Desktop\\etl-agent\\sample_data"
      }
    }
  }
}
```
   If the file already has `mcpServers`, add only the `"etl-agent"` entry inside it.
   A stray comma breaks the whole file: check it with
   `python -m json.tool "$env:APPDATA\Claude\claude_desktop_config.json"`.
4. Quit Claude Desktop completely and start it again.
5. Try: *"Use etl-agent to show the mapping in customer_mapping.csv with source
   table CUSTOMERS_RAW and target table DIM_CUSTOMER."*
6. If it does not appear, read the logs in `%APPDATA%\Claude\logs`
   (a file named after the server).
For a database, set the variable on your PC (never in the chat), then pass
`env:NAME` to the tool.

## Honest status
- Verified here: all files compile, and the safety rules (folder fence, `..`
  escape, wrong file types, `env:NAME` rule, sensitive guard, error messages)
  pass when run against stand-in modules.
- NOT verified: the tools running on your real modules, and the real-server test.
  My sandbox has no network, so I could not install `mcp` or `sqlglot`, and
  `automation.py` is not in my project copy. You will be first to run them. The
  last test starts the real server and lists its tools; it skips itself if `mcp`
  is missing. Send me any failure text.
- Your organization may restrict custom MCP servers or have rules about what
  goes into an AI chat. Tool results are names, counts and verdicts by design,
  but check before using it on work data.

## Limits (v1)
- Only the six tools. No `compare files/tables`, `generate bad files` or `run the
  agent` tools yet (Phases 1-3): a natural next group.
- One allowed folder. Files elsewhere must be copied in.
- `run_sql` needs source and target tables in one database, like the UI.
- Large files are loaded fully into memory (same as the rest of the project).
