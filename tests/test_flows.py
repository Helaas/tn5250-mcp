"""Offline tests for screen flows, the MCP flow tools, the Python API and
the CLI, against a scripted fake host."""

import asyncio
import json
import textwrap

import pytest
import yaml
from mcp.client import Client

from tn5250_mcp import telnet as T
from tn5250_mcp.cli import main as cli_main
from tn5250_mcp.flows import FlowConfigError, FlowError, FlowRunner, FlowSet
from tn5250_mcp.guard import Guard
from tn5250_mcp.manager import Manager
from tn5250_mcp.server import build_server
from tn5250_mcp.session import Session
from tn5250_mcp.terminal import Terminal

E = lambda s: s.encode("cp037")  # noqa: E731


def sba(r, c):
    return bytes([0x11, r, c])


def sf(ffw, attr, length):
    return bytes([0x1D, ffw >> 8, ffw & 0xFF, attr, length >> 8, length & 0xFF])


WTD = b"\x04\x40\x04\x11\x00\x08"
READ = b"\x04\x52\x00\x00"


def menu(msg=""):
    return (WTD + sba(1, 2) + E("MAIN    Demo Main Menu") + sba(19, 2) + E("Selection or command")
            + sba(20, 2) + E("===>") + sba(20, 6) + sf(0x4000, 0x24, 60)
            + (sba(24, 2) + E(msg) if msg else b"") + bytes([0x13, 20, 7]) + READ)


def entry():
    return (WTD + sba(1, 2) + E("ORDER ENTRY")
            + sba(4, 1) + E("Customer:") + sba(4, 12) + sf(0x4000, 0x24, 8)
            + sba(5, 1) + E("Qty:") + sba(5, 12) + sf(0x4700, 0x24, 6)
            + bytes([0x13, 4, 13]) + READ)


def done(order):
    return (WTD + sba(1, 2) + E("ORDER ENTRY") + sba(10, 2) + E("Order %d created" % order)
            + sba(4, 1) + E("Customer:") + sba(4, 12) + sf(0x4000, 0x24, 8)
            + sba(5, 1) + E("Qty:") + sba(5, 12) + sf(0x4700, 0x24, 6) + READ)


def signon_screen():
    return (WTD + sba(1, 10) + E("Welcome to DEMO") + sba(5, 1) + E("Your user name:")
            + sba(5, 25) + sf(0x4020, 0x24, 10) + sba(6, 1) + E("Password:")
            + sba(6, 25) + sf(0x4000, 0x27, 20) + READ)


def decode_fields(data: bytes) -> dict:
    """Read MDT reply -> {(row, col): text}"""
    out, i = {}, 3
    while i < len(data):
        assert data[i] == 0x11
        r, c = data[i + 1], data[i + 2]
        j = data.find(b"\x11", i + 3)
        j = len(data) if j < 0 else j
        out[(r, c)] = data[i + 3:j]
        i = j
    return out


class FakeHost:
    """Answers each Enter/F-key with the next screen from `script(fields, aid)`."""

    def __init__(self, s: Session, script):
        self.s, self.script, self.sent = s, script, []

    def send_record(self, data, opcode, flags=0):
        self.sent.append(data)
        if opcode != T.OP_PUT_GET or len(data) < 3:
            return
        nxt = self.script(decode_fields(data), data[2])
        if nxt is not None:
            self.s._handle_record(T.Record(0, 0, T.OP_PUT_GET, nxt))
            self.s.host_seq += 1
            self.s.last_host_time = 0.0

    def close(self):
        pass


def order_app(fields, aid):
    cmd = fields.get((20, 7), b"").decode("cp037").strip()
    if cmd == "ORDERS":
        return entry()
    if aid == 0x33:                                       # F3
        return menu()
    cust = fields.get((4, 13), b"").decode("cp037").strip()
    if cust == "BAD":
        return b"\x04\x21" + b"\x22" + E("Customer not found.") + READ
    if cust:
        return done(4711)
    return menu()


def make_session(script=order_app, first=menu):
    s = Session("main", "example", 992)
    s.conn = FakeHost(s, script)
    s.connected = True
    s._handle_record(T.Record(0, 0, T.OP_PUT_GET, first()))
    s.host_seq += 1
    return s


def flowset(text: str) -> FlowSet:
    fs = FlowSet()
    fs.add_doc(yaml.safe_load(textwrap.dedent(text)), "test.yaml")
    fs.validate()
    return fs


