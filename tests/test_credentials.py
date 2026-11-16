"""Saved passwords (memory and keychain) and expired passwords: one prompt
per user and host, and never a retry with a password the host rejected."""

import asyncio
import sys
import threading
import time
import types

import pytest
from mcp.client import Client

from tn5250_mcp import telnet as T
from tn5250_mcp.credentials import CredentialStore
from tn5250_mcp.flows import FlowError, FlowRunner
from tn5250_mcp.guard import Guard
from tn5250_mcp.hostservers import HostServers
from tn5250_mcp.manager import Manager
from tn5250_mcp.server import build_server
from tn5250_mcp.session import Session

from test_flows import (E, FakeHost, READ, WTD, flowset, menu, sba, sf,
                        signon_screen)
from test_hostservers import FAKE

SIGNON = flowset("""
flows:
  signon:
    description: sign on
    params:
      user: {type: string, upper: true}
      password: {type: secret, env: DEMO_PW, user_param: user}
    steps:
      - {name: sign on, expect: "Welcome", fill: {user: "{user}", Password: "{password}"},
         press: Enter, check: {keyboard: any}}
      - {name: expired, when: "Password has expired", change_password: {secret: password}}
      - {name: refused, when: "Welcome", fail: "sign-on refused"}
""")


class FakeKeyring(types.SimpleNamespace):
    def __init__(self):
        super().__init__(store={})

    def get_password(self, svc, name):
        return self.store.get((svc, name))

    def set_password(self, svc, name, pw):
        self.store[(svc, name)] = pw

    def delete_password(self, svc, name):
        del self.store[(svc, name)]


@pytest.fixture
def keyring(monkeypatch):
    kr = FakeKeyring()
    monkeypatch.setitem(sys.modules, "keyring", kr)
    return kr


def with_keychain(store):
    store.keychain = True
    return store


# ------------------------------------------------------------ the store
def test_memory_cache_and_idle_expiry(monkeypatch):
    c = CredentialStore(idle_hours=1)
    c.put("PUB400.com", "myuser", "pw1")
    assert c.get("pub400.com", "MYUSER") == "pw1" and c.saved() == ["MYUSER@pub400.com"]
    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now + 3601)
    assert c.get("pub400.com", "MYUSER") is None
    off = CredentialStore(idle_hours=0)
    off.put("h", "u", "pw")
    assert off.get("h", "u") is None


def test_keychain_remember_replace_forget(keyring):
    c = with_keychain(CredentialStore())
    c.put("h", "u", "pw1")                        # not asked to remember: memory only
    assert keyring.store == {}
    c.put("h", "u", "pw1", remember=True)
    later = with_keychain(CredentialStore())      # a new server process
    assert later.get("h", "U") == "pw1"
    later.replace("h", "u", "pw2")                # changed: the keychain follows
    assert with_keychain(CredentialStore()).get("h", "u") == "pw2"
    later.invalidate("h", "u")
    assert with_keychain(CredentialStore()).get("h", "u") is None
    c.put("a", "x", "1", remember=True)
    c.put("b", "y", "2", remember=True)
    assert c.forget(host="a") == 1 and c.get("b", "y") == "2"
    assert c.forget() == 2 and keyring.store == {}


# ------------------------------------------------------------ 5250 sign-on flows
def expired_screen(msg=""):
    return (WTD + sba(1, 30) + E("Change Password") + sba(3, 2)
            + E("Password has expired. Password must be changed to continue sign-on request.")
            + sba(6, 2) + E("Current password:") + sba(6, 30) + sf(0x4000, 0x27, 20)
            + sba(7, 2) + E("New password:") + sba(7, 30) + sf(0x4000, 0x27, 20)
            + sba(8, 2) + E("New password (to verify):") + sba(8, 30) + sf(0x4000, 0x27, 20)
            + (sba(24, 2) + E(msg) if msg else b"") + READ)


class Host:
    """Sign-on (5,26 user, 6,26 password) with an optional expired password."""

    def __init__(self, password="pw-old", expired=False):
        self.password, self.expired, self.signons = password, expired, []

    def __call__(self, fields, aid):
        if (6, 26) in fields:                     # sign-on screen
            pw = fields[(6, 26)].rstrip(b"\x00").decode("cp037")
            self.signons.append(pw)
            if pw != self.password:
                return signon_screen()            # refused
            return expired_screen() if self.expired else menu()
        cur, new, ver = (fields.get((r, 31), b"").rstrip(b"\x00").decode("cp037")
                         for r in (6, 7, 8))
        if cur != self.password:
            return expired_screen("CPF22E2 Password not correct for user profile.")
        if len(new) < 6:
            return expired_screen("CPF22E5 Password too short.")
        self.password, self.expired = new, False
        return menu()


