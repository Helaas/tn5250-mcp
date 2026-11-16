# tn5250-mcp

An MCP server that lets an AI agent drive IBM i (AS/400) 5250 green-screen
sessions: data entry, automated testing, navigating menus. It ships with a live
web viewer, so a person can watch, sign on, or take over at any time.

It speaks the 5250 protocol itself, in pure Python. There is no screen
scraping, and it doesn't need Java or a terminal emulator. That gives the agent
the host's real field table: positions, lengths, numeric and signed fields,
mandatory and hidden fields, and the exact keyboard state.

## Why not just script a terminal emulator?

| | screen scraping | tn5250-mcp |
|---|---|---|
| Input fields | guessed from underlines | exact, from the host's format table |
| Numeric / signed / right-adjusted fields | you send Field Exit / Field Minus yourself | handled per field (`"QTY": "-3"`) |
| Password fields | invisible | listed as hidden, contents never shown to the agent |
| "Is the host done?" | wait for the screen to stop changing | exact: the host's read command + keyboard unlock |
| Wrong-screen typing | possible | refused (`expect` / `screen_id` checks) |
| Destructive commands | nothing stops them | configurable block list |

## Install

```sh
git clone https://github.com/Helaas/tn5250-mcp && cd tn5250-mcp
python3 -m venv .venv && .venv/bin/pip install -e .
```

Python 3.11+. The only dependency is the official `mcp` SDK (2.x).

## Hook it up

**Claude Code** (stdio, server starts with the session):

```sh
claude mcp add tn5250 -- /path/to/tn5250-mcp/.venv/bin/tn5250-mcp --log-dir ~/tn5250-logs
```

**Long-running server** (sessions survive client restarts; several clients can share it):

```sh
.venv/bin/tn5250-mcp --transport http --port 8250 --log-dir logs
claude mcp add --transport http tn5250 http://127.0.0.1:8250/mcp
```

**Claude Desktop** (`claude_desktop_config.json`):

```json
{ "mcpServers": { "tn5250": {
    "command": "/path/to/tn5250-mcp/.venv/bin/tn5250-mcp",
    "args": ["--allowed-hosts", "pub400.com,*.test.example.com", "--log-dir", "/path/to/logs"] } } }
```

## Tools

| tool | what it does |
|---|---|
| `connect` | open a session (`host`, `port` 992/23, `tls`, `device_name`, `wide` for 27x132, `codepage`) |
| `get_screen` | screen text with rulers, cursor, keyboard state, `screen_id`, input-field table |
| `fill` | put values into fields (by `"row,col"`, `"#index"` or label) without sending |
| `press` | optional fills + cursor, then send a key and return the host's reply screen |
| `wait` | wait until the host wants input (optionally for some text) |
| `viewer` | link to the live viewer |
| `list_sessions`, `disconnect`, `keys` | housekeeping |

Several sessions can run side by side (`session` argument).

Example `press` call:

```json
{ "key": "Enter", "expect": "1:WORK WITH DATA IN A FILE",
  "values": { "ID": "3", "NAME": "SPROCKET", "QTY": "-3" } }
```

## Safety rails

- **Screen checks (on by default).** Every `fill`/`press` must carry `expect`
  (text that must be on the screen, or `"row:text"`) or `screen_id` (from the
  last read). If the screen changed in the meantime, because the host moved on
  or a person typed in the viewer, the call is refused.
- **Block list.** Values containing commands such as `PWRDWNSYS`, `ENDSBS`,
  `DLTLIB`, `CLRLIB`, `DLTUSRPRF`, `CHGSYSVAL`, `DROP SCHEMA` are refused (full
  list in `src/tn5250_mcp/guard.py`). Add your own with `--block REGEX`. It's a
  seatbelt, not a security boundary: give the agent's user profile only the
  authority it needs.
- **Host allow list.** `--allowed-hosts "dev400,*.test.example.com"`.
- **Passwords stay with people.** Hidden (non-display) fields are never shown
  to the agent. Sign on in the viewer; the agent carries on from there.
- **Per-tool permissions.** Read tools are marked read-only and `press` is
  marked destructive, so MCP clients can auto-approve reading and still ask
  before keys are sent.
- **Transcripts.** `--log-dir DIR` records every action and every screen the
  host sent, with hidden values masked. Useful as test evidence.

## The viewer

A local page (`http://127.0.0.1:8251/?token=…`, link from the `viewer` tool)
that shows the session live in colour with real input fields. Type, press Enter
or F-keys (Shift+F1-F12 = F13-F24), PageUp/PageDown, Esc = Reset. It only
listens on 127.0.0.1 and needs the random token, and it checks the Host header.
`--viewer-port 0` turns it off.

## Configuration

| flag | env | default |
|---|---|---|
| `--transport stdio\|http` | `T5250_TRANSPORT` | `stdio` |
| `--port` | `T5250_PORT` | `8250` (HTTP transport, path `/mcp`) |
| `--viewer-port` | `T5250_VIEWER_PORT` | `8251`, `0` = off |
| `--allowed-hosts` | `T5250_ALLOWED_HOSTS` | any |
| `--block REGEX` (repeatable) | `T5250_BLOCK` (comma list) | none extra |
| `--no-default-blocks` | `T5250_NO_DEFAULT_BLOCKS=1` | defaults on |
| `--no-require-check` | `T5250_REQUIRE_CHECK=0` | checks required |
| `--log-dir` | `T5250_LOG_DIR` | no logs |
| | `T5250_VIEWER_TOKEN` | random per start (set it to keep viewer links valid across restarts) |
| `--codepage` | `T5250_CODEPAGE` | `cp037` (also cp273, cp500, cp1140, …) |

## Examples and tests

- `examples/call.py TOOL '{json args}'`: call one tool against an HTTP server.
- `examples/data_entry_test.py LIB`: end-to-end test. It creates a table, keys
  three records through UPDDTA (one negative quantity), checks them with STRSQL,
  then cleans up.
- `pytest`: offline protocol tests (data stream parsing, field encoding, signed
  numbers, save/restore screens, guards).

A free public IBM i to try it on: [PUB400.COM](https://pub400.com).

## Protocol coverage

TN5250 over TLS or plain telnet (RFC 1205/4777: terminal type, EOR, binary,
NEW-ENVIRON with DEVNAME). It handles Write to Display (SBA, SF with FFW/FCW,
SOH with the F-key data mask, IC, MC, RA, EA, TD, WEA), Clear Unit (24x80 and
27x132), Clear Format Table, Read MDT / Read Input / Read Immediate, Read
Screen Immediate, Write Error Code (operator errors and Reset), Save/Restore
Screen (pop-ups and F4 prompts), Roll, 5250 Query reply, SysReq, Attn,
continued (multi-line) fields and message-waiting.

Not covered: the GUI-enhanced 5250 constructs (windows, selection fields and
scroll bars as structured fields). The server announces itself as a
non-enhanced display, so the host draws those with plain characters instead.
DBCS and printer sessions aren't covered either.

The open-source [tn5250](https://github.com/tn5250/tn5250) emulator was used
as the protocol reference.
