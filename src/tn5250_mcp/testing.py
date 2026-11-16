"""Test suites for IBM i applications, on top of screen flows.

A suite is a YAML file: how to connect, which flows to load, setup and
teardown (SQL, CL commands, flows), and tests. A test runs one or more flows
and then checks what it expects: captured values, the screen, a saved screen
snapshot, rows in the database. A test can also expect a flow to fail
(negative tests), and run once per row of a CSV file or a list of cases.

    tn5250-run test tests/orders.yaml --junit results.xml --html report.html

    suite: order entry
    profile: pub400
    flows: [../flows]
    vars: {lib: MYLIB}                       # {lib} below; --var lib=X overrides
    home: {text: "IBM i Main Menu", keys: [F3, F12]}   # recovered to before each test
    setup:
      - sql: "DELETE FROM {lib}/ORDERS WHERE CUSTOMER LIKE 'TEST%'"
    tests:
      - name: add an order
        flow: add_order
        with: {customer: TEST1, qty: 5}
        expect:
          captured: {order_no: '^\\d+$'}
          screen: ["Order {order_no} created"]
          snapshot: order_created
          sql:
            - query: SELECT QTY FROM {lib}/ORDERS WHERE CUSTOMER = ?
              params: [TEST1]
              rows: [[5]]
      - name: quantity zero is refused
        flow: add_order
        with: {customer: TEST1, qty: 0}
        expect_failure: "Quantity must be"
      - name: many orders
        flow: add_order
        data: orders.csv                     # one test per row (columns = values)
"""

from __future__ import annotations

import csv
import difflib
import html
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

import yaml

from .flows import FlowError, FlowSet

SUITE_KEYS = {"suite", "profile", "profiles", "connect", "flows", "vars", "home", "snapshots",
              "ignore", "setup", "teardown", "tests"}
TEST_KEYS = {"name", "flow", "with", "steps", "data", "cases", "expect", "expect_failure", "skip"}
EXPECT_KEYS = {"captured", "screen", "not_screen", "snapshot", "sql"}
SQL_KEYS = {"query", "params", "rows", "count", "contains"}
ACTION_KEYS = {"sql", "params", "command", "flow", "with", "ignore_errors"}
# parts of a screen that change from run to run; masked in snapshots
DEFAULT_IGNORE = [
    r"\b\d{2}[/.-]\d{2}[/.-]\d{2,4}\b",          # dates
    r"\b\d{1,2}:\d{2}(:\d{2})?\b",                # times
    r"\b\d{6}/[A-Z0-9_$#@]+/[A-Z0-9_$#@]+\b",     # qualified job names
    r"\bQPADEV\w{4}\b",                           # device names
]
_TEMPLATE = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")


class SuiteError(ValueError):
    """The suite file is wrong (not: a test failed)."""


def fill(text: Any, values: dict[str, Any]) -> Any:
    """{name} from vars, the data row and captures; other types unchanged."""
    if isinstance(text, list):
        return [fill(t, values) for t in text]
    if isinstance(text, dict):
        return {k: fill(v, values) for k, v in text.items()}
    if not isinstance(text, str):
        return text

    def sub(m):
        if m.group(0) == "{{":
            return "{"
        if m.group(0) == "}}":
            return "}"
        if m.group(1) not in values:
            raise SuiteError("{%s} has no value (vars, data columns and captures: %s)"
                             % (m.group(1), ", ".join(sorted(values)) or "none"))
        return str(values[m.group(1)])
    return _TEMPLATE.sub(sub, text)


# ----------------------------------------------------------------- the suite file
@dataclass
class Case:
    name: str
    steps: list[dict]                 # [{flow, with}]
    values: dict[str, Any]            # vars + data row
    expect: dict
    expect_failure: str | None = None
    skip: str | None = None
    row: dict[str, Any] = field(default_factory=dict)   # the data row alone


@dataclass
class Suite:
    name: str
    path: Path
    connect: dict
    flows: list[str]
    vars: dict[str, Any]
    home: dict | None
    snapshots: Path
    ignore: list[str]
    setup: list[dict]
    teardown: list[dict]
    cases: list[Case]


