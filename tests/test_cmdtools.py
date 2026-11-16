"""Command tools (YAML commands:) and reading errors out of compiler listings
(offline: real IBM i 7.5 listings under tests/listings, the fake jt400 helper)."""

import asyncio
import json
import textwrap
from pathlib import Path

import pytest
from mcp.client import Client

from tn5250_mcp.cmdtools import bind, expand, result_text, run_tool
from tn5250_mcp.flows import FlowConfigError, FlowSet
from tn5250_mcp.guard import Guard
from tn5250_mcp.hostservers import HostServerError, HostServers, command_names
from tn5250_mcp.listings import parse_joblog, parse_listing
from tn5250_mcp.manager import Manager
from tn5250_mcp.server import build_server

from test_hostservers import FAKE

LISTINGS = Path(__file__).with_name("listings")


def listing(name):
    return (LISTINGS / name).read_text().split("\n")


def brief(diags):
    return [(d["line"], d["column"], d["severity"], d["id"]) for d in diags]


# ------------------------------------------------------------ listings
def test_rpg_listing():
    r = parse_listing(listing("T5RPG-1-T5RPG.txt"))
    assert r["compiler"] == "rpg" and r["source"] == "MYLIB/T5250SRC(T5RPG)"
    assert brief(r["diagnostics"]) == [(3, 0, 30, "RNF7030"), (3, 0, 30, "RNF7503")]
    assert r["diagnostics"][0]["seq"] == "3.00"
    assert r["diagnostics"][0]["text"] == "The name or indicator TOTL is not defined."


def test_sql_rpg_listings():
    pre = parse_listing(listing("T5SQL-1-T5SQL.txt"))         # the precompiler's
    assert pre["compiler"] == "sql" and pre["source"] == "MYLIB/T5250SRC(T5SQL)"
    assert brief(pre["diagnostics"]) == [(3, 39, 10, "SQL1103")]
    assert pre["diagnostics"][0]["text"] == ("Column definitions for table NOSUCHTAB in *LIBL "
                                             "not found.")
    rpg = parse_listing(listing("T5SQL-2-T5SQL.txt"))         # the RPG step: QTEMP copy
    assert rpg["source"] == "QTEMP/QSQLTEMP1(T5SQL)"
    assert brief(rpg["diagnostics"]) == [(4, 0, 30, "RNF7030"), (4, 0, 30, "RNF7503")]


def test_cl_listing():
    r = parse_listing(listing("T5CL-1-T5CL.txt"))
    assert r["compiler"] == "cl" and r["source"] == "MYLIB/T5250SRC(T5CL)"
    assert brief(r["diagnostics"]) == [(3, 0, 40, "CPD0727"), (4, 0, 30, "CPD0030"),
                                       (0, 0, 10, "CPD0726"), (0, 0, 0, "CPD0791")]


def test_dds_listing():
    r = parse_listing(listing("T5PF-1-T5PF.txt"))
    assert r["compiler"] == "dds" and r["source"] == "MYLIB/T5250SRC(T5PF)"
    assert brief(r["diagnostics"]) == [(2, 0, 30, "CPD7410"), (3, 0, 30, "CPD7410"),
                                       (0, 0, 40, "CPF7302")]
    # the page footer under the message is not part of its text
    assert r["diagnostics"][0]["text"] == "Characters in indicated field not allowed."


def test_joblog():
    msgs = parse_joblog(listing("T5RPG-2-QPJOBLOG.txt"))
    esc = [m for m in msgs if m["type"] == "Escape"]
    assert [(m["id"], m["severity"]) for m in esc] == [("RNS9310", 50)]
    assert esc[0]["text"] == "Compilation failed. Program T5RPG not created in library MYLIB."
    req = next(m for m in msgs if m["type"] == "Request")
    assert req["id"] is None and req["text"].startswith("CRTBNDRPG PGM(MYLIB/T5RPG)")
    assert msgs[-1]["id"] == "CPF1164" and "end code 20" in msgs[-1]["text"]


# ------------------------------------------------------------ definitions
TOOLS = """
toolset: build
commands:
  compile_member:
    description: Compile a member in batch, the way this shop does
    params:
      member: {type: string, upper: true, pattern: '^[A-Z$#@][A-Z0-9_$#@]{0,9}$'}
      text: {type: string, quote: true, default: ""}
    libl: [MYLIB, QGPL]
    command: SBMJOB CMD(CRTBNDRPG PGM(MYLIB/{member}) SRCFILE(MYLIB/QSRC) TEXT({text})) JOB({member})
    wait: true
    timeout: 30
  print_libl:
    description: Print the library list
    command: [DSPLIBL OUTPUT(*PRINT)]
    delete_spooled: true
  free_form:
    description: Pass a raw value
    params: {value: string}
    command: DSPOBJD OBJ({value}) OBJTYPE(*PGM)
"""


