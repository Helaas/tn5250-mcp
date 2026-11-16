"""Terminal-side field checks, the viewer password prompt and pacing."""

import asyncio
import json
import threading
import time
import urllib.request

import pytest
from mcp.client import Client

from tn5250_mcp import telnet as T
from tn5250_mcp.flows import FlowConfigError, FlowError, FlowRunner
from tn5250_mcp.guard import Guard
from tn5250_mcp.manager import GuardError, Manager
from tn5250_mcp.server import build_server
from tn5250_mcp.session import FieldError, Session, mod10_ok, mod11_ok
from tn5250_mcp.viewer import start_viewer

from test_flows import E, FakeHost, READ, WTD, decode_fields, flowset, menu, sba, signon_screen


def sf_fcw(ffw, attr, length, fcw=b""):
    return bytes([0x1D, ffw >> 8, ffw & 0xFF]) + fcw + bytes([attr, length >> 8, length & 0xFF])


def checks_screen(header=b"\x00\x00\x00\x18\x00\x00\x00"):
    """ME field, MF field, mod-10 self-check field, alpha-only field."""
    soh = bytes([0x01, len(header)]) + header
    return (WTD + soh + sba(1, 2) + E("CHECKS")
            + sba(3, 1) + E("Name:") + sba(3, 10) + sf_fcw(0x4008, 0x24, 10)       # ME
            + sba(4, 1) + E("Code:") + sba(4, 10) + sf_fcw(0x4007, 0x24, 4)        # MF
            + sba(5, 1) + E("Card:") + sba(5, 10) + sf_fcw(0x4000, 0x24, 11, b"\xb1\x40")
            + sba(6, 1) + E("City:") + sba(6, 10) + sf_fcw(0x4100, 0x24, 10)       # alpha only
            + READ)


def session(first=checks_screen, script=lambda f, aid: menu()):
    s = Session("main", "example", 992)
    s.conn = FakeHost(s, script)
    s.connected = True
    s._handle_record(T.Record(0, 0, T.OP_PUT_GET, first()))
    s.host_seq += 1
    return s


# ------------------------------------------------------------ field checks
def test_check_digit_math():
    assert mod10_ok("79927398713") and not mod10_ok("79927398710")
    assert mod11_ok("12343") and not mod11_ok("12344")     # 4*2+3*3+2*4+1*5=30 -> 11-8=3


def test_mandatory_entry_blocks_enter():
    s = session()
    with pytest.raises(FieldError, match="0007"):
        s.press("Enter")
    assert s.conn.sent == []
    assert s.buf.rowcol(s.buf.user_cursor) == (3, 11)        # cursor put on the field
    s.fill({"Name": "Bob"})
    s.press("Enter")
    assert len(s.conn.sent) == 1


def test_keys_without_checks():
    s = session()
    s.press("Help")                                          # Help, roll keys, Clear: no checks
    s2 = session(lambda: checks_screen(b"\x00\x00\x00\x18\x00\x00\x20"))   # F3 sends no data
    s2.press("F3")
    assert s.conn.sent and s2.conn.sent


def test_mandatory_fill():
    s = session()
    s.fill({"Name": "Bob", "Code": "AB"})
    with pytest.raises(FieldError, match="full length of 4.*0014"):
        s.press("Enter")
    s.fill({"Code": "ABCD"})
    s.press("Enter")
    s = session()
    s.fill({"Name": "Bob", "Code": ""})                     # empty is fine
    s.press("Enter")


def test_self_check_digit():
    s = session()
    s.fill({"Name": "Bob", "Card": "79927398710"})
    with pytest.raises(FieldError, match="check digit.*0015"):
        s.press("Enter")
    s.fill({"Card": "79927398713"})
    s.press("Enter")
    assert decode_fields(s.conn.sent[-1])[(5, 11)] == E("79927398713")


def test_alpha_only():
    s = session()
    with pytest.raises(FieldError, match="0008"):
        s.fill({"City": "Paris 75"})
    s.fill({"City": "St. Malo"})


def test_check_reported_through_manager():
    mgr = Manager(Guard(require_check=False))
    s = session()
    mgr.sessions["main"] = s
    with pytest.raises(GuardError, match="mandatory entry"):
        mgr.press(s, "Enter")


# ------------------------------------------------------------ viewer prompt
SIGNON = """
flows:
  signon:
    description: sign on
    params:
      user: {type: string, upper: true}
      password: {type: secret, env: DEMO_PW, prompt: "Demo password"}
    steps:
      - {expect: "Welcome", fill: {user: "{user}", Password: "{password}"}, press: Enter}
"""


