"""The PASE shell and source members (offline: the fake jt400 helper runs the
PASE wrapper with the local sh, and keeps members in memory)."""

import asyncio
import json

import pytest
from mcp.client import Client

from tn5250_mcp.guard import Guard
from tn5250_mcp.hostservers import (HostServerError, HostServers, pase_runner, source_dates,
                                    split_member)
from tn5250_mcp.manager import Manager
from tn5250_mcp.server import build_server

from test_hostservers import FAKE


@pytest.fixture
def hs(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_IFS", str(tmp_path))
    (tmp_path / "home" / "BOB").mkdir(parents=True)
    h = HostServers(command=FAKE, guard=Guard(), allow_shell=True, allow_commands=True,
                    member_write_libs=["BOBLIB"])
    h.connect("main", "pub", "bob", "right-pw")
    h.root = tmp_path
    yield h
    h.close()


# ------------------------------------------------------------ PASE
def test_pase(hs):
    r = hs.pase("main", "echo hello; echo oops >&2; exit 3")
    assert (r["exit_code"], r["stdout"], r["stderr"], r["timed_out"]) == (3, "hello\n", "oops\n",
                                                                         False)
    assert not list((hs.root / "tmp").iterdir())           # the work folder is gone
    r = hs.pase("main", "pwd", cwd="/home/BOB")
    assert r["stdout"].strip().endswith("/home/BOB") and r["cwd"] == "/home/BOB"
    r = hs.pase("main", "ls", cwd="/nope")
    assert r["exit_code"] == 126 and "run.sh" not in r["stderr"]
    r = hs.pase("main", "printf 'caf\\303\\251'")
    assert r["stdout"] == "café"


def test_pase_timeout(hs):
    r = hs.pase("main", "echo start; sleep 20; echo never", timeout=1)
    assert r["timed_out"] and r["stdout"] == "start\n" and r["exit_code"] != 0


def test_pase_rules(hs):
    with pytest.raises(HostServerError, match="blocked command 'PWRDWNSYS'"):
        hs.pase("main", "system PWRDWNSYS")
    with pytest.raises(HostServerError, match="1 to 3600"):
        hs.pase("main", "ls", timeout=0)
    hs.allow_shell = False
    with pytest.raises(HostServerError, match="--allow-shell"):
        hs.pase("main", "ls")


def test_pase_runner_quotes_the_folder():
    text = pase_runner("/tmp/t5250-x", "/home/it's here", 5)
    assert "cd '/home/it'\\''s here'" in text and "sleep 5" in text


# ------------------------------------------------------------ members
def test_split_member():
    assert split_member(" mylib/qrpglesrc(hello) ") == ("MYLIB", "QRPGLESRC", "HELLO")
    for bad in ["MYLIB/HELLO", "QRPGLESRC(HELLO)", "A/B(C D)", "TOOLONGNAME1/F(M)"]:
        with pytest.raises(HostServerError, match="LIB/FILE\\(MEMBER\\)"):
            split_member(bad)


def test_source_dates():
    old = [(240101, "a"), (240102, "b"), (240103, "c")]
    assert source_dates(old, ["a", "B", "c", "d"], 261116) == ([
        (240101, "a"), (261116, "B"), (240103, "c"), (261116, "d")], 2)
    assert source_dates([(261116, "x")], ["x"], 261116) == ([(261116, "x")], 0)


def test_members(hs):
    assert hs.source_files("main", "boblib")["rows"] == [["QRPGLESRC", None, "100"]]
    assert "HELLO" in [r[0] for r in hs.members("main", "BOBLIB", "QRPGLESRC")["rows"]]
    r = hs.member_read("main", "boblib/qrpglesrc(hello)")
    assert r["member"] == "BOBLIB/QRPGLESRC(HELLO)" and r["lines"][1][2] == "dsply 'hi';"
    w = hs.member_write("main", "BOBLIB/QRPGLESRC(HELLO)",
                        "**free\r\ndsply 'hello';\r\n*inlr = *on;\r\n")
    assert (w["created"], w["lines"], w["changed_lines"]) == (False, 3, 1)
    dates = [d for _, d, _ in hs.member_read("main", "BOBLIB/QRPGLESRC(HELLO)")["lines"]]
    assert dates[0] == dates[2] == 240101 and dates[1] != 240101
    w = hs.member_write("main", "BOBLIB/QRPGLESRC(NEW)", "**free\n", "rpgle", "a new one")
    assert w["created"] and w["changed_lines"] == 1
    with pytest.raises(HostServerError, match="line 1 is 101 characters"):
        hs.member_write("main", "BOBLIB/QRPGLESRC(NEW)", "x" * 101)
    with pytest.raises(HostServerError, match="ADDPFM failed: CPF7301"):
        hs.member_write("main", "BOBLIB/NOFILE(X)", "x")
    with pytest.raises(HostServerError, match="can't read BOBLIB/QRPGLESRC\\(NOPE\\)"):
        hs.member_read("main", "BOBLIB/QRPGLESRC(NOPE)")
    with pytest.raises(HostServerError, match="only in BOBLIB, not OTHER"):
        hs.member_write("main", "OTHER/QRPGLESRC(X)", "x")
    hs.member_write_libs = []
    with pytest.raises(HostServerError, match="--allow-member-write"):
        hs.member_write("main", "BOBLIB/QRPGLESRC(X)", "x")


# ------------------------------------------------------------ tools and CLI
def test_tools(hs, monkeypatch):
    monkeypatch.setenv("T5250_PASSWORD", "right-pw")
    server = build_server(Manager(Guard()), hostservers=hs)

    async def go():
        async with Client(server) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            out = []
            for n, a in [("host_connect", {"host": "pub", "user": "bob"}),
                         ("pase", {"command": "echo hi; echo bad >&2; exit 2"}),
                         ("source_members", {"lib": "BOBLIB"}),
                         ("source_members", {"lib": "BOBLIB", "file": "QRPGLESRC"}),
                         ("member_write", {"member": "BOBLIB/QRPGLESRC(T1)",
                                           "text": "**free\nreturn;\n", "type": "RPGLE"}),
                         ("member_read", {"member": "BOBLIB/QRPGLESRC(T1)"}),
                         ("member_read", {"member": "BOBLIB/QRPGLESRC(T1)", "numbers": True})]:
                out.append((await c.call_tool(n, a)).content[0].text)
            return names, out
    names, (_, sh, files, mbrs, wrote, text, numbered) = asyncio.run(go())
    assert {"pase", "source_members", "member_read", "member_write"} <= names
    assert sh == "exit 2\n--- stdout\nhi\n--- stderr\nbad"
    assert "QRPGLESRC" in files and "HELLO" in mbrs
    assert wrote == "created BOBLIB/QRPGLESRC(T1): 2 lines (2 new or changed), 100 wide"
    assert text == "BOBLIB/QRPGLESRC(T1): 2 lines, 100 wide\n**free\nreturn;"
    assert numbered.splitlines()[2].startswith("   2.00 ")
    # off without the options
    off = HostServers(command=FAKE, guard=Guard())
    names = asyncio.run(_names(build_server(Manager(Guard()), hostservers=off)))
    assert not {"pase", "member_write"} & names and {"source_members", "member_read"} <= names
    off.close()


async def _names(server):
    async with Client(server) as c:
        return {t.name for t in (await c.list_tools()).tools}


def test_cli(hs, tmp_path, monkeypatch, capsys):
    import tn5250_mcp.credentials as cred
    import tn5250_mcp.hostservers as hsmod
    from tn5250_mcp.cli import main
    monkeypatch.setattr(cred, "keyring_available", lambda: False)
    monkeypatch.setenv("T5250_PASSWORD", "right-pw")
    real = hsmod.HostServers
    monkeypatch.setattr(hsmod, "HostServers",
                        lambda jar, java=None, **kw: real(command=FAKE, **kw))
    jar = tmp_path / "jt400.jar"
    jar.write_bytes(b"")
    args = ["--host", "pub", "--user", "bob", "--jt400", str(jar)]

    def run(*argv):
        with pytest.raises(SystemExit) as ei:
            main(list(argv) + args)
        return ei.value.code, capsys.readouterr()
    code, out = run("sh", "echo out; echo err >&2; exit 4")
    assert (code, out.out, out.err) == (4, "out\n", "err\n")
    src = tmp_path / "hello.rpgle"
    src.write_text("**free\ndsply 'cli';\n")
    code, out = run("member", "write", "BOBLIB/QRPGLESRC(CLI)", str(src), "--type", "RPGLE")
    assert code == 0 and "created BOBLIB/QRPGLESRC(CLI): 2 lines" in out.err
    code, out = run("member", "read", "BOBLIB/QRPGLESRC(CLI)")
    assert out.out == "**free\ndsply 'cli';\n"
    code, out = run("member", "list", "BOBLIB/QRPGLESRC", "--json")
    assert "CLI" in [r[0] for r in json.loads(out.out)["rows"]]