def tools(tmp_path, body=TOOLS):
    f = tmp_path / "tools.yaml"
    f.write_text(textwrap.dedent(body))
    return FlowSet.load([f])


def test_definitions(tmp_path):
    fs = tools(tmp_path)
    t = fs.commands["compile_member"]
    assert t.toolset == "build" and t.wait and t.libl == ["MYLIB", "QGPL"]
    assert expand(t, {"member": "hello", "text": "it's new"}) == [
        "SBMJOB CMD(CRTBNDRPG PGM(MYLIB/HELLO) SRCFILE(MYLIB/QSRC) TEXT('it''s new')) "
        "JOB(HELLO)"]
    assert bind(t, {"member": "A"})["text"] == "''"
    for raw, msg in [({"member": "A) PGM(X"}, "doesn't match"), ({}, "needs member"),
                     ({"member": "A", "colour": 1}, "no parameter colour")]:
        with pytest.raises(HostServerError, match=msg):
            bind(t, raw)
    # without quote: only name-like values, so nothing can be added to the command
    with pytest.raises(HostServerError, match="needs quote: true"):
        bind(fs.commands["free_form"], {"value": "X) OBJTYPE(*ALL"})
    assert bind(fs.commands["free_form"], {"value": "MYLIB/HELLO"}) == {"value": "MYLIB/HELLO"}


@pytest.mark.parametrize("body,msg", [
    ("commands: {x: {command: DSPLIBL}}", "needs a description"),
    ("commands: {x: {description: d}}", "needs command"),
    ("commands: {x: {description: d, command: 'DSPOBJD {nope}'}}", "{nope} is not a parameter"),
    ("commands: {x: {description: d, command: DSPLIBL, colour: red}}", "unknown keys"),
    ("commands: {x: {description: d, command: DSPLIBL, params: {connection: string}}}", "taken"),
    ("commands: {x: {description: d, command: DSPLIBL, params: {pw: secret}}}", "secrets"),
    ("commands: {x: {description: d, command: DSPLIBL, libl: ['no good']}}", "libl"),
    ("commands: {x: {description: d, command: DSPLIBL}}\nflows: {x: {description: d, steps: "
     "[{press: Enter}]}}", "defined twice"),
])
def test_bad_definitions(tmp_path, body, msg):
    with pytest.raises(FlowConfigError, match=msg):
        tools(tmp_path, body)


def test_command_names():
    assert command_names("SBMJOB CMD(QSYS/CRTBNDRPG PGM(A/B)) JOB(X)") == ["SBMJOB", "CRTBNDRPG"]
    assert command_names("DSPLIBL") == ["DSPLIBL"]


