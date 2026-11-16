# tn5250-mcp

An MCP server that lets an AI agent drive IBM i (AS/400) 5250 green-screen
sessions: data entry, automated testing, navigating menus. It ships with a live
web viewer, so a person can watch, sign on, or take over at any time.

Procedures you use often can be written down as **screen flows** in YAML.
Each flow becomes an MCP tool with checked parameters, and the same files run
from a CLI, from Python and from pytest, so they also work as tests in CI.

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
| Mandatory / must-fill / check-digit fields | the host complains afterwards | refused before sending, like a real terminal |
| Destructive commands | nothing stops them | configurable block list |
| Repeatable procedures | prompt the agent again | YAML screen flows, run as tools or tests |
| Writing those procedures | by hand | record them in the viewer or with the agent |
| Host drops the session | start over by hand | reconnects by itself |

## Install

```sh
git clone https://github.com/Helaas/tn5250-mcp && cd tn5250-mcp
python3 -m venv .venv && .venv/bin/pip install -e .
```

Python 3.11+. Dependencies: the official `mcp` SDK (2.x) and PyYAML.

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
| `record_start`, `record_stop` | record a session's key presses as a new flow |
| `list_sessions`, `disconnect`, `keys` | housekeeping |
| *your flows* | one tool per screen flow loaded with `--flows` |

Several sessions can run side by side (`session` argument).

Example `press` call:

```json
{ "key": "Enter", "expect": "1:WORK WITH DATA IN A FILE",
  "values": { "ID": "3", "NAME": "SPROCKET", "QTY": "-3" } }
```

## Screen flows

A flow is a named procedure: a fixed list of steps with typed parameters. The
agent (or a test) only supplies the parameters; it can't type outside the flow.

```yaml
toolset: orders                       # default toolset for the flows below
flows:
  add_order:
    description: Add an order on the order entry screen.   # the agent reads this
    params:
      customer: {type: string, upper: true, pattern: "^[A-Z0-9]{1,8}$"}
      qty:      {type: integer, min: 1, max: 9999}
    steps:
      - call: open_order_entry        # flows can call flows
      - name: enter the order
        expect: "1:ORDER ENTRY"       # must be on screen, or the flow stops
        fill: {Customer: "{customer}", Quantity: "{qty}"}
        press: Enter
        check: {not_text: "not found"}
        capture: {order_no: 'Order (\d+) created'}
      - expect: "Order {order_no} created"
        press: F3
```

```sh
tn5250-mcp --flows flows/                  # offer the flows as tools (next to fill/press)
tn5250-mcp --flows flows/ --flows-only     # only flows: no free typing for the agent
tn5250-mcp --flows flows/ --toolsets orders,ibmi
tn5250-run check flows/                    # validate and list
```

**Parameters** (`params`): `type` is `string` (`min_length`, `max_length`,
`pattern`, `enum`, `upper`), `integer` or `number` (`min`, `max`, `decimals`),
`boolean` (typed as `true_value`/`false_value`, default `Y`/`N`) or `secret`
(a password; optional `prompt` label and `env: VAR`; see "Passwords" below).
Add `default` to make a parameter optional, and
`description` for the agent. Values are checked before anything is typed:
for this flow, for every flow it calls, and against the block list. A flow is
refused as a whole; it doesn't stop half-way because of a bad value.

**Steps** (run in order):

| key | meaning |
|---|---|
| `expect` | text (or `"row:text"`, or a list) that must be on screen before the step acts. Required for any step that fills or presses (`Reset` excepted). |
| `when` | only run this step if this text is on screen; otherwise skip it (pop-ups, confirm screens) |
| `fill` | field → value. Fields by label, `"row,col"`, `"#index"` or `@cursor` |
| `cursor` | put the cursor on a field or position first |
| `press` | key to send, then wait for the host's answer |
| `wait_for`, `timeout` | wait for text after the key (or on its own); default timeout 30 s |
| `check` | after the step: `text`, `not_text`, `regex`, `keyboard` (`ready`, `error`, `any`; default: an operator error fails the step), `captured` (capture → regex) |
| `capture` | name → regex with one group, `{field: ref}` or `{rows: "3-22"}`. Usable as `{name}` in later steps and returned to the caller |
| `call`, `with` | run another flow with these parameters |
| `fail` | with `when`: stop the flow with this message if the text is there |
| `name` | step label for logs and errors |

`{name}` inserts a parameter or capture. In regexes the value matches
literally. Flow options: `description` (required), `toolset`, `read_only`
(your promise that the flow only displays things; such a flow may only call
other read-only flows), `expose: false` (a helper for other flows, not a tool),
`pace` (seconds to wait after each step so a person can follow in the viewer;
`--pace` sets it for all flows).

A failing flow reports the flow, the step and the screen at that moment. Over
MCP, a flow returns its step log, its captures and the final screen.