def _check_keys(d: Any, allowed: set[str], where: str) -> dict:
    if not isinstance(d, dict):
        raise SuiteError("%s: must be a mapping" % where)
    bad = set(d) - allowed
    if bad:
        raise SuiteError("%s: unknown keys %s (allowed: %s)"
                         % (where, sorted(bad), ", ".join(sorted(allowed))))
    return d


def _actions(items: Any, where: str) -> list[dict]:
    if items is None:
        return []
    if not isinstance(items, list):
        raise SuiteError("%s: a list of {sql}, {command} or {flow} items" % where)
    out = []
    for i, a in enumerate(items, 1):
        _check_keys(a, ACTION_KEYS, "%s item %d" % (where, i))
        kinds = [k for k in ("sql", "command", "flow") if k in a]
        if len(kinds) != 1:
            raise SuiteError("%s item %d: one of sql, command or flow" % (where, i))
        out.append(a)
    return out


def _rows_from_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return [{k.strip(): (v if v is not None else "") for k, v in row.items() if k}
                for row in csv.DictReader(fh)]


def load_suite(path: str | os.PathLike, overrides: dict[str, str] | None = None) -> Suite:
    p = Path(path)
    try:
        doc = yaml.safe_load(p.read_text("utf-8"))
    except OSError as e:
        raise SuiteError("%s: %s" % (p, e.strerror or e)) from None
    except yaml.YAMLError as e:
        raise SuiteError("%s: not valid YAML: %s" % (p, e)) from None
    _check_keys(doc, SUITE_KEYS, str(p))
    if not doc.get("tests"):
        raise SuiteError("%s: needs tests" % p)
    base = p.parent
    connect = {}
    if doc.get("profile"):
        connect = {"profile": str(doc["profile"]),
                   "profiles": [str(base / x) for x in (doc.get("profiles") or [])]}
    elif doc.get("connect"):
        connect = dict(doc["connect"])
    vars_ = {str(k): v for k, v in (doc.get("vars") or {}).items()}
    for k, v in (overrides or {}).items():
        vars_[k] = v
    home = doc.get("home")
    if home is not None:
        _check_keys(home, {"text", "keys", "max"}, "%s: home" % p)
        if not home.get("text"):
            raise SuiteError("%s: home needs text (what the home screen shows)" % p)
    cases: list[Case] = []
    for i, t in enumerate(doc["tests"], 1):
        where = "%s: test %d" % (p, i)
        _check_keys(t, TEST_KEYS, where)
        name = str(t.get("name") or "test %d" % i)
        if ("flow" in t) == ("steps" in t):
            raise SuiteError("%s (%s): give flow (with with) or steps" % (where, name))
        steps = ([{"flow": t["flow"], "with": t.get("with")}] if "flow" in t
                 else [_check_keys(s, {"flow", "with"}, "%s step" % where) for s in t["steps"]])
        expect = _check_keys(t.get("expect") or {}, EXPECT_KEYS, "%s expect" % where)
        for q in expect.get("sql") or []:
            _check_keys(q, SQL_KEYS, "%s expect.sql" % where)
            if "query" not in q:
                raise SuiteError("%s expect.sql: needs query" % where)
        rows: list[dict] = [{}]
        if "data" in t:
            rows = _rows_from_csv(base / str(t["data"]))
            if not rows:
                raise SuiteError("%s: %s has no rows" % (where, t["data"]))
        elif "cases" in t:
            rows = [dict(r) for r in t["cases"]]
        for n, row in enumerate(rows, 1):
            label = name if len(rows) == 1 else "%s [%s]" % (
                name, row.get("case") or row.get("name") or n)
            cases.append(Case(name=label, steps=steps, values={**vars_, **row}, row=row,
                              expect=expect,
                              expect_failure=t.get("expect_failure"),
                              skip=str(t["skip"]) if t.get("skip") else None))
    snap = doc.get("snapshots")
    return Suite(name=str(doc.get("suite") or p.stem), path=p, connect=connect,
                 flows=[str(base / x) for x in (doc.get("flows") or [])], vars=vars_,
                 home=home, snapshots=base / snap if snap else p.with_suffix(".snapshots"),
                 ignore=[str(x) for x in (doc.get("ignore") or [])],
                 setup=_actions(doc.get("setup"), "%s: setup" % p),
                 teardown=_actions(doc.get("teardown"), "%s: teardown" % p), cases=cases)