ORDERS = """
toolset: orders
flows:
  open_orders:
    description: Open order entry.
    expose: false
    steps:
      - expect: "Demo Main Menu"
        fill: {"Selection or command": "ORDERS"}
        press: Enter
        check: {text: "1:ORDER ENTRY"}
  add_order:
    description: Add an order.
    params:
      customer: {type: string, upper: true, pattern: "^[A-Z0-9]{1,8}$"}
      qty: {type: integer, min: -999, max: 9999}
    steps:
      - call: open_orders
      - name: enter
        expect: "1:ORDER ENTRY"
        fill: {Customer: "{customer}", Qty: "{qty}"}
        press: Enter
        check: {text: "created"}
        capture: {order_no: 'Order (\\d+) created'}
      - name: leave
        expect: "Order {order_no} created"
        press: F3
        check: {text: "Demo Main Menu"}
  peek:
    description: Read the menu title.
    read_only: true
    steps:
      - capture: {title: {rows: "1"}}
"""


def runner(fs, s):
    mgr = Manager(Guard())
    mgr.sessions["main"] = s
    return FlowRunner(mgr, fs)


# ------------------------------------------------------------ validation
@pytest.mark.parametrize("bad, msg", [
    ("""flows: {f: {description: x, steps: [{press: Enter}]}}""", "needs expect"),
    ("""flows: {f: {description: x, steps: [{expect: a, fill: {A: "{nope}"}, press: Enter}]}}""",
     "not a parameter"),
    ("""flows: {f: {description: x, steps: [{expect: a, press: F99}]}}""", "unknown key"),
    ("""flows: {a: {description: x, steps: [{call: b}]}, b: {description: y, steps: [{call: a}]}}""",
     "loop"),
    ("""flows: {f: {description: x, steps: [{call: missing}]}}""", "unknown flow"),
    ("""flows: {w: {description: x, steps: [{expect: a, press: Enter}]},
               r: {description: y, read_only: true, steps: [{call: w}]}}""", "isn't read-only"),
    ("""flows: {f: {description: x, params: {p: {type: secret, env: X, default: a}},
               steps: [{expect: a}]}}""", "can't have a default"),
    ("""flows: {f: {steps: [{expect: a}]}}""", "needs a description"),
    ("""flows: {f: {description: x, steps: [{expect: a, fail: boom}]}}""", "needs a when"),
    ("""flows: {f: {description: x, params: {n: {type: integer, default: abc}},
               steps: [{expect: a}]}}""", "default is invalid"),
])
def test_invalid_flows(bad, msg):
    with pytest.raises(FlowConfigError, match=msg):
        flowset(bad)


def test_param_coercion():
    fs = flowset(ORDERS)
    p = fs.flows["add_order"].params
    assert p["customer"].coerce("c100") == "C100"
    assert p["qty"].coerce("-3") == "-3" and p["qty"].coerce(25) == "25"
    for bad in ("abc", "1.5", 10000, True):
        with pytest.raises(ValueError):
            p["qty"].coerce(bad)
    with pytest.raises(ValueError, match="pattern"):
        p["customer"].coerce("TOO LONG NAME")
    fs2 = flowset("""
      flows: {f: {description: x, params: {
        amt: {type: number, decimals: 2, min: 0}, ok: {type: boolean, true_value: "1",
        false_value: "0"}, kind: {type: string, enum: [A, B]}}, steps: [{expect: a}]}}""")
    q = fs2.flows["f"].params
    assert q["amt"].coerce("12.50") == "12.5"
    with pytest.raises(ValueError, match="decimals"):
        q["amt"].coerce("1.234")
    assert q["ok"].coerce(True) == "1" and q["ok"].coerce("no") == "0"
    with pytest.raises(ValueError, match="one of"):
        q["kind"].coerce("C")


# ------------------------------------------------------------ running
def test_flow_runs_and_captures():
    s = make_session()
    r = runner(flowset(ORDERS), s).run(s, "add_order", {"customer": "c100", "qty": -3})
    assert r.captures == {"order_no": "4711"}
    assert "Demo Main Menu" in r.screen
    # what went to the host for the entry screen: uppercased, signed numeric
    sent = [decode_fields(d) for d in s.conn.sent if len(d) > 3]
    entry_reply = next(f for f in sent if (4, 13) in f)
    assert entry_reply[(4, 13)] == E("C100")
    assert entry_reply[(5, 13)].endswith(b"\xd3")       # -3: zone D on the last digit
    assert [line.split(": ")[1] for line in r.log] == [
        "call open_orders", "pressed Enter", "pressed Enter", "pressed F3"]


