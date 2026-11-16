"""Building a project folder (upload, order, incremental rebuilds, errors in
local paths) and calling programs with typed parameters (offline, against the
fake jt400 helper)."""

import asyncio
import json

import pytest
import yaml
from mcp.client import Client

from tn5250_mcp.build import (BuildError, build, load_project, load_state, plan, report_text,
                              target_command)
from tn5250_mcp.cli import parse_call_params
from tn5250_mcp.guard import Guard
from tn5250_mcp.hostservers import HostServerError, HostServers, check_param
from tn5250_mcp.manager import Manager
from tn5250_mcp.server import build_server

from test_hostservers import FAKE

PROJECT = """
project: shop
lib: BOBLIB
libl: [BOBLIB, QGPL]
ifs: /home/BOB/shop
src: src
bnddir: SHOP
srvpgms:
  UTILS: {modules: [UTILS], bnd: utils.bnd}
programs:
  MAIN: {modules: [MAIN], srvpgms: [UTILS]}
options: {rpgmod: "TGTRLS(*CURRENT)"}
"""
FILES = {
    "src/inc/utils_h.rpgleinc": "**free\ndcl-pr greet varchar(50) end-pr;\n",
    "src/utils.rpgle": "**free\nctl-opt nomain;\n/copy inc/utils_h.rpgleinc\n",
    "src/main.rpgle": "**free\ndcl-f custs;\n/copy inc/utils_h.rpgleinc\n*inlr = *on;\n",
    "src/hello.pgm.rpgle": "**free\ndsply 'hello';\n*inlr = *on;\n",
    "src/custs.pf": "     A          R CUSTR\n     A            ID             5\n",
    "src/custsl1.lf": "     A          R CUSTR                     PFILE(CUSTS)\n",
    "src/orders.table": "CREATE OR REPLACE TABLE ORDERS (ID INT);\n",
    "src/utils.bnd": "STRPGMEXP SIGNATURE('V1')\nEXPORT SYMBOL(GREET)\nENDPGMEXP\n",
    "src/README.md": "not a source\n",
}


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "shop"
    for rel, text in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    (root / "tn5250-project.yaml").write_text(PROJECT)
    return root