See [`examples/flows/`](examples/flows/): `ibmi.yaml` (`signon`, `run_command`,
a read-only `sql_select`) and `dfu_demo.yaml` (data entry through UPDDTA, plus
`data_entry_test`, the end-to-end test).

### Recording flows

Instead of writing a flow, record it. Click **Record** in the viewer (or let
the agent call `record_start`), go through the screens once, then stop and
give the flow a name and a description. Each key press becomes a step:

- `expect`: the title of the screen the key was pressed on;
- `fill`: the fields typed, by label when the label is unique on that screen;
- `check`: the title of the screen that came back (or `keyboard: error` when
  the host answered with an operator error).

Typed values become parameters, with what you typed as the default, so the
flow replays as is or runs with other values (`params: none` keeps them
literal). Passwords are never recorded: a hidden field becomes a `secret`
parameter that is asked for in the viewer when the flow runs.

The recording is checked to load as a valid flow, saved to `--record-dir` as
`<name>.yaml`, and offered as a tool right away; recordings in that folder
load again when the server starts. Review the expect/check texts once: a
recording knows which screens you went through, not which parts of them
matter. In Python: `t.record_start()` ... `t.record_stop("name", "what it
does", save_dir="flows")`.

## Scripts, CI and pytest

The flows and the engine work without an MCP client.

**Command line:**

```sh
tn5250-run run --host pub400.com --flows examples/flows data_entry_test lib=MYLIB
```

Sign-on: `--signon env` uses `$T5250_USER` / `$T5250_PASSWORD` (CI secrets),
and `--signon viewer` prints a viewer link and waits for a person. `auto`, the
default, uses env when `$T5250_PASSWORD` is set. Add `--signoff`, `--json`,
`--screen` or `--log-dir logs` for transcripts. Exit codes: 0 passed, 1 flow
failed, 2 invalid flows or arguments, 3 connection or sign-on problem.

**Python:**

```python
from tn5250_mcp import Terminal

with Terminal("pub400.com", flows=["flows/"], log_dir="logs") as t:
    t.signon()                                   # from $T5250_USER / $T5250_PASSWORD
    r = t.run("add_order", customer="c100", qty=3)
    print(r.captures["order_no"])
    t.press("F3", expect="ORDER ENTRY")          # raw keys work too
    t.signoff()
```

**pytest** (the plugin is installed with the package):

```python
def test_add_order(t5250):                       # a signed-on session for the test run
    r = t5250.run("add_order", customer="C100", qty=3)
    assert r.captures["order_no"]
```

```sh
T5250_HOST=pub400.com T5250_USER=... T5250_PASSWORD=... pytest --t5250-flows flows/
```

Without `T5250_HOST`, tests that use `t5250` are skipped. Without a password,
pass `--t5250-viewer-port 0` to sign on by hand in the viewer. A failing
test's report includes the 5250 screen. `--t5250-log-dir` keeps transcripts as
test evidence.

### Passwords

A `secret` parameter is never passed in by the caller. When the flow runs:

1. If somebody is watching the session in the viewer, a password dialog opens
   there and the person types it. The dialog keeps password managers out and
   only sends what was actually typed or pasted, because an auto-filled stale
   password counts as a failed sign-on (and enough of those disable the user
   profile).
2. Otherwise the parameter's `env` variable is used (CI secrets).
3. Otherwise the flow stops before anything is typed and tells the agent to
   hand the user the viewer link.

The value goes to the host only: it is masked in transcripts, results and
errors, and never reaches the agent. Flows with secrets are offered over MCP
only when the viewer is running, without the secret in their parameters. So an
agent can run `signon`, and you type the password in the viewer.

## Safety rails

- **Screen checks (on by default).** Every `fill`/`press` must carry `expect`
  (text that must be on the screen, or `"row:text"`) or `screen_id` (from the
  last read). If the screen changed in the meantime, because the host moved on
  or a person typed in the viewer, the call is refused.
- **Block list.** Values containing commands such as `PWRDWNSYS`, `ENDSBS`,
  `DLTLIB`, `CLRLIB`, `DLTUSRPRF`, `CHGSYSVAL`, `DROP SCHEMA` or `QCMDEXC` are
  refused (full list in `src/tn5250_mcp/guard.py`). Add your own with `--block REGEX`. It's a
  seatbelt, not a security boundary: give the agent's user profile only the
  authority it needs.
- **Host allow list.** `--allowed-hosts "dev400,*.test.example.com"`.
- **Flows instead of free typing.** `--flows-only` removes `fill` and `press`, so
  the agent can read screens and run your flows, nothing else. `--read-only`
  also drops every flow not marked `read_only`.
- **Passwords stay with people.** Hidden (non-display) fields are never shown
  to the agent. Sign on in the viewer, or let a sign-on flow ask for the
  password in the viewer; the agent carries on from there.