def session(mgr, host):
    s = Session("main", "pub400.com", 992, log=mgr._logger("main"))
    s.conn = FakeHost(s, host)
    s.connected = True
    s._handle_record(T.Record(0, 0, T.OP_PUT_GET, signon_screen()))
    s.host_seq += 1
    mgr.sessions["main"] = s
    return s


def typist(mgr, answers):
    """Plays the person at the viewer: answers prompts in order."""
    seen = []

    def run():
        for value in answers:
            for _ in range(500):
                p = mgr.open_prompt("main")
                if p and p["id"] not in [x["id"] for x in seen]:
                    seen.append(p)
                    mgr.answer_prompt(None, p["id"], value)
                    break
                time.sleep(0.01)
    threading.Thread(target=run, daemon=True).start()
    return seen


def viewer_on(mgr):
    mgr.viewer_url = "http://127.0.0.1:1/?token=x"
    mgr.viewer_seen["main"] = time.monotonic() + 3600


def test_one_prompt_then_saved(tmp_path):
    mgr = Manager(Guard(), log_dir=str(tmp_path))
    viewer_on(mgr)
    host = Host()
    run = FlowRunner(mgr, SIGNON, env={}.get)
    s = session(mgr, host)
    typist(mgr, ["pw-old"])
    run.run(s, "signon", {"user": "myuser"})
    assert mgr.creds.get("pub400.com", "MYUSER") == "pw-old"
    mgr.viewer_seen.clear()                       # nobody watching any more
    s = session(mgr, host)
    run.run(s, "signon", {"user": "myuser"})          # no prompt: the saved one
    assert host.signons == ["pw-old", "pw-old"]
    assert "pw-old" not in "".join(p.read_text() for p in tmp_path.iterdir())


def test_rejected_saved_password_is_dropped_not_retried():
    mgr = Manager(Guard())
    mgr.creds.put("pub400.com", "MYUSER", "stale")
    host = Host(password="changed-elsewhere")
    s = session(mgr, host)
    with pytest.raises(FlowError, match="refused"):
        FlowRunner(mgr, SIGNON, env={}.get).run(s, "signon", {"user": "myuser"})
    assert host.signons == ["stale"]               # one attempt only
    assert mgr.creds.get("pub400.com", "MYUSER") is None


def test_expired_password_is_changed_in_the_viewer(tmp_path):
    mgr = Manager(Guard(), log_dir=str(tmp_path))
    viewer_on(mgr)
    changed = []
    mgr.on_password_changed = lambda h, u: changed.append((h, u))
    mgr.creds.put("pub400.com", "MYUSER", "pw-old")
    host = Host(expired=True)
    s = session(mgr, host)
    prompts = typist(mgr, ["short", "pw-new-123"])
    r = FlowRunner(mgr, SIGNON, env={}.get).run(s, "signon", {"user": "myuser"})
    assert "Demo Main Menu" in r.screen and host.password == "pw-new-123"
    assert [p["kind"] for p in prompts] == ["new", "new"]
    assert "too short" in prompts[1]["message"]    # the host's reason, shown to the person
    assert host.signons == ["pw-old"]              # the old one was used exactly once
    assert mgr.creds.get("pub400.com", "MYUSER") == "pw-new-123"
    assert changed == [("pub400.com", "MYUSER")]
    log = "".join(p.read_text() for p in tmp_path.iterdir())
    assert "pw-new-123" not in log and "pw-old" not in log and "short" not in log


def test_expired_without_a_person_stops():
    mgr = Manager(Guard())
    host = Host(expired=True)
    s = session(mgr, host)
    with pytest.raises(FlowError, match="has expired. Nothing was retried"):
        FlowRunner(mgr, SIGNON, env={"DEMO_PW": "pw-old"}.get).run(s, "signon", {"user": "myuser"})
    assert host.signons == ["pw-old"] and host.expired


# ------------------------------------------------------------ host servers (SQL/IFS)
def host_tools(mgr, calls, answers=(), env_extra=None):
    hs = HostServers(command=FAKE, guard=Guard())
    server = build_server(mgr, hostservers=hs)
    seen = []
    if answers:
        def run():
            for value in answers:
                for _ in range(500):
                    p = mgr.open_prompt("")
                    if p and p["id"] not in [x["id"] for x in seen]:
                        seen.append(p)
                        mgr.answer_prompt(None, p["id"], value)
                        break
                    time.sleep(0.01)
        threading.Thread(target=run, daemon=True).start()

    async def go():
        async with Client(server) as c:
            return [await c.call_tool(n, a) for n, a in calls]
    try:
        return asyncio.run(go()), seen
    finally:
        hs.close()


