"""SQL and IFS through the host servers: rules and plumbing, offline (a fake
helper speaks the jt400 helper's protocol). The real Java helper is checked
when $T5250_TEST_JT400 points at a JTOpen jar."""

import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from mcp.client import Client

from tn5250_mcp.guard import Guard
from tn5250_mcp.hostservers import (HostServerError, HostServers, find_java, format_table,
                                    helper_command, norm_ifs_path, under)
from tn5250_mcp.manager import Manager
from tn5250_mcp.server import build_server

FAKE = [sys.executable, str(Path(__file__).with_name("fake_jt400_helper.py"))]


@pytest.fixture
def ifs(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_IFS", str(tmp_path))
    (tmp_path / "home" / "BOB").mkdir(parents=True)
    (tmp_path / "home" / "BOB" / "notes.txt").write_text("hello from the IFS\n")
    return tmp_path


def make(ifs_root=None, **kw):
    logs = []
    hs = HostServers(command=FAKE, guard=Guard(), log=lambda n, k, t: logs.append((n, k, t)),
                     **kw)
    return hs, logs


def test_paths():
    assert norm_ifs_path("/home/BOB//x/./y.txt") == "/home/BOB/x/y.txt"
    for bad in ("home/x", "/home/BOB/../QSYS.LIB", ""):
        with pytest.raises(HostServerError):
            norm_ifs_path(bad)
    assert under("/HOME/bob/x.txt", "/home/BOB") and not under("/home/BOBBY/x", "/home/BOB")
    assert not under("/QOpenSys/Home/x", "/QOpenSys/home")        # case matters there
    assert under("/anything", "/")


def test_connect_and_query(ifs):
    hs, logs = make()
    with pytest.raises(HostServerError, match="CPF2234"):
        hs.connect("main", "pub", "bob", "wrong")
    with pytest.raises(HostServerError, match="no host-server connection"):
        hs.query("main", "SELECT 1 FROM SYSIBM.SYSDUMMY1")
    info = hs.connect("main", "pub", "bob", "right-pw")
    assert info["user"] == "BOB" and hs.helper.version == "fake 1.0"
    res = hs.query("main", "SELECT * FROM T WHERE ID > ?", [0], max_rows=3)
    assert res["truncated"] and res["params"] == [0]
    t = format_table(res)
    assert t.splitlines()[0].split() == ["ID", "NAME", "PRICE"]
    assert "NULL" in t and "more rows not shown" in t
    with pytest.raises(HostServerError, match="read-only.*SQL0817"):  # server-side read-only
        hs.query("main", "DELETE FROM T")
    with pytest.raises(HostServerError, match="blocked command 'QCMDEXC'"):
        hs.query("main", "VALUES QSYS2.QCMDEXC('DSPLIBL')")
    assert all("right-pw" not in str(e) for e in logs)
    assert [k for _, k, _ in logs][:2] == ["host connect", "sql query"]
    hs.close()


def test_sql_write_needs_permission(ifs):
    hs, _ = make()
    hs.connect("main", "pub", "bob", "right-pw")
    with pytest.raises(HostServerError, match="--allow-sql-write"):
        hs.execute("main", "UPDATE T SET X = 1")
    hs.close()
    hs, _ = make(allow_sql_write=True)
    hs.connect("main", "pub", "bob", "right-pw")
    assert format_table(hs.execute("main", "UPDATE T SET X = 1")) == "done (1 row changed)"
    with pytest.raises(HostServerError, match="blocked"):
        hs.execute("main", "DROP SCHEMA BOB")
    hs.close()


def test_ifs(ifs):
    hs, logs = make(ifs_write_roots=["/home/BOB"])
    hs.connect("main", "pub", "bob", "right-pw")
    listing = hs.ifs_list("main", "/home/BOB")
    assert [e["name"] for e in listing["entries"]] == ["notes.txt"]
    assert hs.ifs_read("main", "/home/BOB/notes.txt")["text"] == "hello from the IFS\n"
    r = hs.ifs_read("main", "/home/BOB/notes.txt", max_bytes=5, binary=True)
    assert r["truncated"] and r["base64"] == "aGVsbG8="
    hs.ifs_write("main", "/home/bob/new/out.txt", "line 1\n")        # case-insensitive root
    with pytest.raises(HostServerError, match="exists"):
        hs.ifs_write("main", "/home/BOB/new/out.txt", "again")
    hs.ifs_write("main", "/home/BOB/new/out.txt", "line 2\n", append=True)
    assert (ifs / "home/bob/new/out.txt").read_text() == "line 1\nline 2\n"
    with pytest.raises(HostServerError, match="outside the folders"):
        hs.ifs_write("main", "/QSYS.LIB/X.FILE", "x")
    with pytest.raises(HostServerError, match=r"'\.\.'"):
        hs.ifs_write("main", "/home/BOB/../ALICE/x", "x")
    with pytest.raises(HostServerError, match="base64"):
        hs.ifs_write("main", "/home/BOB/b.bin", "not base64!", binary=True)
    assert not any("line 1" in t for _, _, t in logs)               # contents are not logged
    hs.close()
    off, _ = make()
    off.connect("main", "pub", "bob", "right-pw")
    with pytest.raises(HostServerError, match="--ifs-write-root"):
        off.ifs_write("main", "/home/BOB/x", "x")
    off.close()


def test_helper_restarts_after_a_crash(ifs):
    hs, _ = make()
    hs.connect("main", "pub", "bob", "right-pw")
    with pytest.raises(HostServerError, match="stopped"):
        hs.helper.call("crash")
    with pytest.raises(HostServerError, match="not connected"):    # new process, no connections
        hs.helper.call("query", name="main", sql="SELECT 1", params=[], max_rows=1)
    hs.close()


def test_java_lookup_and_jar_check(tmp_path):
    with pytest.raises(HostServerError, match="jt400 jar not found"):
        helper_command(str(tmp_path / "missing.jar"))
    with pytest.raises(HostServerError, match="not a Java 11"):
        find_java(str(tmp_path / "nojava"))


# ------------------------------------------------------------ MCP tools
def tools_and_calls(hs, mgr, calls, answer=None):
    server = build_server(mgr, hostservers=hs)
    if answer:
        def typist():
            for _ in range(200):
                p = mgr.open_prompt("")
                if p:
                    mgr.answer_prompt(None, p["id"], answer)
                    return
                time.sleep(0.02)
        threading.Thread(target=typist, daemon=True).start()

    async def go():
        async with Client(server) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            return names, [await c.call_tool(n, a) for n, a in calls]
    return asyncio.run(go())


def test_tools_password_from_viewer(ifs):
    hs, logs = make()
    mgr = Manager(Guard())
    mgr.viewer_url = "http://127.0.0.1:1/?token=x"
    mgr.viewer_seen["*"] = time.monotonic() + 60                   # somebody has it open
    names, (con, q) = tools_and_calls(hs, mgr, [
        ("host_connect", {"host": "pub", "user": "bob"}),
        ("sql_query", {"sql": "SELECT * FROM T", "max_rows": 2})], answer="right-pw")
    assert {"host_connect", "sql_query", "ifs_list", "ifs_read"} <= names
    assert not {"sql_execute", "ifs_write"} & names                # off unless allowed
    assert not con.is_error and "connected main: BOB on PUB (V7R5M0, TLS)" in con.content[0].text
    assert "ITEM 1" in q.content[0].text
    assert "right-pw" not in "".join(t for _, _, t in mgr.memlogs.get("host-main", []))
    hs.close()


def test_tools_need_a_password_source(ifs, monkeypatch):
    monkeypatch.delenv("T5250_PASSWORD", raising=False)
    hs, _ = make()
    mgr = Manager(Guard())
    mgr.viewer_url = "http://127.0.0.1:1/?token=x"
    _, (r,) = tools_and_calls(hs, mgr, [("host_connect", {"host": "pub", "user": "bob"})])
    assert r.is_error and "give the user the viewer link" in r.content[0].text
    monkeypatch.setenv("T5250_PASSWORD", "right-pw")               # CI: from the environment
    _, (r,) = tools_and_calls(hs, mgr, [("host_connect", {"host": "pub", "user": "bob"})])
    assert not r.is_error
    hs.close()


def test_write_tools_when_allowed(ifs, monkeypatch):
    monkeypatch.setenv("T5250_PASSWORD", "right-pw")
    hs, _ = make(allow_sql_write=True, ifs_write_roots=["/home/BOB"])
    names, (_, w, x) = tools_and_calls(hs, Manager(Guard()), [
        ("host_connect", {"host": "pub", "user": "bob"}),
        ("ifs_write", {"path": "/home/BOB/a.txt", "content": "hi"}),
        ("sql_execute", {"sql": "UPDATE T SET X = 1"})])
    assert {"sql_execute", "ifs_write"} <= names
    assert "wrote /home/BOB/a.txt: 2 bytes" in w.content[0].text
    assert "done (1 row changed)" in x.content[0].text
    hs.close()


# ------------------------------------------------------------ the real helper
@pytest.mark.skipif(not os.environ.get("T5250_TEST_JT400"),
                    reason="set T5250_TEST_JT400 to a JTOpen jar to check the Java helper")
def test_java_helper_starts():
    cmd = helper_command(os.environ["T5250_TEST_JT400"])
    p = subprocess.run(cmd, input='{"id":1,"op":"ping"}\n{"id":2,"op":"query","name":"x"}\n',
                       capture_output=True, text=True, timeout=120)
    lines = p.stdout.splitlines()
    assert '"ready":true' in lines[0] and '"result":"pong"' in lines[1]
    assert '"ok":false' in lines[2] and "missing sql" in lines[2] or "not connected" in lines[2]
