"""Query steps and repeat loops in screen flows: submit a job on a screen,
poll for its completion with SQL, go on only if it ended normally."""

import threading
import time

import pytest

from tn5250_mcp import telnet as T
from tn5250_mcp.flows import FlowConfigError, FlowError, FlowRunner
from tn5250_mcp.guard import Guard
from tn5250_mcp.hostservers import HostServers
from tn5250_mcp.manager import Manager, render
from tn5250_mcp.session import Session

from test_flows import FakeHost, flowset, menu
from test_hostservers import FAKE

JOB = "123456/BOB/PRICES"

FLOWS = """
flows:
  run_prices:
    description: Submit the price update and wait for it.
    steps:
      - name: submit
        expect: "Demo Main Menu"
        fill: {"Selection or command": "SBMJOB CMD(CALL PRICES) JOB(PRICES)"}
        press: Enter
        capture: {job: 'Job (\\d+/\\w+/\\w+) submitted'}
      - name: wait for the job
        query: >-
          SELECT JOB_STATUS, COMPLETION_STATUS
            FROM TABLE(QSYS2.JOB_INFO(JOB_USER_FILTER => '*USER')) X WHERE JOB_NAME = ?
        params: ["{job}"]
        capture: {status: JOB_STATUS, done: COMPLETION_STATUS, n: "#rows"}
        repeat:
          until: {captured: {status: "^OUTQ$"}}
          fail_if: {captured: {n: "^0$"}}
          every: 0.1
          timeout: 3
        check: {captured: {done: "^NORMAL$"}}
      - name: next
        expect: "Demo Main Menu"
        fill: {"Selection or command": "NEXT"}
        press: Enter
"""


class App:
    def __init__(self):
        self.commands = []

    def __call__(self, fields, aid):
        cmd = fields.get((20, 7), b"").rstrip(b"\x00").decode("cp037").strip()
        self.commands.append(cmd)
        if cmd.startswith("SBMJOB"):
            return menu("Job %s submitted to job queue QBATCH in library QGPL." % JOB)
        return menu()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_IFS", str(tmp_path))
    mgr = Manager(Guard())
    mgr.hostservers = HostServers(command=FAKE, guard=Guard())
    mgr.creds.put("example", "BOB", "right-pw")        # saved from the sign-on
    app = App()
    s = Session("main", "example", 992)
    s.conn = FakeHost(s, app)
    s.connected = True
    s.user = "BOB"
    s._handle_record(T.Record(0, 0, T.OP_PUT_GET, menu()))
    s.host_seq += 1
    mgr.sessions["main"] = s
    yield mgr, s, app
    if mgr.hostservers:
        mgr.hostservers.close()


def test_waits_for_the_job_then_goes_on(env, monkeypatch):
    mgr, s, app = env
    monkeypatch.setenv("FAKE_JOB_STATES", "JOBQ,ACTIVE,ACTIVE,OUTQ")
    statuses = []

    def watch():
        for _ in range(200):
            if s.flow_status:
                statuses.append(render(s.snapshot()))
                return
            time.sleep(0.01)
    threading.Thread(target=watch, daemon=True).start()
    r = FlowRunner(mgr, flowset(FLOWS)).run(s, "run_prices", {})
    assert r.captures["job"] == JOB and r.captures["status"] == "OUTQ"
    assert "done after 4 attempts" in "\n".join(r.log)
    assert app.commands[-1] == "NEXT"
    assert statuses and "flow waiting: run_prices/wait for the job: attempt" in statuses[0]
    assert s.flow_status is None


def test_abnormal_end_stops_before_the_next_screen(env, monkeypatch):
    mgr, s, app = env
    monkeypatch.setenv("FAKE_JOB_STATES", "ACTIVE,OUTQ")
    monkeypatch.setenv("FAKE_COMPLETION", "ABNORMAL")
    with pytest.raises(FlowError, match="done doesn't match /\\^NORMAL\\$/") as ei:
        FlowRunner(mgr, flowset(FLOWS)).run(s, "run_prices", {})
    assert ei.value.step == "wait for the job" and "NEXT" not in app.commands