# ----------------------------------------------------------------- snapshots
def mask(rows: list[str], patterns: list[str]) -> list[str]:
    out = []
    for r in rows:
        for rx in patterns:
            r = re.sub(rx, "~", r)          # one ~ whatever the length: "active" vs "ENDED"
        out.append(r.rstrip())
    while out and not out[-1]:
        out.pop()
    return out


def check_snapshot(rows: list[str], name: str, folder: Path, ignore: list[str] = (),
                   update: bool = False) -> tuple[str, str | None]:
    """Compare a screen with snapshot `name` (masking dates, times, job and
    device names and `ignore` patterns). Returns (status, diff): status is
    "matched", "created" (first run, or update) or "differs"."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise SuiteError("snapshot names are letters, digits, _ . - (got %r)" % name)
    patterns = DEFAULT_IGNORE + list(ignore)
    now = mask(rows, patterns)
    f = Path(folder) / (name + ".txt")
    if update or not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("\n".join(now) + "\n", encoding="utf-8")
        return "created", None
    then = f.read_text(encoding="utf-8").rstrip("\n").split("\n")
    then = mask(then, patterns)
    if then == now:
        return "matched", None
    diff = "\n".join(difflib.unified_diff(then, now, "snapshot " + name, "this run", lineterm="",
                                          n=1))
    return "differs", diff


# ----------------------------------------------------------------- results
@dataclass
class Result:
    name: str
    status: str = "passed"            # passed, failed, error, skipped
    message: str = ""
    seconds: float = 0.0
    log: list[str] = field(default_factory=list)
    captures: dict[str, str] = field(default_factory=dict)
    screen: str = ""
    diff: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class SuiteResult:
    suite: str
    results: list[Result]
    seconds: float
    setup_error: str | None = None

    def counts(self) -> dict[str, int]:
        c = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
        for r in self.results:
            c[r.status] += 1
        return c

    @property
    def ok(self) -> bool:
        c = self.counts()
        return not self.setup_error and c["failed"] == 0 and c["error"] == 0


class Failed(Exception):
    pass


# ----------------------------------------------------------------- running
class SuiteRunner:
    """Runs a suite on a connected, signed-on Terminal (and its SQL
    connection, opened on first use with the saved sign-on password)."""

    def __init__(self, suite: Suite, terminal, update_snapshots: bool = False,
                 only: str | None = None, say=print):
        self.suite, self.t, self.update, self.only, self.say = suite, terminal, \
            update_snapshots, only, say
        self._sql_ready = False

    # -- SQL and commands through the host servers ---------------------------
    def _hs(self):
        from .hostservers import host_login
        hs = self.t.mgr.hostservers
        if hs is None:
            raise Failed("this suite uses SQL or commands: pass --jt400 (JTOpen)")
        if not hs.connected("suite"):
            s = self.t.session
            user = s.user or (s.profile.user if s.profile else None)
            if not user:
                raise Failed("don't know the user for SQL: set user in the profile or sign on "
                             "with a flow whose password names its user_param")
            hs.allow_sql_write = hs.allow_commands = True       # a person runs the suite
            host_login(self.t.mgr, hs, "suite", s.host, user, s.tls,
                       s.profile.libl if s.profile else None)
        return hs

    def _action(self, a: dict, values: dict) -> str:
        if "sql" in a:
            res = self._hs().execute("suite", fill(a["sql"], values), fill(a.get("params"), values))
            return "sql: %s" % res.get("update_count", "ok")
        if "command" in a:
            res = self._hs().command("suite", fill(a["command"], values))
            if not res.get("ok"):
                msg = "; ".join("%s %s" % (m.get("id"), m.get("text")) for m in
                                res.get("messages", []) if (m.get("severity") or 0) >= 30)
                raise Failed("command failed: %s (%s)" % (fill(a["command"], values), msg))
            return "command ok"
        r = self.t.run(a["flow"], **fill(a.get("with") or {}, values))
        return "flow %s ok" % r.flow

    def _actions(self, items: list[dict], what: str) -> list[str]:
        out = []
        for a in items:
            try:
                out.append(self._action(a, self.suite.vars))
            except Exception as e:  # noqa: BLE001 - reported, setup decides what next
                if a.get("ignore_errors"):
                    out.append("%s (ignored): %s" % (what, str(e).splitlines()[0]))
                    continue
                raise Failed("%s: %s" % (what, e)) from None
        return out

    # -- getting back to a known screen --------------------------------------
    def go_home(self) -> None:
        home = self.suite.home
        if not home:
            return
        keys = home.get("keys") or ["F3", "F12"]
        for i in range(int(home.get("max", 10))):
            if self.t.on(home["text"]):
                return
            if self.t.keyboard == "error":
                self.t.press("Reset")
            try:
                self.t.press(keys[i % len(keys)], timeout=15)
            except Exception:  # noqa: BLE001 - try the next key
                pass
        if not self.t.on(home["text"]):
            raise Failed("couldn't get back to the home screen (%r)" % home["text"])

    # -- one test ------------------------------------------------------------
    def run_case(self, c: Case) -> Result:
        res = Result(c.name)
        t0 = time.monotonic()
        values = dict(c.values)
        try:
            if c.skip:
                res.status, res.message = "skipped", c.skip
                return res
            self.go_home()
            failure = None
            for st in c.steps:
                if st.get("with") is not None:
                    params = fill(st["with"], values)
                else:                          # data rows: the columns the flow takes
                    flow = (self.t.flows.flows if self.t.flows else {}).get(st["flow"])
                    params = {k: v for k, v in c.row.items() if flow and k in flow.params}
                try:
                    r = self.t.run(st["flow"], **params)
                except FlowError as e:
                    failure = e
                    res.log.append("%s: failed at step %s" % (e.flow or st["flow"], e.step))
                    break
                res.log += r.log
                res.captures.update(r.captures)
                values.update(r.captures)
            if c.expect_failure:
                if failure is None:
                    raise Failed("expected the flow to fail (%r), but it went through"
                                 % c.expect_failure)
                if not re.search(fill(c.expect_failure, values), str(failure.args[0])):
                    raise Failed("the flow failed, but not as expected (%r): %s"
                                 % (c.expect_failure, failure.args[0]))
                res.notes.append("failed as expected: %s" % failure.args[0])
            elif failure is not None:
                res.screen = failure.screen
                raise Failed(str(failure.args[0]) + " (flow %s, step %s)"
                             % (failure.flow, failure.step))
            self._expect(c, values, res)
        except Failed as e:
            res.status, res.message = "failed", str(e)
        except Exception as e:  # noqa: BLE001 - anything else is an error in the test
            res.status, res.message = "error", "%s: %s" % (type(e).__name__, e)
        finally:
            res.seconds = time.monotonic() - t0
            if not res.screen and self.t.s is not None:
                try:
                    res.screen = "\n".join(self.t.rows)
                except Exception:  # noqa: BLE001
                    pass
        return res

    def _expect(self, c: Case, values: dict, res: Result) -> None:
        e = c.expect
        for name, rx in (e.get("captured") or {}).items():
            got = values.get(name)
            if got is None:
                raise Failed("nothing was captured as %s" % name)
            if not re.search(fill(str(rx), {k: re.escape(str(v)) for k, v in values.items()}),
                             str(got)):
                raise Failed("%s is %r, expected /%s/" % (name, got, rx))
        texts = e.get("screen") or []
        for t in texts if isinstance(texts, list) else [texts]:
            if not self.t.on(fill(t, values)):
                raise Failed("%r is not on the screen" % fill(t, values))
        texts = e.get("not_screen") or []
        for t in texts if isinstance(texts, list) else [texts]:
            if self.t.on(fill(t, values)):
                raise Failed("%r is on the screen" % fill(t, values))
        if e.get("snapshot"):
            status, diff = check_snapshot(self.t.rows, fill(e["snapshot"], values),
                                          self.suite.snapshots, self.suite.ignore, self.update)
            if status == "created":
                res.notes.append("snapshot %s saved" % e["snapshot"])
            elif status == "differs":
                res.diff = diff
                raise Failed("the screen differs from snapshot %s" % e["snapshot"])
        for q in e.get("sql") or []:
            self._expect_sql(q, values)

    def _expect_sql(self, q: dict, values: dict) -> None:
        sql = fill(q["query"], values)
        out = self._hs().query("suite", sql, fill(q.get("params"), values), max_rows=1000)
        got = [[("" if v is None else str(v).strip()) for v in row] for row in out["rows"]]

        def norm(rows):
            return [[("" if v is None else str(fill(v, values)).strip()) for v in row]
                    for row in rows]
        if "count" in q and len(got) != int(q["count"]):
            raise Failed("%s returned %d rows, expected %d" % (sql, len(got), int(q["count"])))
        if "rows" in q and got != norm(q["rows"]):
            raise Failed("%s returned %s, expected %s" % (sql, got, norm(q["rows"])))
        for row in norm(q.get("contains") or []):
            if row not in got:
                raise Failed("%s has no row %s (got %s)" % (sql, row, got[:10]))

    # -- the whole suite -----------------------------------------------------
    def run(self) -> SuiteResult:
        t0 = time.monotonic()
        results: list[Result] = []
        setup_error = None
        try:
            for line in self._actions(self.suite.setup, "setup"):
                self.say("  setup: %s" % line)
        except Failed as e:
            setup_error = str(e)
        if setup_error is None:
            for c in self.suite.cases:
                if self.only and not re.search(self.only, c.name):
                    continue
                r = self.run_case(c)
                self.say("  %-7s %s%s" % (r.status.upper(), c.name,
                                          (": " + r.message.splitlines()[0]) if r.message else ""))
                results.append(r)
        if self.suite.teardown:
            try:
                self.go_home()             # e.g. leave a program that still holds a lock
            except Exception:  # noqa: BLE001 - teardown runs anyway
                pass
        try:
            for line in self._actions(self.suite.teardown, "teardown"):
                self.say("  teardown: %s" % line)
        except Failed as e:
            self.say("  teardown failed: %s" % e)
        return SuiteResult(self.suite.name, results, time.monotonic() - t0, setup_error)


# ----------------------------------------------------------------- reports
def junit_xml(sr: SuiteResult) -> str:
    c = sr.counts()
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<testsuites name="%s" tests="%d" failures="%d" errors="%d" skipped="%d" time="%.3f">'
           % (xml_escape(sr.suite, {'"': "&quot;"}), len(sr.results), c["failed"],
              c["error"] + (1 if sr.setup_error else 0), c["skipped"], sr.seconds),
           '  <testsuite name="%s" tests="%d" failures="%d" errors="%d" skipped="%d" time="%.3f">'
           % (xml_escape(sr.suite, {'"': "&quot;"}), len(sr.results), c["failed"],
              c["error"] + (1 if sr.setup_error else 0), c["skipped"], sr.seconds)]
    if sr.setup_error:
        out.append('    <testcase classname="%s" name="setup"><error message="%s"/></testcase>'
                   % (xml_escape(sr.suite), xml_escape(sr.setup_error, {'"': "&quot;"})))
    for r in sr.results:
        out.append('    <testcase classname="%s" name="%s" time="%.3f">'
                   % (xml_escape(sr.suite, {'"': "&quot;"}), xml_escape(r.name, {'"': "&quot;"}),
                      r.seconds))
        detail = "\n".join(filter(None, [r.message, r.diff, r.screen]))
        if r.status == "failed":
            out.append('      <failure message="%s">%s</failure>' % (
                xml_escape(r.message.splitlines()[0] if r.message else "", {'"': "&quot;"}),
                xml_escape(detail)))
        elif r.status == "error":
            out.append('      <error message="%s">%s</error>' % (
                xml_escape(r.message, {'"': "&quot;"}), xml_escape(detail)))
        elif r.status == "skipped":
            out.append('      <skipped message="%s"/>' % xml_escape(r.message, {'"': "&quot;"}))
        log = "\n".join(r.log + r.notes)
        if log:
            out.append("      <system-out>%s</system-out>" % xml_escape(log))
        out.append("    </testcase>")
    out += ["  </testsuite>", "</testsuites>", ""]
    return "\n".join(out)


def html_report(sr: SuiteResult) -> str:
    c = sr.counts()
    e = html.escape
    color = {"passed": "#15803d", "failed": "#b91c1c", "error": "#b45309", "skipped": "#6b7280"}
    rows = []
    for r in sr.results:
        caps = "".join("<li><code>%s</code> = <code>%s</code></li>" % (e(k), e(str(v)))
                       for k, v in r.captures.items())
        parts = []
        if r.message:
            parts.append("<p class=msg>%s</p>" % e(r.message))
        if r.notes:
            parts.append("<ul>%s</ul>" % "".join("<li>%s</li>" % e(n) for n in r.notes))
        if r.diff:
            parts.append("<h4>Snapshot difference</h4><pre class=diff>%s</pre>" % e(r.diff))
        if caps:
            parts.append("<h4>Captured</h4><ul>%s</ul>" % caps)
        if r.log:
            parts.append("<h4>Steps</h4><pre>%s</pre>" % e("\n".join(r.log)))
        if r.screen:
            parts.append("<h4>Screen</h4><pre class=screen>%s</pre>" % e(r.screen))
        rows.append(
            "<details%s><summary><span class=badge style='background:%s'>%s</span> %s "
            "<span class=time>%.1fs</span></summary>%s</details>" % (
                " open" if r.status in ("failed", "error") else "", color[r.status],
                r.status, e(r.name), r.seconds, "".join(parts)))
    setup = ("<p class=msg>Setup failed: %s</p>" % e(sr.setup_error)) if sr.setup_error else ""
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,
initial-scale=1"><title>%(title)s</title>
<style>
:root { --bg:#fff; --fg:#111827; --muted:#6b7280; --line:#e5e7eb; --code:#f3f4f6; }
@media (prefers-color-scheme: dark) { :root { --bg:#0b0d0c; --fg:#e5e7eb; --muted:#9ca3af;
  --line:#2a302d; --code:#161a18; } }
body { margin:0; padding:24px 16px; background:var(--bg); color:var(--fg);
  font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width:1000px; margin:0 auto; }
h1 { font-size:22px; margin:0 0 4px; } .sum { color:var(--muted); margin:0 0 20px; }
details { border:1px solid var(--line); border-radius:8px; margin:8px 0; padding:8px 12px; }
summary { cursor:pointer; } .badge { color:#fff; border-radius:999px; padding:1px 8px;
  font-size:12px; font-weight:600; text-transform:uppercase; } .time { color:var(--muted); }
pre { background:var(--code); padding:10px; border-radius:6px; overflow-x:auto;
  font:13px/1.35 ui-monospace, Menlo, Consolas, monospace; }
.screen { white-space:pre; } .msg { color:#b91c1c; white-space:pre-wrap; } h4 { margin:12px 0 4px; }
</style></head><body><main>
<h1>%(title)s</h1>
<p class=sum>%(passed)d passed, %(failed)d failed, %(error)d errors, %(skipped)d skipped
&middot; %(seconds).1fs &middot; %(when)s</p>
%(setup)s%(rows)s
</main></body></html>
""" % {"title": e(sr.suite), "passed": c["passed"], "failed": c["failed"],
       "error": c["error"], "skipped": c["skipped"], "seconds": sr.seconds,
       "when": time.strftime("%Y-%m-%d %H:%M"), "setup": setup, "rows": "\n".join(rows)}


def load_flows(suite: Suite) -> FlowSet | None:
    return FlowSet.load(suite.flows) if suite.flows else None
