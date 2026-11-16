"""describe, explain_message, and run-time errors mapped to the source line
(offline, against the fake jt400 helper; its crash job log is the one seen on
IBM i 7.5 for a divide by zero in a program called through the host servers)."""

import asyncio
import json

import pytest
from mcp.client import Client

from tn5250_mcp.guard import Guard
from tn5250_mcp.hostservers import HostServerError, HostServers
from tn5250_mcp.manager import Manager
from tn5250_mcp.objects import (describe, describe_text, explain_message, explain_text,
                                message_file_for, rpg_type, split_object)
from tn5250_mcp.runtime import error_text, find_error
from tn5250_mcp.server import build_server

from test_hostservers import FAKE


@pytest.fixture
def hs(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_IFS", str(tmp_path))
    h = HostServers(command=FAKE, guard=Guard(), allow_commands=True)
    h.connect("main", "pub", "bob", "right-pw")
    yield h
    h.close()


# ------------------------------------------------------------ describe
def test_rpg_types():
    f = lambda t, b=0, d=0, p=0, **kw: rpg_type(dict(WHFLDT=t, WHFLDB=b, WHFLDD=d, WHFLDP=p, **kw))
    assert [f("A", 5), f("A", 32, WHVARL="Y"), f("P", 5, 9, 2), f("S", 7, 7, 0), f("B", 4, 9),
            f("L", 10, WHFMT="*USA"), f("Z", 26), f("F", 8)] == [
        "char(5)", "varchar(30)", "packed(9:2)", "zoned(7:0)", "bindec(9:0)", "date(*USA)",
        "timestamp", "float(8)"]


def test_split_object():
    assert split_object("custmast") == ("*LIBL", "CUSTMAST")
    assert split_object("mylib/custmast") == ("MYLIB", "CUSTMAST")
    with pytest.raises(HostServerError, match="NAME or LIB/NAME"):
        split_object("my lib/x")


def test_describe_files(hs):
    [d] = describe(hs, "main", "custmast")
    assert d["object"] == "BOBLIB/CUSTMAST" and d["kind"] == "physical file"
    fields = d["formats"][0]["fields"]
    assert [(x["name"], x["type"]) for x in fields] == [
        ("CUSTID", "char(5)"), ("NAME", "varchar(30)"), ("BAL", "packed(9:2)"),
        ("SINCE", "date(*ISO)")]
    assert fields[1]["alias"] == "CUSTOMER_NAME" and fields[1]["null"]
    assert d["keys"] == [{"field": "CUSTID", "descending": False}] and d["unique"]
    text = describe_text([d])
    assert "BOBLIB/CUSTMAST *FILE PF - Customers" in text
    assert "    NAME       varchar(30)      Name; alias CUSTOMER_NAME; null-capable" in text
    assert "  keys: CUSTID (unique)" in text
    [dsp] = describe(hs, "main", "BOBLIB/CUSTDSP")
    assert dsp["kind"] == "display file" and not dsp["keys"]
    assert "    OPT        char(1)          input; at 5,2" in describe_text([dsp])


def test_describe_programs_and_more(hs):
    [p] = describe(hs, "main", "CRASH")
    assert p["modules"][0]["source"] == "BOBLIB/QRPGLESRC(CRASH)"
    assert p["service_programs"] == ["BOBLIB/UTILS"]          # QSYS ones left out
    [s] = describe(hs, "main", "UTILS")
    assert s["exports"] == [{"symbol": "GREET", "kind": "procedure"},
                            {"symbol": "COUNTER", "kind": "data"}]
    assert "  exports procedures: GREET\n  exports data: COUNTER" in describe_text([s])
    [b] = describe(hs, "main", "APP")
    assert "  BOBLIB/UTILS *SRVPGM" in describe_text([b])
    [m] = describe(hs, "main", "APPMSG", pattern="APP*")
    assert m["messages"][0]["id"] == "APP0001"
    [a] = describe(hs, "main", "CFG")
    assert describe_text([a]).endswith("*CHAR, length 20: 'PROD'")
    both = describe(hs, "main", "DUP")
    assert [x["type"] for x in both] == ["*PGM", "*FILE"]
    assert [x["type"] for x in describe(hs, "main", "DUP", "*PGM")] == ["*PGM"]
    with pytest.raises(HostServerError, match="NOPE not found"):
        describe(hs, "main", "NOPE")


def test_explain_message(hs):
    assert message_file_for("RNF7030") == ("QDEVTOOLS", "QRPGLEMSG")
    assert message_file_for("RNQ0102") == ("QSYS", "QRNXMSG")
    assert message_file_for("CPF9801") == ("QSYS", "QCPFMSG")
    m = explain_message(hs, "main", "rnf7030")
    assert (m["severity"], m["cause"], m["recovery"]) == (
        30, "A field is not defined.", "Define it. Compile again.")
    assert explain_text(m).startswith("RNF7030 (severity 30, QDEVTOOLS/QRPGLEMSG): The name")
    for mid, msg in [("XYZ1234", "pass file"), ("RNQ9999", "not in QSYS/QRNXMSG"),
                     ("hello", "like RNF7030")]:
        with pytest.raises(HostServerError, match=msg):
            explain_message(hs, "main", mid)


# ------------------------------------------------------------ run-time errors
def test_find_error_unmonitored_only():
    e = find_error([{"id": "CEE9901", "type": "escape", "severity": 30, "module": None,
                     "statement": "*N", "from_library": "QSYS", "from_program": "QLEAWI",
                     "procedure": None, "text": "Application error. RNX0100 unmonitored by "
                                                "PGMA at statement 0000000012, instruction"}])
    assert (e["program"], e["statement"], e["rpg_status"]) == ("PGMA", 12, "00100")
    assert find_error([]) is None


def test_crash_is_mapped_to_the_source_line(hs):
    r = hs.call_program("main", "BOBLIB/CRASH", [{"type": "packed(5,0)", "value": 0}])
    e = r["error"]
    assert not r["ok"] and e["id"] == "MCH1211" and e["statement"] == 8
    # statement 8 of the listing = line 6 of the member, after the 2 lines /COPY'd in
    assert (e["file"], e["line"], e["source_line"]) == ("BOBLIB/QRPGLESRC(CRASH)", 6,
                                                        "x = 1 / z;")
    assert (e["rpg_status"], e["rpg_text"]) == ("00102", "Attempt to divide by zero (C G D F).")
    assert [m["id"] for m in e["job_log"]][:1] == ["MCH1211"]     # not the older error
    text = error_text(e)
    assert text.splitlines() == [
        "error MCH1211 at BOBLIB/QRPGLESRC(CRASH):6 in BOBLIB/CRASH: Attempt made to divide by "
        "zero for fixed point operation.",
        "  RPG status 00102 (RNQ0102): Attempt to divide by zero (C G D F).",
        "  6 | x = 1 / z;"]


def test_statement_mapping_notes(hs):
    from tn5250_mcp.runtime import map_statement
    sql = map_statement(hs, "main", "BOBLIB", "SQLCRASH", None, 8)
    assert "precompiler" in sql["note"] and "line" not in sql
    assert map_statement(hs, "main", "BOBLIB", "CRASH", None, 700)["line"] == 7   # *SRCSTMT
    assert "past the end" in map_statement(hs, "main", "BOBLIB", "CRASH", None, 55)["note"]
    assert "no module information" in map_statement(hs, "main", "BOBLIB", "NOPE", None, 1)["note"]


def test_tools(hs, monkeypatch):
    monkeypatch.setenv("T5250_PASSWORD", "right-pw")

    async def go():
        async with Client(build_server(Manager(Guard()), hostservers=hs)) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            out = []
            for n, a in [("host_connect", {"host": "pub", "user": "bob"}),
                         ("describe", {"object": "BOBLIB/CUSTMAST"}),
                         ("describe", {"object": "UTILS", "format": "json"}),
                         ("explain_message", {"id": "RNQ0102"}),
                         ("call_program", {"program": "CRASH"})]:
                out.append((await c.call_tool(n, a)).content[0].text)
            return names, out
    names, (_, d, js, ex, call) = asyncio.run(go())
    assert {"describe", "explain_message"} <= names
    assert "format CUSTR (50 bytes) - customer" in d
    assert json.loads(js)[0]["exports"][0]["symbol"] == "GREET"
    assert ex.startswith("RNQ0102 (severity 99, QSYS/QRNXMSG)") and "Cause: RPG procedure" in ex
    assert call.startswith("FAILED: CALL CRASH")
    assert "error MCH1211 at BOBLIB/QRPGLESRC(CRASH):6" in call and "  6 | x = 1 / z;" in call