# ------------------------------------------------------------ running
@pytest.fixture
def hs(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_IFS", str(tmp_path))
    h = HostServers(command=FAKE, guard=Guard(), allow_commands=True)
    h.connect("main", "pub", "bob", "right-pw")
    yield h
    h.close()


def test_batch_compile_with_errors(tmp_path, hs):
    t = tools(tmp_path).commands["compile_member"]
    r = run_tool(hs, "main", t, {"member": "T5SQL"})
    assert not r["ok"] and r["end_code"] == 20 and r["job"].endswith("/BOB/T5SQL")
    # SQL errors and RPG errors, both pointing at the member (not the QTEMP copy)
    assert {d["file"] for d in r["diagnostics"]} == {"MYLIB/T5250SRC(T5SQL)"}
    assert [(d["line"], d["id"]) for d in r["diagnostics"]] == [
        (4, "RNF7030"), (4, "RNF7503"), (3, "SQL1103")]
    assert [m["id"] for m in r["job_log"] if m["type"] == "Escape"] == ["RNS9310"]
    assert [s["kind"] for s in r["spooled"]] == ["sql listing", "rpg listing", "job log"]
    text = result_text(r)
    assert text.startswith("FAILED: compile_member (job 432000/BOB/T5SQL, end code 20)")
    assert "  MYLIB/T5250SRC(T5SQL):4: severe 30 RNF7030 The name or indicator NOPE" in text
    assert "  MYLIB/T5250SRC(T5SQL):3:39: warning 10 SQL1103" in text
    assert "RNS9310 (escape, severity 50) Compilation failed." in text


def test_batch_ok_and_timeout(tmp_path, hs, monkeypatch):
    t = tools(tmp_path).commands["compile_member"]
    r = run_tool(hs, "main", t, {"member": "GOOD"})
    assert r["ok"] and r["end_code"] == 0 and not r["diagnostics"]
    assert result_text(r).startswith("ok: compile_member (job ")
    t.timeout = 1
    monkeypatch.setenv("FAKE_JOB_NEVER_ENDS", "1")
    hs.close()
    hs.connect("main", "pub", "bob", "right-pw")
    r = run_tool(hs, "main", t, {"member": "GOOD"})
    assert not r["ok"] and r["timed_out"] and "timed out waiting" in result_text(r)


def test_direct_command_reads_new_qprtjob_output(tmp_path, hs):
    t = tools(tmp_path).commands["print_libl"]
    r = run_tool(hs, "main", t, {})
    assert r["ok"] and r["job"] is None
    # only what this run printed (the old QPRTLIBL is left out), deleted after reading
    assert [(s["file"], s["number"]) for s in r["spooled"]] == [("QPRTLIBL", "7")]


def test_rules_apply(tmp_path, hs):
    fs = tools(tmp_path)
    hs.command_allow = ["SBMJOB", "DSP*"]          # the submitted CRTBNDRPG isn't allowed
    with pytest.raises(HostServerError, match="CRTBNDRPG is not among"):
        run_tool(hs, "main", fs.commands["compile_member"], {"member": "A"})
    hs.allow_commands = False
    with pytest.raises(HostServerError, match="allow-commands"):
        run_tool(hs, "main", fs.commands["print_libl"], {})


def test_mcp_tools(tmp_path, hs, monkeypatch):
    monkeypatch.setenv("T5250_PASSWORD", "right-pw")
    fs = tools(tmp_path)

    async def go(server, calls=()):
        async with Client(server) as c:
            listed = {t.name: t for t in (await c.list_tools()).tools}
            return listed, [(await c.call_tool(n, a)) for n, a in calls]
    listed, (_, res, js, bad) = asyncio.run(go(build_server(Manager(Guard()), fs, hostservers=hs), [
        ("host_connect", {"host": "pub", "user": "bob"}),
        ("compile_member", {"member": "T5CL"}),
        ("compile_member", {"member": "T5PF", "format": "json"}),
        ("compile_member", {"member": "a b"})]))
    assert {"compile_member", "print_libl", "free_form"} <= set(listed)
    desc = listed["compile_member"].description
    assert "Runs: SBMJOB CMD(CRTBNDRPG" in desc and "Waits for the job" in desc
    assert set(listed["compile_member"].input_schema["properties"]) == {
        "member", "text", "format", "connection"}
    assert "MYLIB/T5250SRC(T5CL):3: terminal 40 CPD0727" in res.content[0].text
    assert json.loads(js.content[0].text)["diagnostics"][0]["id"] == "CPD7410"
    assert bad.is_error and "doesn't match" in bad.content[0].text
    # only with --allow-commands, never read-only, toolsets apply
    off = HostServers(command=FAKE, guard=Guard())
    for server in (build_server(Manager(Guard()), fs, hostservers=off),
                   build_server(Manager(Guard()), fs, hostservers=hs, read_only=True),
                   build_server(Manager(Guard()), fs, ["other"], hostservers=hs)):
        assert not {"compile_member", "print_libl"} & set(asyncio.run(go(server))[0])
    off.close()


def test_cli(tmp_path, hs, monkeypatch, capsys):
    import tn5250_mcp.credentials as cred
    import tn5250_mcp.hostservers as hsmod
    from tn5250_mcp.cli import main
    monkeypatch.setattr(cred, "keyring_available", lambda: False)
    monkeypatch.setenv("T5250_PASSWORD", "right-pw")
    real = hsmod.HostServers
    monkeypatch.setattr(hsmod, "HostServers",
                        lambda jar, java=None, **kw: real(command=FAKE, **kw))
    (tmp_path / "tools.yaml").write_text(textwrap.dedent(TOOLS))
    jar = tmp_path / "jt400.jar"
    jar.write_bytes(b"")
    with pytest.raises(SystemExit) as ei:
        main(["tool", "compile_member", "member=T5RPG", "--flows", str(tmp_path / "tools.yaml"),
              "--host", "pub", "--user", "bob", "--jt400", str(jar)])
    assert ei.value.code == 1
    out = capsys.readouterr()
    assert "MYLIB/T5250SRC(T5RPG):3: severe 30 RNF7030" in out.out
    assert "waiting for job" in out.err
    with pytest.raises(SystemExit) as ei:
        main(["check", str(tmp_path / "tools.yaml")])
    assert ei.value.code == 0
    out = capsys.readouterr()
    assert "compile_member(member, text): command tool, waits for its job" in out.out
    assert "0 flows, 3 command tools OK" in out.err
