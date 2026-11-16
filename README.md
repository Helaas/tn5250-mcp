# tn5250-mcp

An MCP server that lets an AI agent drive IBM i (AS/400) 5250 green-screen
sessions: data entry, automated testing, navigating menus. It ships with a live
web viewer, so a person can watch, sign on, or take over at any time.

With a jt400 jar (JTOpen) it can also query Db2 for i with SQL and read and
write IFS files through the IBM i host servers.

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

Python 3.11+. Dependencies: the official `mcp` SDK (2.x) and PyYAML. For the
optional SQL/IFS tools: a JTOpen `jt400.jar` and Java 11+. No Java? Install
with the `java` extra (`.venv/bin/pip install -e ".[java]"`): it brings
[jdk4py](https://pypi.org/project/jdk4py/), OpenJDK packaged for pip (about
30 MB, GPLv2 with Classpath exception), and the server uses it by itself.

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
| `connect` | open a session with a saved `profile`, or `host`, `port` 992/23, `tls`, `device_name` (`AGENT*` = AGENTA, AGENTB, ...), `wide` for 27x132, `codepage` |
| `list_profiles` | the saved connection profiles |
| `get_screen` | screen text with rulers, cursor, keyboard state, `screen_id`, input-field table |
| `fill` | put values into fields (by `"row,col"`, `"#index"` or label) without sending |
| `press` | optional fills + cursor, then send a key and return the host's reply screen |
| `wait` | wait until the host wants input (optionally for some text) |
| `viewer` | link to the live viewer |
| `record_start`, `record_stop` | record a session's key presses as a new flow |
| `disconnect` | sign off from the screen's command line, then close (`signoff: false` to skip) |
| `get_session_log` | the session's transcript (passwords masked), also after a disconnect |
| `host_connect`, `host_disconnect` | open a SQL/IFS connection (with `--jt400`) |
| `sql_query` | run a query; the connection is read-only on the server side |
| `sql_execute` | change data (only with `--allow-sql-write`) |
| `ifs_list`, `ifs_read` | list folders, read stream files (text in their CCSID, or base64) |
| `ifs_write` | write stream files (only inside `--ifs-write-root` folders) |
| `list_sessions`, `keys` | housekeeping |
| *your flows* | one tool per screen flow loaded with `--flows` |

Several sessions can run side by side (`session` argument).

Example `press` call:

```json
{ "key": "Enter", "expect": "1:WORK WITH DATA IN A FILE",
  "values": { "ID": "3", "NAME": "SPROCKET", "QTY": "-3" } }
```

## SQL and IFS (host servers)

Besides the screen, IBM i offers its data through the *host servers*:
the database server (SQL) and the file server (IFS). tn5250-mcp uses them
through the IBM Toolbox for Java, which **you supply**:

