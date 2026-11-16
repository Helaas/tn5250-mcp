"""CL commands with a library list, compiles with structured errors, job logs
and spooled files (offline, against the fake jt400 helper)."""

import asyncio
import json

import pytest
from mcp.client import Client

from tn5250_mcp.compiler import CompileError, build_command, parse_events
from tn5250_mcp.guard import Guard
from tn5250_mcp.hostservers import HostServerError, HostServers, check_libl, command_name
from tn5250_mcp.manager import Manager
from tn5250_mcp.server import build_server

from test_hostservers import FAKE

BAD = "**free\ndcl-s total int(10);\ntotal = TOTL + 1;\n*inlr = *on;\n"
GOOD = "**free\ndcl-s total int(10);\ntotal = 1;\n*inlr = *on;\n"


@pytest.fixture
def hs(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_IFS", str(tmp_path))
    (tmp_path / "home" / "BOB").mkdir(parents=True)
    (tmp_path / "home/BOB/bad.rpgle").write_text(BAD)
    (tmp_path / "home/BOB/good.rpgle").write_text(GOOD)
    logs = []
    h = HostServers(command=FAKE, guard=Guard(), allow_commands=True,
                    compile_libs=["BOBLIB"], log=lambda n, k, t: logs.append((k, t)))
    h.connect("main", "pub", "bob", "right-pw", libl=["boblib", "qgpl"])
    h.logs = logs
    yield h
    h.close()


# ------------------------------------------------------------ building blocks
def test_build_command():
    assert build_command("/home/BOB/src/hello.rpgle", "boblib/hello") == (
        "CRTBNDRPG PGM(BOBLIB/HELLO) SRCSTMF('/home/BOB/src/hello.rpgle') OPTION(*EVENTF) "
        "TGTCCSID(*JOB) DBGVIEW(*SOURCE)")
    assert build_command("/x/a.sqlrpgle", "L/A").startswith(
        "CRTSQLRPGI OBJ(L/A) SRCSTMF('/x/a.sqlrpgle') OPTION(*EVENTF) OBJTYPE(*PGM) "
        "CVTCCSID(*JOB)")
    assert "TGTCCSID(*JOB)" not in build_command("/x/a.rpgle", "L/A", options="TGTCCSID(37)")
    assert "TGTCCSID" not in build_command("l/qrpglesrc(a)", "L/A", "rpg")   # members: as is
    assert build_command("boblib/qddssrc(screen1)", "BOBLIB/SCREEN1", "dspf") == (
        "CRTDSPF FILE(BOBLIB/SCREEN1) SRCFILE(BOBLIB/QDDSSRC) SRCMBR(SCREEN1) OPTION(*EVENTF)")
    assert "TGTRLS(*CURRENT)" in build_command("/x/a.clle", "L/A", options="TGTRLS(*CURRENT)")
    for args, msg in [(("/x/a.rpgle", "NOLIB"), "LIB/NAME"),
                      (("/x/a.txt", "L/A"), "source type"),
                      (("/x/a.rpgle", "L/A", None, "OPTION(*SRCDBG)"), "OPTION"),
                      (("/x/../etc/a.rpgle", "L/A"), "bad source path"),
                      (("relative.rpgle", "L/A"), "IFS path")]:
        with pytest.raises(CompileError, match=msg):
            build_command(*args)


def test_parse_events():
    recs = ["TIMESTAMP  0 20261116120001", "PROCESSOR  0 000 1",
            "FILEID     0      001 000000 052 /home/BOB/a.rpgle 20261116120000 0",
            "FILEID     0      002 000003 010 /home/BOB/copy.rpgleinc 20261116120000 0",
            "ERROR      0      002 1 000007 000007 012 000007 015 RNF7030 S 30 049 "
            "The name or indicator TOTL is not defined.",
            "ERROR      0      001 1 000003 000003 001 000003 001 RNF7031 I 00 040 "
            "The name or indicator is not referenced.",
            "ERROR      0 001 0 000000 000000 000 000000 000 RNS9308 T 50 057 Compilation "
            "stopped. Severity 30 errors found in program.",
            "FILEEND    0      001 000010"]
    d = parse_events(recs)
    assert [(x.file, x.line, x.column, x.severity, x.id) for x in d] == [
        ("/home/BOB/copy.rpgleinc", 7, 12, 30, "RNF7030"),
        ("/home/BOB/a.rpgle", 3, 1, 0, "RNF7031"),
        ("/home/BOB/a.rpgle", 0, 0, 50, "RNS9308")]          # the summary goes last
    assert str(d[0]) == ("/home/BOB/copy.rpgleinc:7:12: severe 30 RNF7030 The name or "
                         "indicator TOTL is not defined.")


def test_libl_and_command_name():
    assert check_libl("mylib, qgpl qtemp") == ["MYLIB", "QGPL", "QTEMP"]
    for bad in ([], ["1BAD"], ["A" * 11], "x;y"):
        with pytest.raises(HostServerError):
            check_libl(bad)
    assert command_name("qsys/crtbndrpg pgm(x)") == "CRTBNDRPG"
    assert command_name("DSPLIBL") == "DSPLIBL"


# ------------------------------------------------------------ commands
def test_commands_need_permission_and_respect_lists(hs, tmp_path):
    res = hs.command("main", "DSPLIBL")
    assert res["ok"] and res["libl"] == ["BOBLIB", "QGPL"]      # from the connection
    res = hs.command("main", "DSPLIBL", libl=["other", "qgpl"])
    assert res["libl"] == ["OTHER", "QGPL"]
    with pytest.raises(HostServerError, match="blocked command 'DLTLIB'"):
        hs.command("main", "DLTLIB LIB(BOBLIB)")
    assert not hs.command("main", "NOSUCHCMD")["ok"]
    hs.command_allow = ["DSP*"]
    with pytest.raises(HostServerError, match="not among the commands"):
        hs.command("main", "DLYJOB DLY(1)")
    hs.allow_commands = False
    with pytest.raises(HostServerError, match="--allow-commands"):
        hs.command("main", "DSPLIBL")


# ------------------------------------------------------------ compiling
def test_compile_errors_and_success(hs, tmp_path):
    bad = hs.compile("main", "/home/BOB/bad.rpgle", "BOBLIB/BAD")
    assert not bad["ok"] and bad["object"] == "BOBLIB/BAD"
    assert bad["diagnostics"][0] == {
        "file": "/home/BOB/bad.rpgle", "line": 3, "column": 9, "severity": 30,
        "severity_name": "severe", "id": "RNF7030",
        "text": "The name or indicator TOTL is not defined."}
    assert bad["messages"][0]["id"] == "RNS9310" and bad["job"] == "654321/QUSER/QZRCSRVS"
    good = hs.compile("main", "/home/BOB/good.rpgle", "BOBLIB/GOOD")
    assert good["ok"] and good["diagnostics"] == []
    with pytest.raises(HostServerError, match="only into BOBLIB, not QGPL"):
        hs.compile("main", "/home/BOB/good.rpgle", "QGPL/GOOD")
    missing = hs.compile("main", "/home/BOB/none.rpgle", "BOBLIB/NONE")
    assert not missing["ok"] and "no event file" in missing["note"]


def test_old_event_file_is_not_trusted(hs, monkeypatch):
    hs.compile("main", "/home/BOB/bad.rpgle", "BOBLIB/BAD")
    monkeypatch.setenv("FAKE_EVF_TIME", "20200101000000")   # read when the fake starts
    hs.helper.close()                                  # new process: forget the fake events
    hs.connect("main", "pub", "bob", "right-pw")
    res = hs.compile("main", "/home/BOB/bad.rpgle", "BOBLIB/BAD")
    assert res["diagnostics"] == [] and "earlier compile" in res["note"]


def test_compile_needs_permission(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_IFS", str(tmp_path))
    h = HostServers(command=FAKE, guard=Guard())
    h.connect("main", "pub", "bob", "right-pw")
    with pytest.raises(HostServerError, match="--allow-compile"):
        h.compile("main", "/x/a.rpgle", "L/A")
    h.close()


def test_job_log_and_spooled_files(hs):
    with pytest.raises(HostServerError, match="no command has run"):
        hs.job_log("main")
    hs.command("main", "DSPLIBL")
    log = hs.job_log("main", min_severity=0)
    assert log["job"] == "654321/QUSER/QZRCSRVS" and log["rows"][0][1] == "CPC2206"
    with pytest.raises(HostServerError, match="number/user/name"):
        hs.job_log("main", job="QZRCSRVS")
    listed = hs.spooled_files("main")["rows"][0]
    assert listed == ["QPRTLIBL", "1", "654322/BOB/QPRTJOB"]
    assert hs.spooled_file("main", listed[2], "QPRTLIBL")["lines"][-1] == "TOTAL 42"
    with pytest.raises(HostServerError, match="no spooled file QPRTLIBL.*QPRTJOB"):
        hs.spooled_file("main", "654321/QUSER/QZRCSRVS", "QPRTLIBL")


# ------------------------------------------------------------ MCP tools
def tools(mgr, hs, calls=(), **kw):
    server = build_server(mgr, hostservers=hs, **kw)

    async def go():
        async with Client(server) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            return names, [await c.call_tool(n, a) for n, a in calls]
    return asyncio.run(go())


def test_tools(hs, monkeypatch):
    monkeypatch.setenv("T5250_PASSWORD", "right-pw")
    names, (con, cmd, bad, good, js, log) = tools(Manager(Guard()), hs, [
        ("host_connect", {"host": "pub", "user": "bob", "libl": ["BOBLIB", "QGPL"]}),
        ("run_command", {"command": "DSPLIBL"}),
        ("compile", {"source": "/home/BOB/bad.rpgle", "object": "BOBLIB/BAD"}),
        ("compile", {"source": "/home/BOB/good.rpgle", "object": "BOBLIB/GOOD"}),
        ("compile", {"source": "/home/BOB/bad.rpgle", "object": "BOBLIB/BAD",
                     "format": "json"}),
        ("job_log", {}),
    ])
    assert {"run_command", "compile", "job_log", "spooled_files", "spooled_file"} <= names
    assert "ok: DSPLIBL [libl BOBLIB QGPL]" in cmd.content[0].text
    text = bad.content[0].text
    assert text.startswith("compile FAILED for BOBLIB/BAD")
    assert "/home/BOB/bad.rpgle:3:9 severe 30 RNF7030 The name or indicator TOTL" in text
    assert good.content[0].text.startswith("compiled BOBLIB/GOOD")
    assert json.loads(js.content[0].text)["diagnostics"][0]["line"] == 3
    assert "RNS9310" in log.content[0].text
    # off without the options, and in read-only mode
    off = HostServers(command=FAKE, guard=Guard())
    names, _ = tools(Manager(Guard()), off)
    assert not {"run_command", "compile"} & names and "job_log" in names
    names, _ = tools(Manager(Guard()), hs, read_only=True)
    assert not {"run_command", "compile"} & names
    off.close()


def test_cli_compile(hs, tmp_path, monkeypatch, capsys):
    import tn5250_mcp.credentials as cred
    import tn5250_mcp.hostservers as hsmod
    from tn5250_mcp.cli import main
    monkeypatch.setattr(cred, "keyring_available", lambda: False)   # never the real keychain
    monkeypatch.setenv("T5250_PASSWORD", "right-pw")
    real = hsmod.HostServers
    monkeypatch.setattr(hsmod, "HostServers",
                        lambda jar, java=None, **kw: real(command=FAKE, **kw))
    jar = tmp_path / "jt400.jar"
    jar.write_bytes(b"")
    with pytest.raises(SystemExit) as ei:
        main(["compile", "/home/BOB/bad.rpgle", "BOBLIB/BAD", "--host", "pub", "--user",
              "bob", "--jt400", str(jar)])
    assert ei.value.code == 1
    out = capsys.readouterr()
    assert "/home/BOB/bad.rpgle:3:9: severe 30 RNF7030" in out.out
    assert "FAILED BOBLIB/BAD" in out.err
    with pytest.raises(SystemExit) as ei:
        main(["cmd", "DSPLIBL", "--host", "pub", "--user", "bob", "--jt400", str(jar),
              "--libl", "BOBLIB,QGPL"])
    assert ei.value.code == 0