def signon_session(mgr, seen):
    def host(fields, aid):
        seen.update(fields)
        return menu()
    s = Session("main", "example", 992, log=mgr._logger("main"))
    s.conn = FakeHost(s, host)
    s.connected = True
    s._handle_record(T.Record(0, 0, T.OP_PUT_GET, signon_screen()))
    s.host_seq += 1
    mgr.sessions["main"] = s
    return s


def http(url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.load(e)


def test_password_typed_in_viewer(tmp_path):
    mgr = Manager(Guard(), log_dir=str(tmp_path))
    url = start_viewer(mgr, 0)
    mgr.viewer_url = url
    base, token = url.split("/?token=")
    seen = {}
    s = signon_session(mgr, seen)
    http("%s/api/state?token=%s" % (base, token))            # somebody is watching
    runner = FlowRunner(mgr, flowset(SIGNON), env={"DEMO_PW": "from-env"}.get)
    out = {}
    t = threading.Thread(target=lambda: out.update(r=runner.run(s, "signon", {"user": "bob"})))
    t.start()
    for _ in range(100):
        st = http("%s/api/state?token=%s" % (base, token))
        if st["prompt"]:
            break
        time.sleep(0.05)
    assert st["prompt"]["label"] == "Demo password" and "value" not in st["prompt"]
    assert http("%s/api/secret?token=%s" % (base, token),
                {"session": "main", "id": "wrong", "value": "x"})["ok"] is False
    assert http("%s/api/secret?token=%s" % (base, token),
                {"session": "main", "id": st["prompt"]["id"], "value": "typed-pw-1"})["ok"]
    t.join(5)
    assert seen[(6, 26)] == E("typed-pw-1")                  # the viewer's value, not the env's
    assert "typed-pw-1" not in str(out["r"])
    assert "typed-pw-1" not in "".join(p.read_text() for p in tmp_path.iterdir())
    assert http("%s/api/state?token=%s" % (base, token))["prompt"] is None


def test_prompt_cancel_and_nobody_watching():
    mgr = Manager(Guard())
    mgr.viewer_url = "http://127.0.0.1:1/?token=x"
    s = signon_session(mgr, {})
    runner = FlowRunner(mgr, flowset(SIGNON), env={}.get)
    with pytest.raises(FlowError, match="give the user the viewer link"):
        runner.run(s, "signon", {"user": "bob"})
    mgr.viewer_seen["main"] = time.monotonic()
    threading.Timer(0.2, lambda: mgr.answer_prompt(
        "main", mgr.open_prompt("main")["id"], None)).start()
    with pytest.raises(FlowError, match="cancelled"):
        runner.run(s, "signon", {"user": "bob"})
    assert s.conn.sent == []


def test_secret_cant_be_passed_in():
    fs = flowset(SIGNON)
    mgr = Manager(Guard())
    s = signon_session(mgr, {})
    with pytest.raises(FlowError, match="is a secret"):
        FlowRunner(mgr, fs, env={"DEMO_PW": "x"}.get).run(
            s, "signon", {"user": "bob", "password": "guess"})
    with pytest.raises(FlowConfigError, match="can't be passed in"):
        flowset(SIGNON + """
  wrapper:
    description: w
    steps: [{call: signon, with: {user: a, password: b}}]
""")


def test_secret_flows_offered_only_with_viewer():
    fs = flowset(SIGNON)

    def tools(mgr):
        async def go():
            async with Client(build_server(mgr, fs)) as c:
                return {t.name: t for t in (await c.list_tools()).tools}
        return asyncio.run(go())
    assert "signon" not in tools(Manager(Guard()))
    mgr = Manager(Guard())
    mgr.viewer_url = "http://127.0.0.1:1/?token=x"
    t = tools(mgr)["signon"]
    assert set(t.input_schema["properties"]) == {"user", "session"}   # no password parameter
    assert "types in the viewer" in t.description


# ------------------------------------------------------------ pacing
def test_pace():
    fs = flowset("""
flows:
  slow:
    description: two keys, half a second apart
    pace: 0.3
    steps:
      - {expect: "Demo Main Menu", press: Enter}
      - {expect: "Demo Main Menu", press: Enter}
""")
    mgr = Manager(Guard())
    s = session(first=menu)
    mgr.sessions["main"] = s
    t0 = time.monotonic()
    FlowRunner(mgr, fs).run(s, "slow", {})
    assert time.monotonic() - t0 >= 0.6
    with pytest.raises(FlowConfigError, match="pace"):
        flowset("""flows: {f: {description: x, pace: 99, steps: [{expect: a}]}}""")