def test_fail_if_and_timeout(env, monkeypatch):
    mgr, s, app = env
    monkeypatch.setenv("FAKE_JOB_STATES", "JOBQ,GONE")
    with pytest.raises(FlowError, match="fail_if condition holds"):
        FlowRunner(mgr, flowset(FLOWS)).run(s, "run_prices", {})
    monkeypatch.setenv("FAKE_JOB_STATES", "JOBQ")
    mgr.hostservers.helper.close()                      # fresh poll counter
    with pytest.raises(FlowError, match="still not done after 3s.*status=JOBQ"):
        FlowRunner(mgr, flowset(FLOWS)).run(s, "run_prices", {})
    assert "NEXT" not in app.commands


def test_needs_sql_and_a_user(env):
    mgr, s, _ = env
    hs, mgr.hostservers = mgr.hostservers, None
    with pytest.raises(FlowError, match="start it with --jt400"):
        FlowRunner(mgr, flowset(FLOWS)).run(s, "run_prices", {})
    mgr.hostservers, s.user = hs, None
    with pytest.raises(FlowError, match="which user to run SQL as"):
        FlowRunner(mgr, flowset(FLOWS)).run(s, "run_prices", {})


def test_repeat_a_screen_step(env):
    """Press F5 until the screen says the job has ended (no SQL at all)."""
    mgr, s, _ = env
    presses = [0]

    def host(fields, aid):
        presses[0] += 1
        return menu("Job ended" if presses[0] >= 3 else "Job active")
    s.conn.script = host
    fs = flowset("""
flows:
  refresh:
    description: x
    steps:
      - expect: "Demo Main Menu"
        press: F5
        repeat: {until: {text: "Job ended"}, every: 0.1, timeout: 3}
""")
    r = FlowRunner(mgr, fs).run(s, "refresh", {})
    assert presses[0] == 3 and "done after 3 attempts" in r.log[-1]


@pytest.mark.parametrize("bad, msg", [
    ("""flows: {f: {description: x, params: {j: string},
        steps: [{query: "SELECT * FROM T WHERE J = '{j}'"}]}}""", "use \\? markers"),
    ("""flows: {f: {description: x, steps: [{query: "SELECT ? FROM T", params: []}]}}""",
     None),
    ("""flows: {f: {description: x, steps: [{query: "SELECT ? FROM T", params: [1, 2]}]}}""",
     "1 \\? markers but 2 params"),
    ("""flows: {f: {description: x, steps: [{query: "SELECT 1", press: Enter}]}}""",
     "can't also fill, press"),
    ("""flows: {f: {description: x, steps: [{query: "SELECT 1", repeat: {every: 1}}]}}""",
     "repeat takes until"),
    ("""flows: {f: {description: x, steps: [{query: "SELECT 1",
        repeat: {until: {captured: {x: a}}, every: 0}}]}}""", "repeat.every"),
    ("""flows: {f: {description: x, steps: [{expect: a, params: [1]}]}}""",
     "params belongs to a query"),
])
def test_invalid_query_steps(bad, msg):
    if msg is None:                                     # no params at all is fine
        flowset(bad)
        return
    with pytest.raises(FlowConfigError, match=msg):
        flowset(bad)


def test_blocked_sql_refused_before_starting(env):
    mgr, s, app = env
    fs = flowset("""
flows:
  f:
    description: x
    steps:
      - {expect: "Demo Main Menu", fill: {"Selection or command": "DSPLIBL"}, press: Enter}
      - {query: "VALUES QSYS2.QCMDEXC('DSPLIBL')"}
""")
    with pytest.raises(FlowError, match="refused before starting.*QCMDEXC"):
        FlowRunner(mgr, fs).run(s, "f", {})
    assert app.commands == []
