"""Automatic reconnect and the flow recorder."""

import time

import pytest
import yaml

from tn5250_mcp import telnet as T
from tn5250_mcp.flows import FlowRunner, FlowSet
from tn5250_mcp.guard import Guard
from tn5250_mcp.manager import GuardError, Manager, render
from tn5250_mcp.recorder import screen_title
from tn5250_mcp.session import Session

from test_flows import E, FakeHost, decode_fields, entry, menu, order_app, signon_screen


# ------------------------------------------------------------ reconnect
class Net:
    """Stands in for the network: Session.connect attaches a FakeHost and the
    host's first screen (sign-on), or fails while `down` > 0."""

    def __init__(self, monkeypatch, down=0):
        self.down, self.attempts = down, 0
        net = self

        def connect(s, timeout=20.0):
            net.attempts += 1
            if net.down > 0:
                net.down -= 1
                raise ConnectionRefusedError("host not reachable")
            s.conn = FakeHost(s, order_app)
            s.connected = True
            with s.cond:
                s._handle_record(T.Record(0, 0, T.OP_PUT_GET, signon_screen()))
                s.host_seq += 1
                s.cond.notify_all()
        monkeypatch.setattr(Session, "connect", connect)


def drop(s, why="ConnectionResetError: reset by peer"):
    """What the reader thread does when the host closes the connection."""
    with s.cond:
        s.last_error = why
        s.connected = False
        s.cond.notify_all()
    s.on_drop(s)


def test_reconnects_after_a_drop(monkeypatch):
    net = Net(monkeypatch, down=2)                # two failed attempts, then up
    mgr = Manager(Guard(), reconnect_attempts=5, reconnect_interval=0.05)
    net.down = 0
    s = mgr.connect("main", "example", 992, True, True, None, False, None, 5)
    net.down = 2
    drop(s)
    time.sleep(0.02)
    assert s.reconnecting and "attempt" in s.reconnecting
    with pytest.raises(GuardError, match="reconnecting"):
        mgr.press(s, "Enter", expect="Welcome")
    assert s.wait(timeout=5)                       # wait covers the reconnect
    assert s.connected and s.reconnecting is None and s.reconnects == 1
    assert net.attempts == 1 + 3
    text = render(s.snapshot())
    assert "reconnected at" in text and "Welcome to DEMO" in text


def test_gives_up_after_the_attempts(monkeypatch):
    net = Net(monkeypatch)
    mgr = Manager(Guard(), reconnect_attempts=3, reconnect_interval=0.01)
    s = mgr.connect("main", "example", 992, True, True, None, False, None, 5)
    net.down = 99
    drop(s)
    for _ in range(200):
        if not s.reconnecting and "gave up" in (s.last_error or ""):
            break
        time.sleep(0.01)
    assert "gave up reconnecting after 3 attempts" in s.last_error
    assert s.snapshot()["keyboard"] == "disconnected"


def test_no_reconnect_after_disconnect_or_when_off(monkeypatch):
    net = Net(monkeypatch)
    mgr = Manager(Guard(), reconnect_attempts=3, reconnect_interval=0.01)
    s = mgr.connect("main", "example", 992, True, True, None, False, None, 5)
    mgr.disconnect("main")
    mgr._dropped(s)                                # a late reader error after disconnect
    time.sleep(0.1)
    assert net.attempts == 1 and not s.connected
    off = Manager(Guard(), reconnect_attempts=0)
    s2 = off.connect("x", "example", 992, True, True, None, False, None, 5)
    drop(s2)
    time.sleep(0.05)
    assert net.attempts == 2 and s2.reconnecting is None


def test_viewer_reconnect_button(monkeypatch):
    net = Net(monkeypatch)
    mgr = Manager(Guard(), reconnect_attempts=0)
    s = mgr.connect("main", "example", 992, True, True, None, False, None, 5)
    drop(s)
    mgr.reconnect_now(s, timeout=2)
    assert s.connected and s.reconnects == 1 and net.attempts == 2


# ------------------------------------------------------------ recorder
def fresh(first=menu, script=order_app):
    s = Session("main", "example", 992)
    s.conn = FakeHost(s, script)
    s.connected = True
    s._handle_record(T.Record(0, 0, T.OP_PUT_GET, first()))
    s.host_seq += 1
    return s


def test_screen_title():
    assert screen_title([" MAIN    Demo Main Menu", ""]) == "1:Demo Main Menu"
    assert screen_title(["", " Work with Objects        11/16/26  19:01:22"]) == \
        "2:Work with Objects"
    assert screen_title(["", "", "", "    Order 4711 created"]) == "4:Order 4711 created"