- **Terminal-side field checks.** Like a real 5250 terminal, `press` (and the
  viewer) refuse to send when a mandatory-entry field is empty (operator error
  0007), a mandatory-fill field is only partly filled (0014) or a self-check
  field has a wrong modulus 10/11 check digit (0015). Alphabetic-only (0008),
  numeric-only (0009) and digits-only (0010) fields reject other characters.
  Nothing is sent, the cursor goes to the field, and the error says what to
  fix. Help, Clear, Print and the roll keys are exempt, as are keys whose field
  data the screen doesn't send.
- **Per-tool permissions.** Read tools are marked read-only and `press` is
  marked destructive, so MCP clients can auto-approve reading and still ask
  before keys are sent.
- **Transcripts.** `--log-dir DIR` records every action and every screen the
  host sent, with hidden values masked. Useful as test evidence.

## Reconnecting

When the host drops a session (a line failure, an idle timeout, `SIGNOFF
ENDCNN(*YES)`), the server reconnects by itself, like a 5250 emulator: up to
`--reconnect-attempts` times (default 10), `--reconnect-interval` seconds
apart (default 5). Meanwhile the screen header and the viewer say
"reconnecting (attempt n of m)", `wait` waits for it, and keys are refused
with a message telling the agent to wait. Afterwards the session starts over
at the sign-on screen, and the header says when it reconnected. Sign on again,
in the viewer or with a sign-on flow. If every attempt fails, the session
stays disconnected (use `connect`, or **Reconnect** in the viewer).
`disconnect` never triggers a reconnect. The Python `Terminal` and
`tn5250-run` don't reconnect unless you ask (`reconnect_attempts=`), because
scripts should fail fast.

To get back the *same* job instead of a new one, connect with a fixed
`device_name`: the host can then reconnect you to the disconnected job,
depending on its `QDSCJOBITV` setting.

## The viewer

A local page (`http://127.0.0.1:8251/?token=…`, link from the `viewer` tool)
that shows the session live in colour with real input fields. It also shows
the password dialog when a flow needs a secret, has the **Record** button for
recording flows, shows reconnect progress, and reloads itself when the server
has a newer version of the page. Type, press Enter
or F-keys (Shift+F1-F12 = F13-F24), PageUp/PageDown, Esc = Reset. It only
listens on 127.0.0.1 and needs the random token, and it checks the Host header.
`--viewer-port 0` turns it off.

## Configuration

| flag | env | default |
|---|---|---|
| `--transport stdio\|http` | `T5250_TRANSPORT` | `stdio` |
| `--port` | `T5250_PORT` | `8250` (HTTP transport, path `/mcp`) |
| `--viewer-port` | `T5250_VIEWER_PORT` | `8251` (a free port if taken), `0` = off |
| `--allowed-hosts` | `T5250_ALLOWED_HOSTS` | any |
| `--block REGEX` (repeatable) | `T5250_BLOCK` (comma list) | none extra |
| `--no-default-blocks` | `T5250_NO_DEFAULT_BLOCKS=1` | defaults on |
| `--no-require-check` | `T5250_REQUIRE_CHECK=0` | checks required |
| `--log-dir` | `T5250_LOG_DIR` | no logs |
| | `T5250_VIEWER_TOKEN` | random per start (set it to keep viewer links valid across restarts) |
| `--codepage` | `T5250_CODEPAGE` | `cp037` (also cp273, cp500, cp1140, …) |
| `--flows PATH` (repeatable) | `T5250_FLOWS` (`:`-separated) | none |
| `--toolsets a,b` | `T5250_TOOLSETS` | all |
| `--flows-only` | `T5250_FLOWS_ONLY=1` | off |
| `--read-only` | `T5250_READ_ONLY=1` | off |
| `--pace SECONDS` | `T5250_PACE` | `0` (no wait between flow steps) |
| `--reconnect-attempts N` | `T5250_RECONNECT_ATTEMPTS` | `10`, `0` = off |
| `--reconnect-interval SECONDS` | `T5250_RECONNECT_INTERVAL` | `5` |
| `--record-dir DIR` | `T5250_RECORD_DIR` | recordings are returned, not saved |

## Examples and tests

- `examples/call.py TOOL '{json args}'`: call one tool against an HTTP server.
- `examples/flows/`: example screen flows, including `data_entry_test`, an
  end-to-end test. It creates a table, keys three records through UPDDTA (one
  negative quantity), checks them with STRSQL, then drops the table.
- `examples/data_entry_test.py LIB`: the same test written as an MCP client
  script, using `press` directly.
- `pytest`: offline tests (data stream parsing, field encoding, signed
  numbers, save/restore screens, guards, flows, MCP flow tools, CLI). They use
  a scripted fake host, so no IBM i is needed.

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