1. Get **JTOpen**, the open-source Toolbox (IBM Public License 1.0), as
   `jt400.jar`: from [GitHub](https://github.com/IBM/JTOpen/releases) or Maven
   Central (`net.sf.jt400:jt400`). Don't take the copy inside IBM i Access
   Client Solutions: its licence doesn't allow using its parts on their own.
2. Have Java 11 or newer: either install the `java` extra (`pip install
   "tn5250-mcp[java]"`, a Java runtime as a Python package), or use one you
   have (`brew install openjdk@21`). It's found by itself in this order:
   `--java`, `$T5250_JAVA`, `$JAVA_HOME`, the `java` extra, the system's.
3. Start the server with `--jt400 /path/to/jt400.jar`.

```sh
tn5250-mcp --jt400 ~/lib/jt400.jar --profiles profiles.yaml \
           --ifs-write-root /home/MYUSER/out          # optional: allow ifs_write here
           # --allow-sql-write                        # optional: allow sql_execute
```

The agent calls `host_connect` (with a profile, whose `user` key names the
user profile, or with `host` and `user`), then `sql_query`, `ifs_list` or
`ifs_read`. The password is typed in the viewer, or comes from
`$T5250_PASSWORD` when nobody has the viewer open (CI). The agent never sees
it.

- `sql_query` runs on a connection the database server keeps read-only
  (`access=read only`), so even a statement that slips past every other check
  can't change data. It supports `?` parameter markers, a row limit, and
  output as a table or as JSON. Naming is system naming: `MYLIB/TABLE` or
  `MYLIB.TABLE`, plus the library list. QSYS2 services work too, for example
  `QSYS2.ACTIVE_JOB_INFO` or `QSYS2.IFS_OBJECT_STATISTICS`.
- `sql_execute` (INSERT, UPDATE, DELETE, CREATE, CALL, ...) is offered only with
  `--allow-sql-write`.
- `ifs_read` converts text from the file's CCSID; `binary: true` returns
  base64. `ifs_write` creates new files tagged with the CCSID you give (1208 =
  UTF-8 by default, 37 = EBCDIC US), refuses to replace a file unless
  `overwrite` or `append` is set, and is offered only with `--ifs-write-root`.
  It never writes outside those folders, and `..` isn't accepted.
- SQL text and write paths go through the block list (`QCMDEXC`, `DROP
  SCHEMA`, ...). The transcript (`get_session_log` with session `host-main`)
  records statements and file names, not file contents.
- The work is done by a small Java helper that talks JSON to the server. It
  ships compiled (`java/t5250-helper.jar`, our own classes only, for Java
  11+), so a runtime without a compiler is enough. After changing
  `T5250Helper.java`, rebuild it with `python scripts/build_helper.py
  /path/to/jt400.jar` (a test checks the jar matches the source). In Python:
  `HostServers("jt400.jar").connect(...)`, `.query(...)`, `.ifs_read(...)`.

## Connection profiles

Save the systems you use in a YAML file and start the server with
`--profiles profiles.yaml` (a file or a folder). The agent then connects with
`connect {"profile": "pub400"}` and never needs the host details.

```yaml
profiles:
  pub400:
    description: PUB400.COM, a free public IBM i
    host: pub400.com
    port: 992                         # tls: true is the default; port 23 without TLS
    after_connect: {flow: signon, with: {user: MYUSER}}
    on_reconnect: true                # sign on again after the host drops the session
  dev:
    host: dev400.example.com
    tls: false
    device_name: "AGENT*"             # AGENTA, AGENTB, ... (= is a random character)
    codepage: cp273
```

Keys: `host` (required), `description`, `port`, `tls`, `verify_tls`,
`device_name`, `wide`, `codepage`, `user` (for `host_connect`), `after_connect` (a flow name, or `{flow,
with}`), `on_reconnect`. Profiles never hold passwords; a file with a
`password` key is refused. Use a sign-on flow as `after_connect`: it asks for
the password in the viewer, or reads it from the environment.

- When the server starts it checks that every `after_connect` flow exists
  and that its parameters are valid.
- If the after-connect flow can't finish (for example nobody has the viewer
  open), the session stays connected and `connect` says why.
- After a reconnect the session asks for the same device name again, so the
  host can hand back the same job (depending on `QDSCJOBITV`).
- `--profiles-only` refuses `connect` with a host name, so the agent can only
  reach the systems you saved.
- Outside MCP: `Terminal.from_profile("pub400", "profiles.yaml", flows=[...])`,
  `tn5250-run run --profile pub400 ...` and pytest `--t5250-profile pub400`.
  When the sign-on flow needs a password that isn't in the environment, these
  print the viewer link and wait for you.

See [`examples/profiles.yaml`](examples/profiles.yaml).

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
- **Host allow list.** `--allowed-hosts "dev400,*.test.example.com"`, or
  `--profiles-only` to allow only your saved connection profiles.
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
- **Transcripts.** Every action and every screen the host sent is recorded,
  with hidden fields and secrets masked (decided before the key is sent, so a
  fast host can't expose a password). The agent reads it with
  `get_session_log` (last 400 entries in memory); `--log-dir DIR` also writes
  it to files, useful as test evidence.
- **Clean sign-off.** `disconnect` first types `SIGNOFF` on the screen's CL
  command line (a `===>` field labelled with "command", as on menus, list
  panels and Command Entry; not the STRSQL statement line), so no job is
  left behind on the host. On a screen without one it types nothing and says
  so. `Terminal.signoff()` and `tn5250-run --signoff` do the same.

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
| `--profiles PATH` (repeatable) | `T5250_PROFILES` (`:`-separated) | none |
| `--profiles-only` | `T5250_PROFILES_ONLY=1` | off |
| `--jt400 PATH` | `T5250_JT400` | no SQL/IFS tools |
| `--java PATH` | `T5250_JAVA` | found automatically (Java 11+) |
| `--allow-sql-write` | `T5250_ALLOW_SQL_WRITE=1` | off |
| `--ifs-write-root DIR` (repeatable) | `T5250_IFS_WRITE_ROOT` (`:`-separated) | no `ifs_write` |

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