def test_flow_stops_on_operator_error_with_screen():
    s = make_session()
    with pytest.raises(FlowError) as ei:
        runner(flowset(ORDERS), s).run(s, "add_order", {"customer": "BAD", "qty": 1})
    e = ei.value
    assert e.flow == "add_order" and e.step == "enter"
    assert "Customer not found" in str(e) and "ORDER ENTRY" in e.screen


def test_flow_refuses_wrong_screen():
    s = make_session(first=entry)
    with pytest.raises(FlowError, match="expected 'Demo Main Menu'"):
        runner(flowset(ORDERS), s).run(s, "add_order", {"customer": "A", "qty": 1})
    assert s.conn.sent == []                             # nothing was typed


def test_bad_params_rejected_before_anything_is_sent():
    s = make_session()
    with pytest.raises(FlowError, match="maximum"):
        runner(flowset(ORDERS), s).run(s, "add_order", {"customer": "A", "qty": 99999})
    with pytest.raises(FlowError, match="no parameter"):
        runner(flowset(ORDERS), s).run(s, "add_order", {"customer": "A", "qty": 1, "x": 1})
    assert s.conn.sent == []


def test_when_skip_and_fail():
    fs = flowset("""
      flows:
        f:
          description: x
          steps:
            - {when: "No such screen", press: Enter}
            - {when: "Demo Main Menu", fail: "menu is not allowed here"}
    """)
    s = make_session()
    with pytest.raises(FlowError, match="menu is not allowed"):
        runner(fs, s).run(s, "f", {})


def test_blocked_command_inside_flow():
    fs = flowset("""
      flows:
        f:
          description: x
          params: {cmd: {type: string}}
          steps: [{expect: "Demo Main Menu", fill: {"Selection or command": "{cmd}"},
                   press: Enter}]
    """)
    s = make_session()
    with pytest.raises(FlowError, match="blocked command"):
        runner(fs, s).run(s, "f", {"cmd": "PWRDWNSYS"})


def test_secret_param_masked_and_not_a_tool(tmp_path):
    fs = flowset("""
      flows:
        login:
          description: sign on
          params: {pw: {type: secret, env: DEMO_PW}, user: {type: string}}
          steps:
            - {expect: "Welcome", fill: {"user": "{user}", "#2": "{pw}"}, press: Enter}
    """)
    assert fs.select() == []                             # secrets never become MCP tools
    mgr = Manager(Guard(), log_dir=str(tmp_path))
    s = Session("main", "example", 992, log=mgr._logger("main"))
    s.conn = FakeHost(s, lambda f, aid: menu())
    s.connected = True
    s._handle_record(T.Record(0, 0, T.OP_PUT_GET, signon_screen()))
    s.host_seq += 1
    mgr.sessions["main"] = s
    r = FlowRunner(mgr, fs, env={"DEMO_PW": "s3cr3t-pw"}.get).run(s, "login", {"user": "bob"})
    assert "Demo Main Menu" in r.screen
    sent = decode_fields(s.conn.sent[-1])
    assert sent[(6, 26)] == E("s3cr3t-pw")
    log = "".join(p.read_text() for p in tmp_path.iterdir())
    assert "s3cr3t-pw" not in log and "***" in log
    with pytest.raises(FlowError, match=r"\$DEMO_PW"):
        FlowRunner(mgr, fs, env={}.get).run(s, "login", {"user": "bob"})


# ------------------------------------------------------------ MCP tools
def tools_of(server):
    async def go():
        async with Client(server) as c:
            return {t.name: t for t in (await c.list_tools()).tools}
    return asyncio.run(go())


def test_flows_become_mcp_tools():
    fs = flowset(ORDERS)
    tools = tools_of(build_server(Manager(Guard()), fs))
    assert {"add_order", "peek", "press", "fill"} <= set(tools)
    assert "open_orders" not in tools                    # expose: false
    t = tools["add_order"]
    props = t.input_schema["properties"]
    assert set(t.input_schema["required"]) == {"customer", "qty"}
    assert props["qty"]["type"] == "integer" and props["qty"]["maximum"] == 9999
    assert "pattern" not in props["customer"]           # uppercased before matching
    assert t.annotations.destructive_hint and not tools["peek"].annotations.destructive_hint
    assert "starts on a screen showing 'Demo Main Menu'" in t.description   # via the call


