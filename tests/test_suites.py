"""Test suites: loading, data-driven cases, expectations, snapshots, recovery,
reports (offline: fake 5250 host and fake jt400 helper)."""

import textwrap
import xml.etree.ElementTree as ET

import pytest

from tn5250_mcp import telnet as T
from tn5250_mcp.guard import Guard
from tn5250_mcp.hostservers import HostServers
from tn5250_mcp.session import Session
from tn5250_mcp.terminal import Terminal
from tn5250_mcp.testing import (SuiteError, SuiteRunner, check_snapshot, html_report, junit_xml,
                                load_suite)

from test_flows import ORDERS, FakeHost, entry, flowset, menu, order_app
from test_hostservers import FAKE


def suite_file(tmp_path, body: str, name="suite.yaml"):
    p = tmp_path / name
    p.write_text(textwrap.dedent(body))
    return p


@pytest.fixture
def term(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_IFS", str(tmp_path))
    t = Terminal("example", flows=flowset(ORDERS))
    t.mgr.hostservers = HostServers(command=FAKE, guard=Guard())
    t.mgr.creds.put("example", "BOB", "right-pw")
    s = Session("main", "example", 992, log=t.mgr._logger("main"))
    s.conn = FakeHost(s, order_app)
    s.connected = True
    s.user = "BOB"
    s._handle_record(T.Record(0, 0, T.OP_PUT_GET, menu()))
    s.host_seq += 1
    t.mgr.sessions["main"] = t.s = s
    yield t
    t.mgr.hostservers.close()


SUITE = """
suite: orders
vars: {who: C100}
home: {text: "Demo Main Menu", keys: [F3]}
setup:
  - sql: "DELETE FROM TESTS WHERE WHO = ?"
    params: ["{who}"]
tests:
  - name: add an order
    flow: add_order
    with: {customer: "{who}", qty: 5}
    expect:
      captured: {order_no: '^\\d+$'}
      screen: ["Demo Main Menu"]
      snapshot: after_order
      sql:
        - query: SELECT ID, NAME, PRICE FROM T
          count: 5
          contains: [["1", "ITEM 1", "1.50"]]
  - name: unknown customer is refused
    flow: add_order
    with: {customer: BAD, qty: 1}
    expect_failure: "Customer not found"
  - name: wrong expectation
    flow: add_order
    with: {customer: C2, qty: 1}
    expect:
      captured: {order_no: '^9999$'}
  - name: data driven
    flow: add_order
    data: orders.csv
    expect:
      captured: {order_no: '4711'}
  - name: later
    flow: add_order
    with: {customer: X, qty: 1}
    skip: not ready yet
teardown:
  - sql: "DELETE FROM TESTS WHERE WHO = ?"
    params: ["{who}"]
"""


def test_load_and_validate(tmp_path):
    (tmp_path / "orders.csv").write_text("case,customer,qty\nsmall,A1,1\nbig,B2,500\n")
    s = load_suite(suite_file(tmp_path, SUITE), {"who": "C900"})
    assert s.vars["who"] == "C900"
    assert [c.name for c in s.cases] == ["add an order", "unknown customer is refused",
                                         "wrong expectation", "data driven [small]",
                                         "data driven [big]", "later"]
    assert s.cases[3].values["qty"] == "1" and s.snapshots == tmp_path / "suite.snapshots"
    for bad, msg in [("suite: x\n", "needs tests"),
                     ("tests: [{name: a, flow: f, steps: []}]\n", "flow .* or steps"),
                     ("tests: [{flow: f, expect: {sql: [{rows: []}]}}]\n", "needs query"),
                     ("tests: [{flow: f, colour: red}]\n", "unknown keys"),
                     ("tests: [{flow: f}]\nsetup: [{sql: a, command: b}]\n", "one of sql")]:
        with pytest.raises(SuiteError, match=msg):
            load_suite(suite_file(tmp_path, bad, "bad.yaml"))


def test_run_a_suite_and_report(tmp_path, term):
    (tmp_path / "orders.csv").write_text("case,customer,qty\nsmall,A1,1\nbig,B2,500\n")
    suite = load_suite(suite_file(tmp_path, SUITE))
    term.press("Enter", {"Selection or command": "ORDERS"})      # start elsewhere: recovery
    assert term.on("ORDER ENTRY")
    sr = SuiteRunner(suite, term, say=lambda m: None).run()
    status = {r.name: r.status for r in sr.results}
    assert status == {"add an order": "passed", "unknown customer is refused": "passed",
                      "wrong expectation": "failed", "data driven [small]": "passed",
                      "data driven [big]": "passed", "later": "skipped"}
    assert not sr.ok and sr.counts() == {"passed": 4, "failed": 1, "error": 0, "skipped": 1}
    wrong = next(r for r in sr.results if r.name == "wrong expectation")
    assert "order_no is '4711', expected /^9999$/" in wrong.message
    first = sr.results[0]
    assert first.captures["order_no"] == "4711" and "snapshot after_order saved" in first.notes
    assert (tmp_path / "suite.snapshots" / "after_order.txt").exists()
    # JUnit XML for CI, HTML for people
    root = ET.fromstring(junit_xml(sr))
    ts = root.find("testsuite")
    assert (ts.get("tests"), ts.get("failures"), ts.get("skipped")) == ("6", "1", "1")
    assert root.find(".//testcase[@name='wrong expectation']/failure") is not None
    page = html_report(sr)
    assert "wrong expectation" in page and "4 passed, 1 failed" in page
    # second run: the snapshot is compared, not saved again
    sr2 = SuiteRunner(suite, term, only="^add an order$", say=lambda m: None).run()
    assert [r.status for r in sr2.results] == ["passed"] and not sr2.results[0].notes


def test_snapshot_differs(tmp_path, term):
    suite = load_suite(suite_file(tmp_path, """
        tests:
          - {name: menu, flow: add_order, with: {customer: A, qty: 1},
             expect: {snapshot: menu}}
        """))
    SuiteRunner(suite, term, say=lambda m: None).run()
    snap = tmp_path / "suite.snapshots" / "menu.txt"
    snap.write_text(snap.read_text().replace("Demo Main Menu", "Demo Other Menu"))
    r = SuiteRunner(suite, term, say=lambda m: None).run().results[0]
    assert r.status == "failed" and "-" in r.diff and "+ MAIN    Demo Main Menu" in r.diff
    r = SuiteRunner(suite, term, update_snapshots=True, say=lambda m: None).run().results[0]
    assert r.status == "passed" and "Demo Main Menu" in snap.read_text()


def test_snapshot_masks_what_changes(tmp_path):
    a = ["Work with Jobs     16/11/26  20:41:07", "Job 123456/BOB/QPADEV0021 active"]
    b = ["Work with Jobs     17/11/26  09:02:55", "Job 654321/BOB/QPADEV0042 active"]
    assert check_snapshot(a, "jobs", tmp_path)[0] == "created"
    assert check_snapshot(b, "jobs", tmp_path)[0] == "matched"
    c = ["Work with Jobs     17/11/26  09:02:55", "Job 654321/BOB/QPADEV0042 ENDED"]
    status, diff = check_snapshot(c, "jobs", tmp_path)
    assert status == "differs" and "ENDED" in diff
    assert check_snapshot(c, "jobs", tmp_path, ignore=[r"active|ENDED"])[0] == "matched"


def test_setup_failure_stops_the_suite(tmp_path, term):
    suite = load_suite(suite_file(tmp_path, """
        setup:
          - command: "NOSUCHCMD"
        tests:
          - {flow: add_order, with: {customer: A, qty: 1}}
        """))
    sr = SuiteRunner(suite, term, say=lambda m: None).run()
    assert sr.results == [] and "command failed: NOSUCHCMD" in sr.setup_error and not sr.ok
    assert ET.fromstring(junit_xml(sr)).find(".//testcase[@name='setup']/error") is not None


def test_terminal_assert_snapshot(tmp_path, term):
    assert term.assert_snapshot("menu", str(tmp_path)) == "created"
    assert term.assert_snapshot("menu", str(tmp_path)) == "matched"
    term.s._handle_record(T.Record(0, 0, T.OP_PUT_GET, entry()))
    with pytest.raises(AssertionError, match="differs from snapshot menu"):
        term.assert_snapshot("menu", str(tmp_path))
