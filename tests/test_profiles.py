"""Saved connection profiles."""

import asyncio
import time

import pytest
import yaml
from mcp.client import Client

from tn5250_mcp.flows import FlowRunner, FlowSet
from tn5250_mcp.guard import Guard
from tn5250_mcp.manager import Manager, render
from tn5250_mcp.profiles import ProfileError, ProfileSet
from tn5250_mcp.server import build_server
from tn5250_mcp.terminal import Terminal

from test_flows import flowset
from test_reconnect_record import Net, drop

SIGNON = flowset("""
flows:
  signon:
    description: sign on
    params:
      user: {type: string, upper: true}
      password: {type: secret, env: DEMO_PW}
    steps:
      - {expect: "Welcome", fill: {user: "{user}", Password: "{password}"}, press: Enter,
         check: {text: "Demo Main Menu"}}
""")


def profiles(text):
    ps = ProfileSet()
    ps.add_doc(yaml.safe_load(text), "test.yaml")
    return ps


DEMO = """
profiles:
  demo:
    description: the demo host
    host: demo.example.com
    device_name: "AGENT*"
    after_connect: {flow: signon, with: {user: bob}}
    on_reconnect: true
  plain:
    host: plain.example.com
    tls: false
"""


@pytest.mark.parametrize("bad, msg", [
    ("profiles: {p: {host: h, password: x}}", "never hold passwords"),
    ("profiles: {p: {host: h, colour: red}}", "unknown keys"),
    ("profiles: {p: {port: 23}}", "needs a host"),
    ("profiles: {p: {host: h, port: 99999}}", "port"),
    ("profiles: {p: {host: h, device_name: TOOLONGNAME1}}", "device_name"),
    ("profiles: {p: {host: h, codepage: cp9999}}", "unknown code page"),
    ("profiles: {p: {host: h, on_reconnect: true}}", "needs an after_connect"),
    ("profiles: {p: {host: h, after_connect: {with: {}}}}", "after_connect is a flow"),
    ("profiles: {P: {host: h}}", "lower_snake_case"),
    ("hosts: {}", "profiles:"),
])
def test_invalid_profiles(bad, msg):
    with pytest.raises(ProfileError, match=msg):
        profiles(bad)


def test_defaults_device_names_and_flow_checks():
    ps = profiles(DEMO)
    d, p = ps.get("demo"), ps.get("plain")
    assert (d.port, d.tls, p.port, p.tls) == (992, True, 23, False)
    assert [d.next_device_name() for _ in range(3)] == ["AGENTA", "AGENTB", "AGENTC"]
    assert p.next_device_name() is None
    with pytest.raises(ProfileError, match="no profile 'nope'"):
        ps.get("nope")
    with pytest.raises(ProfileError, match="no such flow is loaded"):
        ps.validate_flows(FlowSet())
    mgr = Manager(Guard())
    ps.validate_flows(SIGNON, FlowRunner(mgr, SIGNON))
    bad = profiles(DEMO.replace("{user: bob}", "{user: bob, shoe: 9}"))
    with pytest.raises(ProfileError, match="no parameter shoe"):
        bad.validate_flows(SIGNON, FlowRunner(mgr, SIGNON))


def call(server, *calls):
    async def go():
        async with Client(server) as c:
            return [await c.call_tool(n, a) for n, a in calls]
    return asyncio.run(go())


def test_connect_with_profile_signs_on(monkeypatch):
    Net(monkeypatch)
    monkeypatch.setenv("DEMO_PW", "pw-1")
    mgr = Manager(Guard())
    server = build_server(mgr, SIGNON, profiles=profiles(DEMO))
    ok, both, lst = call(server, ("connect", {"profile": "demo"}),
                         ("connect", {"profile": "demo", "host": "x", "session": "two"}),
                         ("list_profiles", {}))
    assert not ok.is_error, ok.content[0].text
    text = ok.content[0].text
    assert text.startswith("signon (profile demo) finished") and "Demo Main Menu" in text
    s = mgr.sessions["main"]
    assert s.device_name == "AGENTA" and s.profile.name == "demo"
    assert both.is_error and "not both" in both.content[0].text
    assert "demo: demo.example.com:992, TLS, device AGENT*" in lst.content[0].text


def test_after_connect_failure_keeps_the_session(monkeypatch):
    Net(monkeypatch)
    monkeypatch.delenv("DEMO_PW", raising=False)
    mgr = Manager(Guard())
    server = build_server(mgr, SIGNON, profiles=profiles(DEMO))
    (r,) = call(server, ("connect", {"profile": "demo"}))
    assert not r.is_error and "did not finish" in r.content[0].text
    assert mgr.sessions["main"].connected and "Welcome to DEMO" in r.content[0].text


def test_profiles_only(monkeypatch):
    Net(monkeypatch)
    server = build_server(Manager(Guard()), SIGNON, profiles=profiles(DEMO), profiles_only=True)
    (r,) = call(server, ("connect", {"host": "anything.example.com"}))
    assert r.is_error and "only connects through saved profiles: demo, plain" in r.content[0].text


def test_signs_on_again_after_a_reconnect(monkeypatch):
    net = Net(monkeypatch)
    monkeypatch.setenv("DEMO_PW", "pw-1")
    mgr = Manager(Guard(), reconnect_attempts=3, reconnect_interval=0.01)
    mgr.flow_runner = FlowRunner(mgr, SIGNON)
    s, note = mgr.connect_profile("main", profiles(DEMO).get("demo"))
    assert "finished" in note
    drop(s)
    for _ in range(300):
        if s.reconnect_note and not s.reconnect_note.startswith("running"):
            break
        time.sleep(0.01)
    assert s.reconnect_note == "signon (profile demo) finished"
    assert s.device_name == "AGENTA" and net.attempts == 2      # same device name again
    text = render(s.snapshot())
    assert "signon (profile demo) finished" in text and "Demo Main Menu" in text


def test_terminal_from_profile(monkeypatch, tmp_path):
    Net(monkeypatch)
    monkeypatch.setenv("DEMO_PW", "pw-1")
    f = tmp_path / "profiles.yaml"
    f.write_text(DEMO)
    t = Terminal.from_profile("demo", str(f), flows=SIGNON)
    t.connect()
    assert t.on("Demo Main Menu") and t.session.device_name == "AGENTA"
    t.close()


def test_example_profiles_are_valid():
    ps = ProfileSet.load(["examples/profiles.yaml"])
    fs = FlowSet.load(["examples/flows"])
    ps.validate_flows(fs, FlowRunner(Manager(Guard()), fs))
    assert set(ps.profiles) == {"pub400", "dev"}