def test_host_connect_reuses_the_signon_password(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_IFS", str(tmp_path))
    monkeypatch.delenv("T5250_PASSWORD", raising=False)
    mgr = Manager(Guard())
    mgr.creds.put("pub", "BOB", "right-pw")        # e.g. from the 5250 sign-on
    (r,), prompts = host_tools(mgr, [("host_connect", {"host": "pub", "user": "bob"})])
    assert not r.is_error and prompts == []


def test_host_connect_stale_saved_password(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_IFS", str(tmp_path))
    mgr = Manager(Guard())
    viewer_on(mgr)
    mgr.viewer_seen["*"] = time.monotonic() + 3600
    mgr.creds.put("pub", "BOB", "stale")
    (r,), prompts = host_tools(mgr, [("host_connect", {"host": "pub", "user": "bob"})],
                               answers=["right-pw"])
    assert not r.is_error and len(prompts) == 1     # dropped, asked once
    assert mgr.creds.get("pub", "BOB") == "right-pw"


def test_host_connect_expired_and_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_IFS", str(tmp_path))
    monkeypatch.setenv("FAKE_EXPIRED", "BOB")
    mgr = Manager(Guard())
    viewer_on(mgr)
    mgr.viewer_seen["*"] = time.monotonic() + 3600
    (r,), prompts = host_tools(mgr, [("host_connect", {"host": "pub", "user": "bob"})],
                               answers=["right-pw", "abc", "brand-new-1"])
    assert not r.is_error, r.content[0].text
    assert [p["kind"] for p in prompts] == ["password", "new", "new"]
    assert "too short" in prompts[2]["message"]
    assert "had expired; it was changed" in r.content[0].text
    assert mgr.creds.get("pub", "BOB") == "brand-new-1"
    monkeypatch.delenv("FAKE_EXPIRED")
    monkeypatch.setenv("FAKE_WARN_DAYS", "3")
    monkeypatch.setenv("T5250_PASSWORD", "right-pw")
    (r,), _ = host_tools(Manager(Guard()), [("host_connect", {"host": "pub", "user": "bob"})])
    assert "expires in 3 days" in r.content[0].text


def test_host_connect_expired_without_a_person(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_IFS", str(tmp_path))
    monkeypatch.setenv("FAKE_EXPIRED", "BOB")
    monkeypatch.setenv("T5250_PASSWORD", "right-pw")
    (r,), _ = host_tools(Manager(Guard()), [("host_connect", {"host": "pub", "user": "bob"})])
    assert r.is_error and "has expired. Nothing was retried" in r.content[0].text


def test_forget_tool_and_viewer(keyring, tmp_path):
    import json
    import urllib.request
    from tn5250_mcp.viewer import start_viewer
    mgr = Manager(Guard(), creds=with_keychain(CredentialStore()))
    mgr.creds.put("pub", "BOB", "x", remember=True)
    mgr.creds.put("dev", "AL", "y")
    url = start_viewer(mgr, 0)
    base, token = url.split("/?token=")
    req = urllib.request.Request("%s/api/forget?token=%s" % (base, token), data=b"{}")
    assert json.load(urllib.request.urlopen(req)) == {"ok": True, "count": 2}
    assert keyring.store == {} and mgr.creds.saved() == []
    mgr.creds.put("pub", "BOB", "x")
    (r,), _ = host_tools(mgr, [("forget_password", {"user": "bob"})])
    assert "forgot 1 saved password" in r.content[0].text


def test_remember_on_this_computer(keyring):
    mgr = Manager(Guard(), creds=with_keychain(CredentialStore()))
    viewer_on(mgr)
    s = session(mgr, Host())

    def person():
        for _ in range(500):
            p = mgr.open_prompt("main")
            if p:
                assert p["remember"] is True        # the checkbox is offered
                mgr.answer_prompt(None, p["id"], "pw-old", remember=True)
                return
            time.sleep(0.01)
    threading.Thread(target=person, daemon=True).start()
    FlowRunner(mgr, SIGNON, env={}.get).run(s, "signon", {"user": "myuser"})
    assert keyring.store[("tn5250-mcp", "MYUSER@pub400.com")] == "pw-old"
    assert with_keychain(CredentialStore()).get("pub400.com", "myuser") == "pw-old"