@pytest.fixture
def hs(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_IFS", str(tmp_path / "ifs"))
    monkeypatch.setenv("FAKE_CMDLOG", str(tmp_path / "commands.log"))
    h = HostServers(command=FAKE, guard=Guard(), allow_commands=True, compile_libs=["BOBLIB"],
                    ifs_write_roots=["/home/BOB"], allow_sql_write=True)
    h.connect("main", "pub", "bob", "right-pw")
    h.cmdlog = tmp_path / "commands.log"
    yield h
    h.close()


def commands(hs) -> list[str]:
    lines = hs.cmdlog.read_text().splitlines() if hs.cmdlog.exists() else []
    hs.cmdlog.write_text("")
    return [x.split()[0] + " " + x.split()[1].split("(")[1].rstrip(")") if "(" in x.split()[1]
            else x.split()[0] for x in lines]


# ------------------------------------------------------------ project file
def test_load_project(proj):
    p = load_project(proj)
    kinds = {k: (t.kind, t.phase) for k, t in p.targets.items()}
    assert kinds == {"*SRVPGM:UTILS": ("srvpgm", 5), "*PGM:MAIN": ("pgm", 6),
                     "*PGM:HELLO": ("rpg", 6), "*MODULE:MAIN": ("rpgmod", 4),
                     "*MODULE:UTILS": ("rpgmod", 4), "*FILE:CUSTS": ("pf", 1),
                     "*FILE:CUSTSL1": ("lf", 2), "SQL:src/orders.table": ("sql", 0)}
    assert p.targets["*PGM:HELLO"].srvpgms == ["UTILS"]          # bound through the bnddir
    assert "src/README.md" not in p.uploads and "src/utils.bnd" in p.uploads
    assert p.includes == ["src/inc/utils_h.rpgleinc"]
    assert p.remote("src/main.rpgle") == "/home/BOB/shop/src/main.rpgle"
    assert p.local("/home/BOB/shop/src/main.rpgle") == str(proj / "src/main.rpgle")
    srv = target_command(p, p.targets["*SRVPGM:UTILS"])
    assert srv == ("CRTSRVPGM SRVPGM(BOBLIB/UTILS) MODULE(BOBLIB/UTILS) EXPORT(*SRCFILE) "
                   "SRCSTMF('/home/BOB/shop/src/utils.bnd') REPLACE(*YES)")
    assert "BNDDIR(BOBLIB/SHOP)" in target_command(p, p.targets["*PGM:HELLO"])
    assert target_command(p, p.targets["SQL:src/orders.table"]).startswith(
        "RUNSQLSTM SRCSTMF('/home/BOB/shop/src/orders.table') COMMIT(*NONE)")


def test_two_sources_for_one_object(proj):
    # module UTILS and service program UTILS may share a name; two sources for
    # one module may not, nor a program from a source and from programs:
    (proj / "src/utils.clle").write_text("PGM\nENDPGM\n")
    with pytest.raises(BuildError, match="would both create \\*MODULE UTILS"):
        load_project(proj)
    (proj / "src/utils.clle").unlink()
    (proj / "src/main.pgm.rpgle").write_text("**free\n")
    with pytest.raises(BuildError, match="programs.MAIN: src/main.pgm.rpgle already"):
        load_project(proj)


@pytest.mark.parametrize("change,msg", [
    ("lib: ''", "needs lib"),
    ("colour: red", "keys are"),
    ("ifs: home/BOB", "absolute IFS folder"),
    ("lib: MUCHTOOLONGNAME", "not an IBM i object name"),
    ("programs: {X: {modules: [NOPE]}}", "no nope.rpgle"),
    ("programs: {X: {modules: [MAIN], srvpgms: [NOPE]}}", "isn't in srvpgms"),
    ("srvpgms: {U2: {modules: [UTILS], bnd: missing.bnd}}", "missing.bnd not found"),
])
def test_bad_project(proj, change, msg):
    doc = yaml.safe_load(PROJECT) | yaml.safe_load(change)
    (proj / "tn5250-project.yaml").write_text(yaml.safe_dump(doc))
    with pytest.raises(BuildError, match=msg):
        load_project(proj)


# ------------------------------------------------------------ building
def test_build_then_incremental(proj, hs, tmp_path):
    p = load_project(proj)
    r = build(hs, "main", p)
    assert r["ok"] and len(r["uploaded"]) == 8
    assert [(o["object"], o["status"]) for o in r["objects"]] == [
        ("ORDERS", "built"), ("CUSTS", "built"), ("CUSTSL1", "built"), ("MAIN", "built"),
        ("UTILS", "built"), ("UTILS", "built"), ("HELLO", "built"), ("MAIN", "built")]
    assert (tmp_path / "ifs/home/BOB/shop/src/inc/utils_h.rpgleinc").exists()
    cmds = commands(hs)
    assert cmds[0] == "CRTBNDDIR BOBLIB/SHOP" and "ADDBNDDIRE BOBLIB/SHOP" in cmds
    assert "CRTSRCPF QTEMP/QDDSSRC" in cmds and "CPYFRMSTMF '/home/BOB/shop/src/custs.pf'" in cmds
    assert r["objects"][1]["command"].startswith(
        "CRTPF FILE(BOBLIB/CUSTS) SRCFILE(QTEMP/QDDSSRC) SRCMBR(CUSTS) OPTION(*EVENTF *NOSRC *NOLIST)")
    assert "BNDDIR(BOBLIB/SHOP) DFTACTGRP(*NO)" in r["objects"][6]["command"]
    assert "INCDIR('/home/BOB/shop')" in r["objects"][3]["command"]
    assert "TGTRLS(*CURRENT)" in r["objects"][3]["command"]
    assert "DBGVIEW(*SOURCE) OUTPUT(*NONE)" in r["objects"][3]["command"]     # no listings left behind
    assert "OPTION(*EVENTF *NOSRC *NOLIST)" in r["objects"][1]["command"]
    # nothing changed: nothing to do
    r = build(hs, "main", load_project(proj))
    assert r["up_to_date"] and report_text(r) == "shop: everything is up to date"
    # an include changes: both modules that copy it, the service program and the program
    (proj / "src/inc/utils_h.rpgleinc").write_text("**free\n// v2\n")
    up, todo = plan(load_project(proj), load_state(load_project(proj)))
    assert up == ["src/inc/utils_h.rpgleinc"]
    assert [(t.name, t.kind) for t in todo] == [("MAIN", "rpgmod"), ("UTILS", "rpgmod"),
                                                ("UTILS", "srvpgm"), ("HELLO", "rpg"),
                                                ("MAIN", "pgm")]
    # the physical file changes: dropped (logical file first) and created again,
    # with the logical file and the module that declares it
    build(hs, "main", load_project(proj))
    commands(hs)
    (proj / "src/custs.pf").write_text(FILES["src/custs.pf"] + "     A            NAME          30\n")
    r = build(hs, "main", load_project(proj))
    assert [o["object"] for o in r["objects"]] == ["CUSTS", "CUSTSL1", "MAIN", "MAIN"]
    assert commands(hs)[:2] == ["DLTF BOBLIB/CUSTSL1", "DLTF BOBLIB/CUSTS"]
    # a file fails: what uses it is skipped
    (proj / "src/custs.pf").write_text("     A            TOTL           5\n")
    r = build(hs, "main", load_project(proj))
    assert [(o["object"], o["status"]) for o in r["objects"]] == [
        ("CUSTS", "failed"), ("CUSTSL1", "skipped"), ("MAIN", "skipped"), ("MAIN", "skipped")]
    assert r["objects"][0]["diagnostics"][0]["file"] == str(proj / "src/custs.pf")
    (proj / "src/custs.pf").write_text(FILES["src/custs.pf"])
    assert build(hs, "main", load_project(proj))["ok"]
    # --only, and --all
    _, todo = plan(load_project(proj), {}, only=["hello"])
    assert [t.name for t in todo] == ["HELLO"]
    up, todo = plan(load_project(proj), load_state(load_project(proj)), rebuild_all=True)
    assert len(up) == 8 and len(todo) == 8


def test_errors_point_at_local_files(proj, hs):
    (proj / "src/main.rpgle").write_text("**free\ndcl-s t int(10);\nt = TOTL;\n")
    r = build(hs, "main", load_project(proj))
    assert not r["ok"]
    by = {(o["object"], o["kind"]): o for o in r["objects"]}
    main_mod = by[("MAIN", "rpgmod")]
    assert main_mod["status"] == "failed"
    assert main_mod["diagnostics"][0]["file"] == str(proj / "src/main.rpgle")
    assert by[("MAIN", "pgm")]["status"] == "skipped"
    assert by[("HELLO", "rpg")]["status"] == "built"
    text = report_text(r)
    assert "%s:3:5: severe 30 RNF7030" % (proj / "src/main.rpgle") in text
    assert "SKIPPED BOBLIB/MAIN (pgm): needs MAIN, which failed" in text
    assert text.endswith("shop: FAILED")
    # fixed: only the failed module and the program after it are built again
    (proj / "src/main.rpgle").write_text(FILES["src/main.rpgle"])
    r = build(hs, "main", load_project(proj))
    assert r["ok"] and [o["object"] for o in r["objects"]] == ["MAIN", "MAIN"]


def test_sql_errors_from_the_listing(proj, hs):
    (proj / "src/orders.table").write_text("CREATE TABLE ORDERS\n  syntax error;\n")
    r = build(hs, "main", load_project(proj))
    o = r["objects"][0]
    assert o["status"] == "failed" and o["diagnostics"] == [{
        "file": str(proj / "src/orders.table"), "line": 2, "column": 8, "severity": 30,
        "severity_name": "severe", "id": "SQL0104", "text": "Token SYNTAX was not valid."}]
    assert "DLTSPLF FILE(ORDERS) JOB(654322/BOB/QPRTJOB)" in hs.cmdlog.read_text()


def test_build_policy(proj, hs):
    p = load_project(proj)
    hs.compile_libs = ["OTHER"]
    with pytest.raises(HostServerError, match="may not create objects in BOBLIB"):
        build(hs, "main", p)
    hs.compile_libs = ["BOBLIB"]
    hs.ifs_write_roots = ["/home/ALICE"]
    with pytest.raises(HostServerError, match="outside the folders"):
        build(hs, "main", p)
    hs.ifs_write_roots = ["/home/BOB"]
    hs.allow_sql_write = False
    with pytest.raises(HostServerError, match="allow-sql-write"):
        build(hs, "main", p)
    hs.allow_sql_write = True
    (proj / "src/evil.sql").write_text("CALL QSYS2.QCMDEXC('CRTLIB X');\n")
    with pytest.raises(HostServerError, match="blocked command 'QCMDEXC'"):
        build(hs, "main", load_project(proj))


# ------------------------------------------------------------ program calls
def test_check_param():
    assert check_param({"type": "Packed(7, 2)", "value": 1.5}) == {
        "type": "packed(7,2)", "value": 1.5, "out": False}
    for bad, msg in [({"type": "char"}, "type is"), ({"type": "packed(70,2)"}, "size"),
                     ({"type": "packed(5,6)"}, "size"), ({"type": "int(10)", "value": "x"},
                                                         "not a number"),
                     ({"type": "char(5)", "colour": 1}, "type, value, out"), ("char(5)", "type, value, out")]:
        with pytest.raises(HostServerError, match=msg):
            check_param(bad)


def test_call_program(hs):
    res = hs.call_program("main", "boblib/greet", [
        {"type": "char(10)", "value": "BOB"}, {"type": "packed(7,2)", "value": 2},
        {"type": "packed(7,2)", "value": 3}, {"type": "packed(9,2)", "out": True},
        {"type": "char(20)", "out": True}], libl=["BOBLIB"])
    assert res["ok"] and res["outputs"][3:] == [5.0, "HELLO BOB"]
    assert not hs.call_program("main", "NOSUCH")["ok"]
    for prog, msg in [("a b", "NAME or LIB/NAME"), ("QSYS/QCMDEXC", "blocked")]:
        with pytest.raises(HostServerError, match=msg):
            hs.call_program("main", prog)
    hs.command_allow = ["DSP*"]
    with pytest.raises(HostServerError, match="CALL is not among"):
        hs.call_program("main", "GREET")
    hs.allow_commands = False
    with pytest.raises(HostServerError, match="allow-commands"):
        hs.call_program("main", "GREET")


def test_parse_call_params():
    assert parse_call_params(["char(10)=BOB", "packed(7,2)=0:out", "char(50):out",
                              "char(1)="]) == [
        {"type": "char(10)", "value": "BOB", "out": False},
        {"type": "packed(7,2)", "value": "0", "out": True},
        {"type": "char(50)", "out": True}, {"type": "char(1)", "value": "", "out": False}]


def test_tools(proj, hs, monkeypatch):
    monkeypatch.setenv("T5250_PASSWORD", "right-pw")
    server = build_server(Manager(Guard()), hostservers=hs)

    async def go():
        async with Client(server) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            out = []
            for n, a in [("host_connect", {"host": "pub", "user": "bob"}),
                         ("call_program", {"program": "GREET", "params": [
                             {"type": "char(10)", "value": "ANN"},
                             {"type": "char(20)", "out": True}]}),
                         ("build_project", {"folder": str(proj)}),
                         ("build_project", {"folder": str(proj), "format": "json"}),
                         ("build_project", {"folder": str(proj / "src")})]:
                out.append(await c.call_tool(n, a))
            return names, out
    names, (_, call, built, again, missing) = asyncio.run(go())
    assert {"call_program", "build_project"} <= names
    assert call.content[0].text.startswith("ok: CALL GREET")
    assert 'parameter 2 char(20) = "HELLO ANN"' in call.content[0].text
    assert "BUILT   BOBLIB/HELLO (rpg)" in built.content[0].text
    assert json.loads(again.content[0].text)["up_to_date"]
    assert missing.is_error and "no tn5250-project.yaml" in missing.content[0].text


def test_cli_build_and_call(proj, hs, tmp_path, monkeypatch, capsys):
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
    with pytest.raises(SystemExit) as ei:
        main(["build", str(proj)] + args)
    assert ei.value.code == 0
    out = capsys.readouterr()
    assert "uploaded src/main.rpgle" in out.err and out.out.strip().endswith("shop: built")
    with pytest.raises(SystemExit) as ei:
        main(["call", "GREET", "char(10)=ZED", "char(20):out"] + args)
    assert ei.value.code == 0
    assert '2 char(20) = "HELLO ZED"' in capsys.readouterr().out


def test_example_project():
    from pathlib import Path
    p = load_project(Path(__file__).parent.parent / "examples" / "project")
    _, todo = plan(p, {})
    assert [(t.name, t.kind) for t in todo] == [
        ("ORDERS", "sql"), ("CUST", "pf"), ("CUSTL1", "lf"), ("GREET", "rpgmod"),
        ("MAIN", "rpgmod"), ("GREET", "srvpgm"), ("CALC", "rpg"), ("MAIN", "pgm")]
