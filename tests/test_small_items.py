"""Sign-off on disconnect, the session log tool, device name patterns,
SysReq keyboard handling."""

import asyncio

import pytest
from mcp.client import Client

from tn5250_mcp import telnet as T
from tn5250_mcp.guard import Guard
from tn5250_mcp.manager import GuardError, Manager
from tn5250_mcp.server import build_server
from tn5250_mcp.session import Session

from test_flows import E, FakeHost, READ, WTD, decode_fields, menu, sba, sf, signon_screen
from test_reconnect_record import Net


def sql_screen():
    """STRSQL-like: a ===> line that is not a CL command line."""
    return (WTD + sba(1, 30) + E("Enter SQL Statements") + sba(19, 2)
            + E("Type SQL statement, press Enter.") + sba(20, 2) + E("===>")
            + sba(20, 6) + sf(0x4000, 0x24, 60) + READ)


def work_with():
    """A list panel: 'Parameters or command' above a ===> line."""
    return (WTD + sba(1, 30) + E("Work with Objects") + sba(19, 2) + E("Parameters or command")
            + sba(20, 2) + E("===>") + sba(20, 6) + sf(0x4000, 0x24, 60) + READ)


def session(first, script):
    s = Session("main", "example", 992)
    s.conn = FakeHost(s, script)
    s.connected = True
    s._handle_record(T.Record(0, 0, T.OP_PUT_GET, first()))
    s.host_seq += 1
    return s


def host_signs_off(fields, aid):
    cmd = fields.get((20, 7), b"").decode("cp037").strip()
    return signon_screen() if cmd == "SIGNOFF" else menu()


def test_command_line_detection():
    assert session(menu, host_signs_off).command_line() == "20,7"
    assert session(work_with, host_signs_off).command_line() == "20,7"
    assert session(sql_screen, host_signs_off).command_line() is None
    assert session(signon_screen, host_signs_off).command_line() is None


def test_disconnect_signs_off():
    mgr = Manager(Guard())
    s = session(menu, host_signs_off)
    mgr.sessions["main"] = s
    mgr.memlogs["main"] = __import__("collections").deque()
    assert mgr.disconnect("main", signoff=True) == "signed off"
    assert decode_fields(s.conn.sent[-1])[(20, 7)] == E("SIGNOFF")
    assert not s.connected and "main" not in mgr.sessions


@pytest.mark.parametrize("first, want", [
    (sql_screen, "no command line"),
    (signon_screen, "not signed on"),
])
def test_disconnect_without_a_command_line(first, want):
    mgr = Manager(Guard())
    s = session(first, host_signs_off)
    mgr.sessions["main"] = s
    assert want in mgr.disconnect("main", signoff=True)
    assert s.conn.sent == []                      # nothing typed into the SQL line


def test_sysreq_does_not_lock_the_keyboard():
    s = session(signon_screen, lambda f, aid: None)
    s.press("SysReq")
    assert s.conn.sent[-1] == b"" and s.keyboard_state() == "ready"


def test_device_name_patterns(monkeypatch):
    Net(monkeypatch)
    mgr = Manager(Guard())
    names = [mgr.connect("s%d" % i, "h", 992, True, True, "agent*", False, None, 5).device_name
             for i in range(3)]
    assert names == ["AGENTA", "AGENTB", "AGENTC"]
    r = mgr.connect("r", "h", 992, True, True, "T5=", False, None, 5).device_name
    assert len(r) == 3 and r.startswith("T5")
    with pytest.raises(GuardError, match="device name"):
        mgr.connect("x", "h", 992, True, True, "1BAD", False, None, 5)


def test_session_log_tool(monkeypatch):
    Net(monkeypatch)
    mgr = Manager(Guard(require_check=False))
    server = build_server(mgr)

    async def go():
        async with Client(server) as c:
            await c.call_tool("connect", {"host": "h"})
            await c.call_tool("press", {"key": "Enter",
                                        "values": {"5,26": "bob", "6,26": "pw-xyz-123"}})
            short = await c.call_tool("get_session_log", {"last": 5})
            full = await c.call_tool("get_session_log", {"last": 50, "screens": True})
            await c.call_tool("disconnect", {})
            after = await c.call_tool("get_session_log", {})
            return short, full, after
    short, full, after = asyncio.run(go())
    t = short.content[0].text
    assert "] connect" in t and "] press Enter" in t and "] screen" not in t
    assert "'6,26': '***'" in t and "pw-xyz-123" not in full.content[0].text
    assert "] screen" in full.content[0].text
    assert "] disconnect" in after.content[0].text   # still readable after disconnect