def test_flows_only_and_read_only_modes():
    fs = flowset(ORDERS)
    only = tools_of(build_server(Manager(Guard()), fs, flows_only=True))
    assert "press" not in only and "fill" not in only and "add_order" in only
    ro = tools_of(build_server(Manager(Guard()), fs, read_only=True))
    assert "peek" in ro and "add_order" not in ro and "press" not in ro
    ts = tools_of(build_server(Manager(Guard()), fs, toolsets=["other"]))
    assert "add_order" not in ts and "get_screen" in ts


def test_flow_tool_call_end_to_end():
    fs = flowset(ORDERS)
    mgr = Manager(Guard())
    mgr.sessions["main"] = make_session()
    server = build_server(mgr, fs)

    async def go():
        async with Client(server) as c:
            ok = await c.call_tool("add_order", {"customer": "c7", "qty": 2})
            bad = await c.call_tool("add_order", {"customer": "c7", "qty": 123456})
            return ok, bad
    ok, bad = asyncio.run(go())
    assert not ok.is_error and "order_no = '4711'" in ok.content[0].text
    assert bad.is_error and "maximum" in bad.content[0].text


def test_builtin_name_clash():
    fs = flowset("""flows: {press: {description: x, steps: [{expect: a}]}}""")
    with pytest.raises(FlowConfigError, match="built-in"):
        build_server(Manager(Guard()), fs)


# ------------------------------------------------------------ Python API
def test_terminal_signon_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("T5250_USER", "bob")
    monkeypatch.setenv("T5250_PASSWORD", "pw-123456")
    t = Terminal("example", log_dir=str(tmp_path), flows=flowset(ORDERS))
    seen = {}

    def host(fields, aid):
        seen.update(fields)
        return menu()
    s = Session("main", "example", 992, log=t.mgr._logger("main"))
    s.conn = FakeHost(s, host)
    s.connected = True
    s._handle_record(T.Record(0, 0, T.OP_PUT_GET, signon_screen()))
    s.host_seq += 1
    t.mgr.sessions["main"] = t.s = s
    assert t.on_signon()
    t.signon()
    assert seen[(5, 26)] == E("BOB") and seen[(6, 26)] == E("pw-123456")
    assert t.on("Demo Main Menu")
    assert "pw-123456" not in "".join(p.read_text() for p in tmp_path.iterdir())
    s.conn.script = order_app
    assert t.run("add_order", customer="x1", qty=5).captures["order_no"] == "4711"


def test_cli_check(tmp_path, capsys):
    good = tmp_path / "orders.yaml"
    good.write_text(textwrap.dedent(ORDERS))
    with pytest.raises(SystemExit) as ei:
        cli_main(["check", str(tmp_path)])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "add_order(customer, qty)" in out and "helper" in out
    (tmp_path / "broken.yaml").write_text("flows: {f: {description: x, steps: [{press: Enter}]}}")
    with pytest.raises(SystemExit) as ei:
        cli_main(["check", str(tmp_path)])
    assert ei.value.code == 2


def test_example_flows_are_valid():
    fs = FlowSet.load(["examples/flows"])
    assert {"data_entry_test", "sql_select", "add_record"} <= set(fs.flows)
    names = {f.name for f in fs.select()}
    assert "run_command" not in names and "sql_select" in names
    sq = fs.flows["sql_select"].params["statement"]
    sq.coerce("SELECT * FROM QSYS2.SYSTABLES")
    for bad in ("DELETE FROM X", "SELECT 1; DROP TABLE X"):
        with pytest.raises(ValueError):
            sq.coerce(bad)
    json.dumps([f.summary() for f in fs.flows.values()])


def test_refused_as_a_whole_before_any_key():
    """A blocked value or a bad parameter in a later step or a called flow is
    caught before the first key is sent."""
    fs = flowset(ORDERS + """
  report:
    description: Open orders, then run a command.
    params: {cmd: {type: string}, qty: {type: integer, default: 1}}
    steps:
      - call: open_orders
      - {expect: "ORDER ENTRY", fill: {Customer: "{cmd}"}, press: Enter}
  nested:
    description: Calls add_order with a bad quantity.
    steps:
      - {call: add_order, with: {customer: A, qty: 123456}}
""")
    s = make_session()
    with pytest.raises(FlowError, match="refused before starting.*blocked command"):
        runner(fs, s).run(s, "report", {"cmd": "QCMDEXC"})
    with pytest.raises(FlowError, match="maximum"):
        runner(fs, s).run(s, "nested", {})
    assert s.conn.sent == []