def test_record_and_replay(tmp_path):
    mgr = Manager(Guard(), record_dir=str(tmp_path))
    s = fresh()
    mgr.sessions["main"] = s
    mgr.record_start(s)
    # the agent works through the screens by hand ...
    mgr.press(s, "Enter", {"Selection or command": "ORDERS"}, expect="Demo Main Menu")
    mgr.fill(s, {"Customer": "C100"}, expect="ORDER ENTRY")      # fill, then press
    mgr.press(s, "Enter", {"#2": "-3"}, expect="ORDER ENTRY")
    mgr.press(s, "F3", expect="created")
    assert s.snapshot()["recording"] == 3
    assert "RECORDING a flow: 3 steps" in render(s.snapshot())
    text, path = mgr.record_stop(s, "add_order", "Add an order.")
    assert path == str(tmp_path / "add_order.yaml") and (tmp_path / "add_order.yaml").exists()
    assert s.snapshot()["recording"] is None
    doc = yaml.safe_load(text)
    flow = doc["flows"]["add_order"]
    assert flow["params"] == {
        "selection_or_command": {"type": "string", "max_length": 60, "default": "ORDERS"},
        "customer": {"type": "string", "max_length": 8, "default": "C100"},
        "qty": {"type": "integer", "default": -3}}
    st = flow["steps"]
    assert st[0]["expect"] == "1:Demo Main Menu" and st[0]["check"] == {"text": "1:ORDER ENTRY"}
    assert st[1]["fill"] == {"Customer": "{customer}", "Qty": "{qty}"}   # by label
    assert st[2]["press"] == "F3" and st[2]["check"] == {"text": "1:Demo Main Menu"}
    # ... and the recording replays, with other values
    fs = FlowSet.load([str(tmp_path)])
    s2 = fresh()
    mgr.sessions["main"] = s2
    r = FlowRunner(mgr, fs).run(s2, "add_order", {"customer": "Z9", "qty": 7})
    sent = [decode_fields(d) for d in s2.conn.sent if len(d) > 3]
    assert next(f for f in sent if (4, 13) in f)[(4, 13)] == E("Z9")
    assert "Demo Main Menu" in r.screen
    with pytest.raises(GuardError, match="already exists"):
        mgr.record_start(s2)
        mgr.press(s2, "Enter", expect="Demo Main Menu")
        mgr.record_stop(s2, "add_order", "again")


def test_passwords_are_never_recorded():
    mgr = Manager(Guard())
    s = fresh(first=signon_screen, script=lambda f, aid: menu())
    mgr.sessions["main"] = s
    mgr.record_start(s)
    mgr.press(s, "Enter", {"5,26": "bob", "6,26": "hunter2-secret"}, from_viewer=True)
    rec = mgr.recorders["main"]
    assert "hunter2-secret" not in repr(rec.steps)
    text, path = mgr.record_stop(s, "sign_on", "Sign on.", save=True)   # no record dir
    assert path is None and "hunter2-secret" not in text
    flow = yaml.safe_load(text)["flows"]["sign_on"]
    assert flow["params"]["password"]["type"] == "secret"
    assert "{password}" in flow["steps"][0]["fill"].values()
    assert flow["params"]["your_user_name"]["default"] == "bob"


def test_literal_values_reset_and_discard():
    mgr = Manager(Guard(require_check=False))
    s = fresh(first=entry, script=order_app)
    mgr.sessions["main"] = s
    mgr.record_start(s)
    mgr.press(s, "Enter", {"Customer": "BAD", "Qty": "1"})        # host answers with an error
    mgr.press(s, "Reset")
    text, _ = mgr.record_stop(s, "bad_customer", "Shows the error.", params="none")
    st = yaml.safe_load(text)["flows"]["bad_customer"]["steps"]
    assert st[0]["fill"] == {"Customer": "BAD", "Qty": "1"}
    assert st[0]["check"] == {"keyboard": "error"}
    assert st[1] == {"name": "step 2", "press": "Reset"}
    mgr.record_start(s)
    assert mgr.record_stop(s, None) == (None, None)
    with pytest.raises(GuardError, match="not being recorded"):
        mgr.record_stop(s, "x")
    mgr.record_start(s)
    with pytest.raises(GuardError, match="nothing was recorded"):
        mgr.record_stop(s, "empty", "")


def test_record_over_mcp_becomes_a_tool_at_once(tmp_path):
    import asyncio
    from mcp.client import Client
    from tn5250_mcp.server import build_server
    mgr = Manager(Guard(), record_dir=str(tmp_path))
    mgr.sessions["main"] = fresh()
    server = build_server(mgr, None)

    async def go():
        async with Client(server) as c:
            await c.call_tool("record_start", {})
            await c.call_tool("press", {"key": "Enter", "expect": "Demo Main Menu",
                                        "values": {"Selection or command": "ORDERS"}})
            await c.call_tool("press", {"key": "F3", "expect": "ORDER ENTRY"})
            out = await c.call_tool("record_stop", {"name": "open_and_close",
                                                    "description": "Open order entry, leave."})
            names = {t.name for t in (await c.list_tools()).tools}
            again = await c.call_tool("open_and_close", {"selection_or_command": "ORDERS"})
            clash = await c.call_tool("record_stop", {"name": "press"})
            return out, names, again, clash
    out, names, again, clash = asyncio.run(go())
    assert not out.is_error and "available now as the tool open_and_close" in out.content[0].text
    assert "open_and_close" in names and (tmp_path / "open_and_close.yaml").exists()
    assert not again.is_error and "flow open_and_close finished" in again.content[0].text
    assert clash.is_error and "already a tool" in clash.content[0].text


def test_viewer_recording_becomes_a_tool(tmp_path):
    import asyncio
    import json
    import urllib.request
    from mcp.client import Client
    from tn5250_mcp.server import build_server
    from tn5250_mcp.viewer import start_viewer
    mgr = Manager(Guard(), record_dir=str(tmp_path))
    url = start_viewer(mgr, 0)
    base, token = url.split("/?token=")
    mgr.sessions["main"] = fresh()
    server = build_server(mgr, None)

    def post(path, body):
        req = urllib.request.Request("%s%s?token=%s" % (base, path, token),
                                     data=json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            return json.load(e)
    assert post("/api/record/start", {"session": "main"})["ok"]
    post("/api/press", {"session": "main", "key": "Enter",
                        "values": {"Selection or command": "ORDERS"}})
    assert post("/api/record/stop", {"session": "main", "name": "press"})["ok"] is False
    r = post("/api/record/stop", {"session": "main", "name": "open_orders_v",
                                  "description": "Open order entry."})
    assert r["ok"] and r["tool"] and r["path"].endswith("open_orders_v.yaml")

    async def names():
        async with Client(server) as c:
            return {t.name for t in (await c.list_tools()).tools}
    assert "open_orders_v" in asyncio.run(names())
