"""Screen flows: named, parameterised 5250 procedures defined in YAML.

A flow is a fixed list of steps (check the screen, fill fields, press a key,
check the answer). The caller only supplies typed, validated parameters, so an
agent using a flow can't type outside of it. Flows become MCP tools, and the
same files run from the `tn5250-run` CLI, the Python API and pytest.

    toolset: orders                  # default toolset for the flows below
    flows:
      add_order:
        description: Add an order on the order entry screen.
        params:
          customer: {type: string, pattern: "^[A-Z0-9]{1,8}$"}
          qty:      {type: integer, min: 1, max: 9999}
        steps:
          - expect: "1:ORDER ENTRY"
            fill: {Customer: "{customer}", Quantity: "{qty}"}
            press: Enter
            check: {not_text: "not found"}
            capture: {order_no: 'Order (\\d+) created'}

See README.md ("Screen flows") for every key.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

import yaml

from .manager import GuardError, Manager, render
from .session import ALL_KEYS, FieldError, Session, norm_key

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
PARAM_TYPES = {"string", "integer", "number", "boolean", "secret"}
PARAM_KEYS = {"type", "description", "default", "min_length", "max_length", "pattern",
              "enum", "upper", "min", "max", "decimals", "true_value", "false_value", "env",
              "prompt", "user_param"}
STEP_KEYS = {"name", "expect", "when", "fill", "cursor", "press", "wait_for", "timeout",
             "check", "capture", "call", "with", "fail", "change_password", "query", "params",
             "repeat"}
REPEAT_KEYS = {"until", "fail_if", "every", "timeout"}
CHANGE_PASSWORD_LABELS = {"current": "Current password", "new": "New password",
                          "verify": "New password (to verify)"}
CHECK_KEYS = {"text", "not_text", "regex", "keyboard", "captured"}
FLOW_KEYS = {"description", "toolset", "read_only", "expose", "params", "steps", "pace"}
_TEMPLATE = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")


class FlowConfigError(ValueError):
    """A flow file is invalid; the message names the file, flow and step."""


class FlowError(Exception):
    """A flow failed while running. `screen` is the screen at the failure."""

    def __init__(self, message: str, screen: str = "", flow: str = "", step: str = ""):
        super().__init__(message)
        self.screen, self.flow, self.step = screen, flow, step

    def __str__(self) -> str:
        where = "flow %s, step %s: " % (self.flow, self.step) if self.flow else ""
        return where + self.args[0] + ("\n" + self.screen if self.screen else "")


# ---------------------------------------------------------------- definitions
@dataclass
class Param:
    name: str
    type: str
    description: str = ""
    default: Any = None
    required: bool = True
    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    enum: list[str] | None = None
    upper: bool = False
    min: float | None = None
    max: float | None = None
    decimals: int | None = None
    true_value: str = "Y"
    false_value: str = "N"
    env: str | None = None
    prompt: str | None = None           # secret: label for the viewer prompt
    user_param: str | None = None       # secret: parameter with the user profile; lets the
                                        # password be kept for that user and host

    def coerce(self, raw: Any) -> str:
        """Validate a value (from MCP, the CLI or Python) and return the text
        to type. Raises ValueError with a readable message."""
        n = self.name
        if self.type == "secret":
            raise ValueError("parameter %r is a secret: a person types it in the viewer, or it "
                             "comes from the environment; it can't be passed in" % n)
        if self.type == "boolean":
            if isinstance(raw, bool):
                b = raw
            elif str(raw).strip().lower() in ("1", "true", "yes", "y", "on"):
                b = True
            elif str(raw).strip().lower() in ("0", "false", "no", "n", "off"):
                b = False
            else:
                raise ValueError("%s: expected true or false, got %r" % (n, raw))
            return self.true_value if b else self.false_value
        if self.type in ("integer", "number"):
            if isinstance(raw, bool):
                raise ValueError("%s: expected a number, got %r" % (n, raw))
            try:
                d = Decimal(str(raw).strip())
            except InvalidOperation:
                raise ValueError("%s: expected a number, got %r" % (n, raw)) from None
            if not d.is_finite():
                raise ValueError("%s: expected a number, got %r" % (n, raw))
            if self.type == "integer" and d != d.to_integral_value():
                raise ValueError("%s: expected a whole number, got %r" % (n, raw))
            if self.min is not None and d < Decimal(str(self.min)):
                raise ValueError("%s: %s is below the minimum %s" % (n, d, self.min))
            if self.max is not None and d > Decimal(str(self.max)):
                raise ValueError("%s: %s is above the maximum %s" % (n, d, self.max))
            if self.type == "integer":
                return str(int(d))
            if self.decimals is not None:
                exp = -d.normalize().as_tuple().exponent
                if exp > self.decimals:
                    raise ValueError("%s: at most %d decimals allowed, got %s"
                                     % (n, self.decimals, d))
            return format(d.normalize(), "f")
        # string
        if not isinstance(raw, str):
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                raw = str(raw)
            else:
                raise ValueError("%s: expected text, got %r" % (n, raw))
        v = raw.upper() if self.upper else raw
        if "\n" in v or "\r" in v:
            raise ValueError("%s: line breaks are not allowed" % n)
        if self.min_length is not None and len(v) < self.min_length:
            raise ValueError("%s: at least %d characters needed" % (n, self.min_length))
        if self.max_length is not None and len(v) > self.max_length:
            raise ValueError("%s: at most %d characters allowed, got %d"
                             % (n, self.max_length, len(v)))
        if self.enum is not None and v not in self.enum:
            raise ValueError("%s: must be one of %s, got %r" % (n, ", ".join(self.enum), v))
        if self.pattern is not None and not re.fullmatch(self.pattern, v):
            raise ValueError("%s: %r doesn't match the pattern %s" % (n, v, self.pattern))
        return v


@dataclass
class Check:
    text: list[str] = field(default_factory=list)
    not_text: list[str] = field(default_factory=list)
    regex: list[str] = field(default_factory=list)
    keyboard: str | None = None          # ready | error | any (default: not error)
    captured: dict[str, list[str]] = field(default_factory=dict)   # capture -> regexes


@dataclass
class Step:
    label: str
    expect: list[str] = field(default_factory=list)
    when: list[str] = field(default_factory=list)
    fill: dict[str, str] = field(default_factory=dict)
    cursor: str | None = None
    press: str | None = None
    wait_for: str | None = None
    timeout: float = 30.0
    check: Check = field(default_factory=Check)
    capture: dict[str, Any] = field(default_factory=dict)
    call: str | None = None
    call_with: dict[str, Any] = field(default_factory=dict)
    fail: str | None = None
    change_password: dict | None = None   # {secret, current, new, verify}: expired password
    query: str | None = None              # SQL through the host servers (read-only)
    params: list = field(default_factory=list)
    repeat: "Repeat | None" = None


@dataclass
class Repeat:
    """Do a step again until a condition holds: polling a query or a screen."""
    until: list[Check]                    # any of these
    fail_if: list[Check] = field(default_factory=list)
    every: float = 5.0
    timeout: float = 300.0


@dataclass
class Flow:
    name: str
    description: str
    toolset: str | None
    read_only: bool
    expose: bool
    params: dict[str, Param]
    steps: list[Step]
    source: str
    pace: float = 0.0

    def captures(self, flows: "FlowSet", seen=()) -> set[str]:
        out: set[str] = set()
        for st in self.steps:
            out.update(st.capture)
            if st.call and st.call not in seen and st.call in flows.flows:
                out.update(flows.flows[st.call].captures(flows, (*seen, self.name)))
        return out

    def secrets(self, flows: "FlowSet", seen=()) -> list[Param]:
        """Secret parameters of this flow and of every flow it calls."""
        out = [p for p in self.params.values() if p.type == "secret"]
        for st in self.steps:
            if st.call and st.call not in seen and st.call in flows.flows:
                out += flows.flows[st.call].secrets(flows, (*seen, self.name))
        return out

    def summary(self) -> str:
        ps = []
        for p in self.params.values():
            if p.type == "secret":
                ps.append("%s (secret: viewer prompt%s)" % (
                    p.name, " or $%s" % p.env if p.env else ""))
            else:
                ps.append(p.name + ("" if p.required else "=%r" % p.default))
        return "%s(%s)%s%s - %s" % (self.name, ", ".join(ps),
                                    " [read-only]" if self.read_only else "",
                                    " [%s]" % self.toolset if self.toolset else "",
                                    self.description)


def _strs(v: Any, where: str) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (str, int, float)) and not isinstance(v, bool):
        return [str(v)]
    if isinstance(v, list) and all(isinstance(x, (str, int, float)) for x in v):
        return [str(x) for x in v]
    raise FlowConfigError("%s: expected text or a list of text" % where)


def _template_names(s: str) -> set[str]:
    return {m.group(1) for m in _TEMPLATE.finditer(s) if m.group(1)}


def fill_template(s: str, values: dict[str, str]) -> str:
    def sub(m):
        if m.group(0) == "{{":
            return "{"
        if m.group(0) == "}}":
            return "}"
        if m.group(1) not in values:
            raise FlowError("{%s} has no value yet (the step that captures it didn't run)"
                            % m.group(1))
        return values[m.group(1)]
    return _TEMPLATE.sub(sub, s)


def _parse_param(name: str, spec: Any, where: str) -> Param:
    if not NAME_RE.match(name) or name == "session":
        raise FlowConfigError("%s: parameter name %r must be lower_snake_case and not "
                              "'session'" % (where, name))
    if isinstance(spec, str):
        spec = {"type": spec}
    if not isinstance(spec, dict):
        raise FlowConfigError("%s: parameter %s must be a mapping" % (where, name))
    bad = set(spec) - PARAM_KEYS
    if bad:
        raise FlowConfigError("%s: parameter %s: unknown keys %s" % (where, name, sorted(bad)))
    t = spec.get("type", "string")
    if t not in PARAM_TYPES:
        raise FlowConfigError("%s: parameter %s: type must be one of %s"
                              % (where, name, sorted(PARAM_TYPES)))
    p = Param(name=name, type=t, description=str(spec.get("description", "")))
    for k in ("min_length", "max_length", "decimals"):
        if k in spec:
            if not isinstance(spec[k], int) or spec[k] < 0:
                raise FlowConfigError("%s: parameter %s: %s must be a whole number"
                                      % (where, name, k))
            setattr(p, k, spec[k])
    for k in ("min", "max"):
        if k in spec:
            if not isinstance(spec[k], (int, float)) or isinstance(spec[k], bool):
                raise FlowConfigError("%s: parameter %s: %s must be a number" % (where, name, k))
            setattr(p, k, spec[k])
    if "pattern" in spec:
        try:
            re.compile(str(spec["pattern"]))
        except re.error as e:
            raise FlowConfigError("%s: parameter %s: bad pattern: %s" % (where, name, e)) from None
        p.pattern = str(spec["pattern"])
    if "enum" in spec:
        p.enum = _strs(spec["enum"], "%s: parameter %s: enum" % (where, name))
    p.upper = bool(spec.get("upper", False))
    p.true_value = str(spec.get("true_value", "Y"))
    p.false_value = str(spec.get("false_value", "N"))
    if t == "secret":
        if "env" in spec:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(spec["env"])):
                raise FlowConfigError("%s: secret parameter %s: env must be a variable name"
                                      % (where, name))
            p.env = str(spec["env"])
        p.prompt = str(spec.get("prompt") or p.description or name.replace("_", " ").capitalize())
        if "user_param" in spec:
            p.user_param = str(spec["user_param"])
        if "default" in spec:
            raise FlowConfigError("%s: secret parameter %s can't have a default"
                                  % (where, name))
    elif "env" in spec or "prompt" in spec or "user_param" in spec:
        raise FlowConfigError("%s: parameter %s: env, prompt and user_param are only for "
                              "type: secret" % (where, name))
    if "default" in spec:
        p.required = False
        p.default = spec["default"]
        try:
            p.coerce(p.default)
        except ValueError as e:
            raise FlowConfigError("%s: default is invalid: %s" % (where, e)) from None
    return p


def _parse_check(spec: Any, where: str) -> Check:
    if spec is None:
        return Check()
    if not isinstance(spec, dict) or set(spec) - CHECK_KEYS:
        raise FlowConfigError("%s: check takes %s" % (where, sorted(CHECK_KEYS)))
    c = Check(text=_strs(spec.get("text"), where + " check.text"),
              not_text=_strs(spec.get("not_text"), where + " check.not_text"),
              regex=_strs(spec.get("regex"), where + " check.regex"))
    for rx in c.regex:
        try:
            re.compile(rx)
        except re.error as e:
            raise FlowConfigError("%s: check.regex %r: %s" % (where, rx, e)) from None
    cap = spec.get("captured") or {}
    if not isinstance(cap, dict):
        raise FlowConfigError("%s: check.captured maps capture name -> regex(es)" % where)
    c.captured = {str(k): _strs(v, where + " check.captured." + str(k)) for k, v in cap.items()}
    for rx in (r for rs in c.captured.values() for r in rs):
        try:
            re.compile(rx)
        except re.error as e:
            raise FlowConfigError("%s: check.captured %r: %s" % (where, rx, e)) from None
    if "keyboard" in spec:
        if spec["keyboard"] not in ("ready", "error", "any"):
            raise FlowConfigError("%s: check.keyboard is ready, error or any" % where)
        c.keyboard = spec["keyboard"]
    return c


def _parse_step(i: int, spec: Any, where: str) -> Step:
    if not isinstance(spec, dict):
        raise FlowConfigError("%s step %d: must be a mapping" % (where, i))
    bad = set(spec) - STEP_KEYS
    label = str(spec.get("name") or i)
    w = "%s step %s" % (where, label)
    if bad:
        raise FlowConfigError("%s: unknown keys %s (allowed: %s)"
                              % (w, sorted(bad), sorted(STEP_KEYS)))
    st = Step(label=label, expect=_strs(spec.get("expect"), w + " expect"),
              when=_strs(spec.get("when"), w + " when"),
              check=_parse_check(spec.get("check"), w))
    fill = spec.get("fill") or {}
    if not isinstance(fill, dict):
        raise FlowConfigError("%s: fill must map field -> value" % w)
    st.fill = {str(k): ("" if v is None else str(v)) for k, v in fill.items()}
    if "cursor" in spec:
        st.cursor = str(spec["cursor"])
    if "press" in spec:
        key = str(spec["press"])
        if norm_key(key) not in {norm_key(k) for k in ALL_KEYS}:
            raise FlowConfigError("%s: unknown key %r (keys: %s)" % (w, key, ", ".join(ALL_KEYS)))
        st.press = key
    if "wait_for" in spec:
        st.wait_for = str(spec["wait_for"])
    if "timeout" in spec:
        if not isinstance(spec["timeout"], (int, float)) or spec["timeout"] <= 0:
            raise FlowConfigError("%s: timeout must be a positive number of seconds" % w)
        st.timeout = float(spec["timeout"])
    if "query" in spec:
        q = spec["query"]
        if not isinstance(q, str) or not q.strip():
            raise FlowConfigError("%s: query is an SQL statement" % w)
        if _template_names(q):
            raise FlowConfigError("%s: no {templates} inside a query (they would become SQL); "
                                  "use ? markers and params instead" % w)
        st.query = q.strip()
        prm = spec.get("params") or []
        if not isinstance(prm, list) or not all(
                isinstance(x, (str, int, float)) and not isinstance(x, bool) or x is None
                for x in prm):
            raise FlowConfigError("%s: params is a list of values for the ? markers" % w)
        if prm and len(prm) != q.count("?"):
            raise FlowConfigError("%s: the query has %d ? markers but %d params"
                                  % (w, q.count("?"), len(prm)))
        st.params = list(prm)
        if any(k in spec for k in ("fill", "press", "cursor", "call", "fail",
                                   "change_password", "wait_for")):
            raise FlowConfigError("%s: a query step can't also fill, press, call, wait or fail"
                                  % w)
    elif "params" in spec:
        raise FlowConfigError("%s: params belongs to a query step" % w)
    if "repeat" in spec:
        rp = spec["repeat"]
        if not isinstance(rp, dict) or "until" not in rp or set(rp) - REPEAT_KEYS:
            raise FlowConfigError("%s: repeat takes until (required), fail_if, every and "
                                  "timeout" % w)

        def conds(v, what):
            items = v if isinstance(v, list) else [v]
            if not items:
                raise FlowConfigError("%s: repeat.%s is empty" % (w, what))
            return [_parse_check(c, "%s repeat.%s" % (w, what)) for c in items]
        every, timeout = rp.get("every", 5), rp.get("timeout", 300)
        for k, v, lo, hi in (("every", every, 0.1, 3600), ("timeout", timeout, 1, 86400)):
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
                raise FlowConfigError("%s: repeat.%s is seconds (%g-%g)" % (w, k, lo, hi))
        st.repeat = Repeat(until=conds(rp["until"], "until"),
                           fail_if=conds(rp["fail_if"], "fail_if") if rp.get("fail_if") else [],
                           every=float(every), timeout=float(timeout))
        if spec.get("fill") and not spec.get("press"):
            raise FlowConfigError("%s: repeat on a step that fills without pressing does "
                                  "nothing new each time" % w)
    cap = spec.get("capture") or {}
    if not isinstance(cap, dict):
        raise FlowConfigError("%s: capture must map name -> regex or {field: ref}" % w)
    if st.query:                          # query steps capture result columns
        for name, c in cap.items():
            if not NAME_RE.match(str(name)) or not isinstance(c, str) or not c.strip():
                raise FlowConfigError("%s: capture %s: give a column name (or #rows)"
                                      % (w, name))
            st.capture[str(name)] = c.strip()
        cap = {}
    for name, c in cap.items():
        if not NAME_RE.match(str(name)):
            raise FlowConfigError("%s: capture name %r must be lower_snake_case" % (w, name))
        if isinstance(c, str):
            try:
                if re.compile(c).groups < 1:
                    raise FlowConfigError("%s: capture %s: the regex needs a (group)" % (w, name))
            except re.error as e:
                raise FlowConfigError("%s: capture %s: %s" % (w, name, e)) from None
        elif isinstance(c, dict) and set(c) == {"rows"}:
            m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*", str(c["rows"]))
            if not m:
                raise FlowConfigError("%s: capture %s: rows is 'N' or 'N-M'" % (w, name))
        elif not (isinstance(c, dict) and set(c) == {"field"}):
            raise FlowConfigError("%s: capture %s: give a regex, {field: ref} or "
                                  "{rows: 'N-M'}" % (w, name))
        st.capture[str(name)] = c
    if "call" in spec:
        st.call = str(spec["call"])
        args = spec.get("with") or {}
        if not isinstance(args, dict):
            raise FlowConfigError("%s: with must map parameter -> value" % w)
        st.call_with = dict(args)
        if st.fill or st.press or st.cursor or st.wait_for:
            raise FlowConfigError("%s: a call step can't also fill, press or wait" % w)
    elif "with" in spec:
        raise FlowConfigError("%s: with is only for call steps" % w)
    if "fail" in spec:
        st.fail = str(spec["fail"])
        if not st.when:
            raise FlowConfigError("%s: fail needs a when condition" % w)
        if st.fill or st.press or st.call:
            raise FlowConfigError("%s: a fail step can't also fill, press or call" % w)
    if "change_password" in spec:
        cp = spec["change_password"]
        if not isinstance(cp, dict) or not cp.get("secret") or \
                set(cp) - {"secret", "current", "new", "verify"}:
            raise FlowConfigError("%s: change_password takes secret (the password parameter) "
                                  "and optionally current, new and verify field labels" % w)
        if not (st.when or st.expect):
            raise FlowConfigError("%s: change_password needs when (the expired-password "
                                  "screen)" % w)
        if st.fill or st.press or st.call or st.fail or st.cursor:
            raise FlowConfigError("%s: a change_password step can't also fill, press, call "
                                  "or fail" % w)
        st.change_password = dict(CHANGE_PASSWORD_LABELS, **{k: str(v) for k, v in cp.items()})
    acts = bool(st.fill or st.cursor or (st.press and norm_key(st.press) != "reset"))
    if acts and not (st.expect or st.when):
        raise FlowConfigError("%s: a step that fills or presses needs expect (or when), so "
                              "it can't land on the wrong screen" % w)
    return st


class FlowSet:
    """All flows loaded from one or more YAML files or directories."""

    def __init__(self) -> None:
        self.flows: dict[str, Flow] = {}

    @classmethod
    def load(cls, paths: list[str | os.PathLike]) -> "FlowSet":
        fs = cls()
        for p in paths:
            p = Path(p).expanduser()
            files = sorted([*p.glob("*.yaml"), *p.glob("*.yml")]) if p.is_dir() else [p]
            if p.is_dir() and not files:
                raise FlowConfigError("%s: no .yaml files in this folder" % p)
            for f in files:
                fs.add_file(f)
        fs.validate()
        return fs

    def add_file(self, path: Path) -> None:
        try:
            doc = yaml.safe_load(path.read_text("utf-8"))
        except OSError as e:
            raise FlowConfigError("%s: %s" % (path, e.strerror or e)) from None
        except yaml.YAMLError as e:
            raise FlowConfigError("%s: not valid YAML: %s" % (path, e)) from None
        self.add_doc(doc, str(path))

    def add_doc(self, doc: Any, source: str = "<flows>") -> None:
        if not isinstance(doc, dict) or not isinstance(doc.get("flows"), dict):
            raise FlowConfigError("%s: expected a top-level 'flows:' mapping" % source)
        if set(doc) - {"flows", "toolset"}:
            raise FlowConfigError("%s: unknown top-level keys %s"
                                  % (source, sorted(set(doc) - {"flows", "toolset"})))
        default_ts = doc.get("toolset")
        for name, spec in doc["flows"].items():
            where = "%s: flow %s" % (source, name)
            if not NAME_RE.match(str(name)):
                raise FlowConfigError("%s: flow names must be lower_snake_case" % where)
            if name in self.flows:
                raise FlowConfigError("%s: defined twice (also in %s)"
                                      % (where, self.flows[name].source))
            if not isinstance(spec, dict):
                raise FlowConfigError("%s: must be a mapping" % where)
            bad = set(spec) - FLOW_KEYS
            if bad:
                raise FlowConfigError("%s: unknown keys %s" % (where, sorted(bad)))
            if not spec.get("description"):
                raise FlowConfigError("%s: needs a description (the agent reads it)" % where)
            params_spec = spec.get("params") or {}
            if not isinstance(params_spec, dict):
                raise FlowConfigError("%s: params must be a mapping" % where)
            params = {str(k): _parse_param(str(k), v, where) for k, v in params_spec.items()}
            steps_spec = spec.get("steps")
            if not isinstance(steps_spec, list) or not steps_spec:
                raise FlowConfigError("%s: needs a list of steps" % where)
            steps = [_parse_step(i, s, where) for i, s in enumerate(steps_spec, 1)]
            pace = spec.get("pace", 0)
            if not isinstance(pace, (int, float)) or isinstance(pace, bool) or not 0 <= pace <= 10:
                raise FlowConfigError("%s: pace is seconds between steps (0-10)" % where)
            self.flows[name] = Flow(
                name=name, description=str(spec["description"]).strip(),
                toolset=spec.get("toolset", default_ts), read_only=bool(spec.get("read_only")),
                expose=bool(spec.get("expose", True)), params=params, steps=steps,
                source=source, pace=float(pace))

    def validate(self) -> None:
        """Cross-flow checks: calls resolve, no cycles, templates name known
        parameters or captures, read-only flows don't call writing flows."""
        for f in self.flows.values():
            for p in f.params.values():
                if p.user_param and (p.user_param not in f.params
                                     or f.params[p.user_param].type == "secret"):
                    raise FlowConfigError("%s: flow %s: secret %s: user_param %r must name "
                                          "another parameter of this flow"
                                          % (f.source, f.name, p.name, p.user_param))
            known = set(f.params) | f.captures(self)
            for st in f.steps:
                w = "%s: flow %s step %s" % (f.source, f.name, st.label)
                rconds = [*st.repeat.until, *st.repeat.fail_if] if st.repeat else []
                texts = [*st.expect, *st.when, *st.fill.values(), *st.fill.keys(),
                         *[str(x) for x in st.params if isinstance(x, str)],
                         *[t for c in rconds for t in (*c.text, *c.not_text, *c.regex)],
                         *[r for c in rconds for rs in c.captured.values() for r in rs],
                         st.cursor or "", st.wait_for or "", st.fail or "",
                         *st.check.text, *st.check.not_text, *st.check.regex,
                         *[r for rs in st.check.captured.values() for r in rs],
                         *[c if isinstance(c, str) else str(c.get("field", ""))
                           for c in st.capture.values()],
                         *[str(v) for v in st.call_with.values()]]
                for t in texts:
                    unknown = _template_names(t) - known
                    if unknown:
                        raise FlowConfigError("%s: {%s} is not a parameter or capture of this "
                                              "flow" % (w, ", ".join(sorted(unknown))))
                if st.change_password:
                    sp = f.params.get(st.change_password["secret"])
                    if not sp or sp.type != "secret":
                        raise FlowConfigError("%s: change_password.secret must name a secret "
                                              "parameter of this flow" % w)
                unknown = set(st.check.captured) - known
                if unknown:
                    raise FlowConfigError("%s: check.captured: %s is not captured by this flow"
                                          % (w, ", ".join(sorted(unknown))))
                if st.call:
                    callee = self.flows.get(st.call)
                    if not callee:
                        raise FlowConfigError("%s: calls unknown flow %r" % (w, st.call))
                    passed = sorted(n for n in st.call_with
                                    if n in callee.params and callee.params[n].type == "secret")
                    if passed:
                        raise FlowConfigError("%s: %s is a secret of %s and can't be passed in"
                                              % (w, ", ".join(passed), st.call))
                    extra = set(st.call_with) - set(callee.params)
                    if extra:
                        raise FlowConfigError("%s: %s has no parameter %s"
                                              % (w, st.call, ", ".join(sorted(extra))))
                    missing = {n for n, p in callee.params.items()
                               if p.required and p.type != "secret"} - set(st.call_with)
                    if missing:
                        raise FlowConfigError("%s: call to %s is missing %s"
                                              % (w, st.call, ", ".join(sorted(missing))))
                    if f.read_only and not callee.read_only:
                        raise FlowConfigError("%s: read-only flow calls %s, which isn't "
                                              "read-only" % (w, st.call))
            self._no_cycle(f.name, [])

    def _no_cycle(self, name: str, stack: list[str]) -> None:
        if name in stack:
            raise FlowConfigError("flows call each other in a loop: %s"
                                  % " -> ".join([*stack, name]))
        for st in self.flows[name].steps:
            if st.call:
                self._no_cycle(st.call, [*stack, name])

    def select(self, toolsets: list[str] | None = None, read_only: bool = False,
               allow_secrets: bool = False) -> list[Flow]:
        """Flows to expose as tools: exposed, in the toolsets. Flows that need a
        secret only when a person can type it (allow_secrets: the viewer runs)."""
        out = []
        for f in self.flows.values():
            if not f.expose or (f.secrets(self) and not allow_secrets):
                continue
            if toolsets and f.toolset not in toolsets:
                continue
            if read_only and not f.read_only:
                continue
            out.append(f)
        return out


# ---------------------------------------------------------------- running
@dataclass
class FlowResult:
    flow: str
    captures: dict[str, str]
    log: list[str]
    screen: str

    def __str__(self) -> str:
        cap = "".join("\n  %s = %r" % kv for kv in self.captures.items())
        return "flow %s finished%s\nsteps:\n  %s\n%s" % (
            self.flow, ("\ncaptured:" + cap) if cap else "", "\n  ".join(self.log), self.screen)


class FlowRunner:
    """Runs flows on a session.

    Secrets: when somebody is watching the session in the viewer, the person
    is asked there (the value never reaches the caller or the log). Otherwise
    the parameter's env variable is used. `pace` waits that many seconds after
    each step so a person can follow along (a flow's own `pace` can be longer).
    """

    def __init__(self, mgr: Manager, flows: FlowSet,
                 env: Callable[[str], str | None] = os.environ.get, pace: float = 0.0,
                 prompt_timeout: float = 120.0):
        self.mgr, self.flows, self.env = mgr, flows, env
        self.pace, self.prompt_timeout = pace, prompt_timeout

    def run(self, s: Session, name: str, params: dict[str, Any]) -> FlowResult:
        flow = self.flows.flows.get(name)
        if not flow:
            raise FlowError("no flow %r (flows: %s)" % (name, ", ".join(self.flows.flows)))
        log: list[str] = []
        secrets: list[str] = []
        used: list[dict] = []           # where each password came from (see secret())
        self.precheck(flow, params)
        try:
            caps = self._run(s, flow, params, log, secrets, depth=0, used=used)
        except FlowError as e:
            # a saved password that didn't get the flow through is dropped: failed
            # sign-ons count toward disabling the profile
            for u in used:
                if u["source"] == "saved" and u["flow"] == e.flow:
                    self.mgr.creds.invalidate(*u["key"])
            raise
        for u in used:                  # typed, and the flow went through: keep it
            if u["source"] == "typed" and u["key"]:
                self.mgr.creds.put(*u["key"], u["value"], remember=u["remember"])
            if u.get("key"):
                s.user = u["key"][1]    # who is signed on (for the flow's SQL steps)
        return FlowResult(name, {k: v for k, v in caps.items() if k not in flow.params},
                          log, self._mask(render(s.snapshot()), secrets))

    @staticmethod
    def _mask(text: str, secrets: list[str]) -> str:
        for sec in secrets:
            if sec:
                text = text.replace(sec, "***")
        return text

    def precheck(self, flow: Flow, params: dict[str, Any], stack=()) -> None:
        """Before anything is sent: validate the parameters of this flow and of
        every flow it calls, and run the block list over every value that is
        already known (everything but screen captures). A flow is refused as a
        whole instead of stopping half-way."""
        vals = self.bind(flow, params, [], with_secrets=False)
        for st in flow.steps:
            if st.call:
                args = {}
                for k, v in st.call_with.items():
                    if isinstance(v, str):
                        if _template_names(v) - set(vals):
                            continue                 # depends on a capture
                        v = fill_template(v, vals)
                    args[k] = v
                callee = self.flows.flows[st.call]
                if {n for n, p in callee.params.items()
                        if p.required and p.type != "secret"} <= set(args):
                    self.precheck(callee, args, (*stack, flow.name))
                continue
            known = {}
            if st.query:
                known["sql"] = st.query
            for k, v in st.fill.items():
                if not (_template_names(k) | _template_names(v)) - set(vals):
                    known[fill_template(k, vals)] = fill_template(v, vals)
            try:
                self.mgr.guard.check_values(known)
            except PermissionError as e:
                raise FlowError("refused before starting: %s (in %s)"
                                % (e, " > ".join((*stack, flow.name, "step " + st.label))))

    def secret(self, s: Session | None, flow: Flow, p: Param, vals: dict[str, str],
               used: list[dict]) -> str:
        """A secret parameter's value: saved from an earlier sign-on (when the
        secret names its user_param), typed in the viewer, or from env."""
        key = None
        if p.user_param and s is not None and vals.get(p.user_param):
            key = (s.host, vals[p.user_param])
            saved = self.mgr.creds.get(*key)
            if saved:
                used.append({"source": "saved", "key": key, "flow": flow.name})
                return saved
        if s is not None and self.mgr.viewer_url and self.mgr.viewer_watching(s.name):
            try:
                v, remember = self.mgr.ask_secret(s.name, p.prompt or p.name, self.prompt_timeout,
                                                  offer_remember=key is not None)
            except GuardError as e:
                raise FlowError("%s: %s" % (flow.name, e)) from None
            used.append({"source": "typed", "key": key, "value": v, "remember": remember,
                         "flow": flow.name})
            return v
        v = self.env(p.env) if p.env else None
        if v:
            return v
        if self.mgr.viewer_url:
            raise FlowError("%s needs %s, typed by a person: give the user the viewer link (%s), "
                            "ask them to keep it open, then run the flow again"
                            % (flow.name, p.prompt or p.name, self.mgr.viewer_url))
        raise FlowError("%s needs the secret %s: %s" % (
            flow.name, p.name, "set $%s" % p.env if p.env else "open the viewer to type it"))

    def bind(self, flow: Flow, params: dict[str, Any], secrets: list[str],
             with_secrets: bool = True, s: Session | None = None,
             used: list[dict] | None = None) -> dict[str, str]:
        extra = set(params) - set(flow.params)
        if extra:
            raise FlowError("%s has no parameter %s (parameters: %s)" % (
                flow.name, ", ".join(sorted(extra)), ", ".join(
                    n for n, p in flow.params.items() if p.type != "secret") or "none"))
        passed = sorted(n for n in params if flow.params[n].type == "secret")
        if passed:
            raise FlowError("%s: %s is a secret; a person types it in the viewer (or it comes "
                            "from the environment), it can't be passed in"
                            % (flow.name, ", ".join(passed)))
        out: dict[str, str] = {}
        for n, p in flow.params.items():
            if p.type == "secret":
                continue                       # after the others: user_param must be known
            if n not in params or params[n] is None:
                if p.required:
                    raise FlowError("%s: missing parameter %s" % (flow.name, n))
                raw = p.default
            else:
                raw = params[n]
            try:
                out[n] = p.coerce(raw)
            except ValueError as e:
                raise FlowError("%s: %s" % (flow.name, e)) from None
        if with_secrets:
            for n, p in flow.params.items():
                if p.type == "secret":
                    v = self.secret(s, flow, p, out, used if used is not None else [])
                    secrets.append(v)
                    out[n] = v
        return out

    def _run(self, s: Session, flow: Flow, params: dict[str, Any], log: list[str],
             secrets: list[str], depth: int, used: list[dict] | None = None) -> dict[str, str]:
        used = used if used is not None else []
        vals = self.bind(flow, params, secrets, s=s, used=used)
        ind = "  " * depth
        pace = max(self.pace, flow.pace)
        s.log("flow", "%s%s %r" % (ind, flow.name, self._mask(repr(
            {k: v for k, v in vals.items()}), secrets)))
        s.wait(timeout=10)

        def fail(st: Step, msg: str) -> FlowError:
            return FlowError(msg, self._mask(render(s.snapshot()), secrets), flow.name, st.label)

        for st in flow.steps:
            tv = lambda t: fill_template(t, vals)  # noqa: E731
            rows = s.rows()
            if st.when and not all(self.mgr.guard.expect_matches(rows, tv(w)) for w in st.when):
                log.append("%s%s/%s: skipped" % (ind, flow.name, st.label))
                continue
            if st.fail:
                raise fail(st, tv(st.fail))
            if st.change_password:
                self._change_password(s, flow, st, vals, secrets, used, fail)
                log.append("%s%s/%s: password changed" % (ind, flow.name, st.label))
                continue
            if st.call:
                callee = self.flows.flows[st.call]
                args = {k: (tv(v) if isinstance(v, str) else v) for k, v in st.call_with.items()}
                log.append("%s%s/%s: call %s" % (ind, flow.name, st.label, st.call))
                sub = self._run(s, callee, args, log, secrets, depth + 1, used)
                vals.update({k: v for k, v in sub.items() if k not in callee.params})
                self._check(s, st, vals, fail)
                continue
            if st.repeat:
                did = self._repeat(s, flow, st, vals, secrets, fail, ind)
                log.append("%s%s/%s: %s" % (ind, flow.name, st.label, did))
                continue
            if st.query:
                text = self._query(s, st, vals, fail)
                self._check(s, st, vals, fail, text=text)
                log.append("%s%s/%s: query" % (ind, flow.name, st.label))
                continue
            expects = [tv(e) for e in (st.expect or st.when)]
            values = {tv(k): tv(v) for k, v in st.fill.items()}
            try:
                with s.cond:
                    for e in expects:
                        if not self.mgr.guard.expect_matches(s.rows(), e):
                            raise fail(st, "expected %r on the screen" % e)
                    sid = s.screen_id()
                if st.press:
                    ok = self.mgr.press(s, st.press, values, tv(st.cursor) if st.cursor else None,
                                        expects[0] if expects else None, sid, True,
                                        tv(st.wait_for) if st.wait_for else None, st.timeout,
                                        mask=secrets)
                    if not ok:
                        raise fail(st, "the host didn't answer within %gs%s" % (
                            st.timeout, " (waiting for %r)" % tv(st.wait_for)
                            if st.wait_for else ""))
                elif values or st.cursor:
                    self.mgr.fill(s, values, expects[0] if expects else None, sid, mask=secrets)
                    if st.cursor:
                        s.set_cursor(tv(st.cursor))
                elif st.wait_for:
                    if not s.wait(st.timeout, tv(st.wait_for)):
                        raise fail(st, "%r didn't appear within %gs" % (tv(st.wait_for),
                                                                          st.timeout))
            except GuardError as e:
                raise fail(st, str(e)) from None
            except FieldError as e:
                raise fail(st, str(e)) from None
            self._check(s, st, vals, fail)
            log.append("%s%s/%s: %s" % (ind, flow.name, st.label,
                                        ("pressed " + st.press) if st.press else
                                        "filled" if values else "checked"))
            if pace and (st.press or values or st.cursor):
                time.sleep(pace)
        return vals

    def _change_password(self, s: Session, flow: Flow, st: Step, vals: dict[str, str],
                         secrets: list[str], used: list[dict], fail) -> None:
        """The host says the password has expired: the person chooses a new one
        in the viewer, it goes into the change-password screen with the current
        one (which just worked), and the saved password is replaced. Nothing is
        ever retried with the old password; a wrong current password stops."""
        cp = st.change_password
        pname = cp["secret"]
        p = flow.params[pname]
        user = vals.get(p.user_param, "") if p.user_param else ""
        key = (s.host, user) if user else None
        if not (self.mgr.viewer_url and self.mgr.viewer_watching(s.name)):
            raise fail(st, "the password%s has expired. Nothing was retried. A person has to "
                       "choose a new one: give the user the viewer link%s and run the flow "
                       "again, or change it on the system" % (
                           " of " + user if user else "",
                           " (%s)" % self.mgr.viewer_url if self.mgr.viewer_url else ""))
        screen = [fill_template(t, vals) for t in (st.when or st.expect)]
        message = "The password has expired. Choose a new one."
        for _ in range(3):
            try:
                new, _remember = self.mgr.ask_secret(
                    s.name, "New password%s" % (" for " + user if user else ""),
                    self.prompt_timeout, kind="new", message=message)
            except GuardError as e:
                raise fail(st, str(e)) from None
            secrets.append(new)
            try:
                self.mgr.press(s, "Enter", {cp["current"]: vals[pname], cp["new"]: new,
                                            cp["verify"]: new}, None, screen[0], None, True,
                               None, 60.0, mask=secrets)
            except GuardError as e:
                raise fail(st, str(e)) from None
            if all(self.mgr.guard.expect_matches(s.rows(), t) for t in screen):
                msg = s.snapshot()["message_line"].strip()
                if s.keyboard_state() == "error":
                    self.mgr.press(s, "Reset")
                if "not correct" in msg.lower():     # the current password: never retry
                    raise fail(st, "the host rejected the current password: %s" % msg)
                message = "Not accepted: %s" % (msg or "see the screen")
                continue
            vals[pname] = new
            for u in used:                   # neither keep nor drop the old one at the end
                if u.get("key") == key:
                    u["source"], u["value"] = "changed", new
            if key:
                self.mgr.creds.replace(*key, new)
            self.mgr.on_password_changed(s.host, user)
            s.log("password changed", user or "(user unknown)")
            return
        raise fail(st, "the new password was not accepted three times (%s); nothing else was "
                   "tried" % message)

    # -- SQL steps and repeats ----------------------------------------------------
    def _query(self, s: Session, st: Step, vals: dict[str, str], fail) -> str:
        """Run the step's query (read-only, through the host servers) and
        capture columns of the first row. Returns the result as a text table."""
        from .hostservers import HostServerError, format_table, host_login
        hs = getattr(self.mgr, "hostservers", None)
        if hs is None:
            raise fail(st, "this server can't run SQL: start it with --jt400")
        conn = "flow-" + s.name
        if not hs.connected(conn):
            user = s.user or (s.profile.user if s.profile else None)
            if not user:
                raise fail(st, "don't know which user to run SQL as: sign on with a flow whose "
                               "password names its user_param, or set user in the profile")
            try:
                host_login(self.mgr, hs, conn, s.host, user, s.tls)
            except HostServerError as e:
                raise fail(st, str(e)) from None
        params = [fill_template(p, vals) if isinstance(p, str) else p for p in st.params]
        try:
            res = hs.query(conn, st.query, params, max_rows=100)
        except HostServerError as e:
            raise fail(st, str(e)) from None
        cols = [str(c).upper() for c in res.get("columns", [])]
        rows = res.get("rows", [])
        for name, col in st.capture.items():
            if col == "#rows":
                vals[name] = str(len(rows))
                continue
            if col.upper() not in cols:
                raise fail(st, "capture %s: the query has no column %s (columns: %s)"
                           % (name, col, ", ".join(cols)))
            v = rows[0][cols.index(col.upper())] if rows else None
            vals[name] = "" if v is None else str(v)
        return format_table(res)

    def _holds(self, s: Session, conds: list[Check], vals: dict[str, str],
               text: str | None) -> bool:
        """Does any of these conditions hold (on the query result `text`, or
        the screen, and the captures)?"""
        trx = lambda t: fill_template(t, {k: re.escape(v) for k, v in vals.items()})  # noqa
        tv = lambda t: fill_template(t, vals)  # noqa: E731
        rows = s.rows() if text is None else text.splitlines()
        screen = "\n".join(rows)
        for c in conds:
            kb = s.keyboard_state()
            if c.keyboard == "ready" and kb != "ready" or c.keyboard == "error" and kb != "error":
                continue
            if not all(self.mgr.guard.expect_matches(rows, tv(t)) for t in c.text):
                continue
            if any(self.mgr.guard.expect_matches(rows, tv(t)) for t in c.not_text):
                continue
            if not all(re.search(trx(rx), screen, re.M) for rx in c.regex):
                continue
            if not all(name in vals and re.search(trx(rx), vals[name], re.M)
                       for name, rxs in c.captured.items() for rx in rxs):
                continue
            return True
        return False

    def _repeat(self, s: Session, flow: Flow, st: Step, vals: dict[str, str],
                secrets: list[str], fail, ind: str) -> str:
        """Do the step (press, query, or just look at the screen) until `until`
        holds; stop at once when `fail_if` holds, or after `timeout`."""
        rp = st.repeat
        tv = lambda t: fill_template(t, vals)  # noqa: E731
        deadline = time.monotonic() + rp.timeout
        n = 0
        try:
            while True:
                n += 1
                text = None
                if st.query:
                    text = self._query(s, st, vals, fail)
                else:
                    expects = [tv(e) for e in (st.expect or st.when)]
                    try:
                        if st.press:
                            ok = self.mgr.press(s, st.press, {tv(k): tv(v)
                                                              for k, v in st.fill.items()},
                                                tv(st.cursor) if st.cursor else None,
                                                expects[0] if expects else None, None, True,
                                                None, st.timeout, mask=secrets)
                            if not ok:
                                raise fail(st, "the host didn't answer within %gs" % st.timeout)
                        else:
                            s.wait(min(rp.every, 5.0))
                    except GuardError as e:
                        raise fail(st, str(e)) from None
                    self._capture(s, st, vals, fail)
                if self._holds(s, rp.until, vals, text):
                    break
                if rp.fail_if and self._holds(s, rp.fail_if, vals, text):
                    raise fail(st, "stopped: the fail_if condition holds (attempt %d)%s" % (
                        n, self._shown(st, vals)))
                if time.monotonic() + rp.every > deadline:
                    raise fail(st, "still not done after %gs (%d attempts)%s" % (
                        rp.timeout, n, self._shown(st, vals)))
                with s.cond:
                    s.flow_status = "%s/%s: attempt %d, next in %gs%s" % (
                        flow.name, st.label, n, rp.every, self._shown(st, vals))
                    s.cond.notify_all()
                time.sleep(rp.every)
        finally:
            with s.cond:
                s.flow_status = None
        self._check(s, st, vals, fail, text=text, captured_already=True)
        return "done after %d attempt%s" % (n, "" if n == 1 else "s")

    @staticmethod
    def _shown(st: Step, vals: dict[str, str]) -> str:
        caps = ["%s=%s" % (k, vals.get(k, "?")) for k in st.capture]
        return "; last: " + ", ".join(caps) if caps else ""

    def _capture(self, s: Session, st: Step, vals: dict[str, str], fail) -> None:
        """Screen captures of a step (regex, field or rows)."""
        tv = lambda t: fill_template(t, vals)  # noqa: E731
        trx = lambda t: fill_template(t, {k: re.escape(v) for k, v in vals.items()})  # noqa
        rows = s.rows()
        screen = "\n".join(rows)
        for name, c in st.capture.items():
            if isinstance(c, str):
                m = re.search(trx(c), screen, re.M)
                if m:
                    vals[name] = m.group(1).strip()
            elif "rows" in c:
                m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*", str(c["rows"]))
                a, b = int(m.group(1)), int(m.group(2) or m.group(1))
                vals[name] = "\n".join(r.rstrip() for r in rows[a - 1:b]).strip("\n")
            else:
                try:
                    g = s.resolve(tv(str(c["field"])))
                except FieldError:
                    continue
                if not g[0].hidden:
                    vals[name] = s.buf.field_text(g, s.cp).strip()

    def _check(self, s: Session, st: Step, vals: dict[str, str], fail,
               text: str | None = None, captured_already: bool = False) -> None:
        tv = lambda t: fill_template(t, vals)  # noqa: E731
        # in regexes, substituted values match literally
        trx = lambda t: fill_template(t, {k: re.escape(v) for k, v in vals.items()})  # noqa
        if text is None:                     # a screen step (a query step leaves the screen)
            kb = s.keyboard_state()
            want = st.check.keyboard
            if kb == "disconnected":
                raise fail(st, "the session disconnected")
            if want is None and kb == "error":
                raise fail(st, "operator error: %r" % s.snapshot()["message_line"])
            if want == "ready" and kb != "ready":
                raise fail(st, "keyboard is %s, expected ready" % kb)
            if want == "error" and kb != "error":
                raise fail(st, "expected an operator error, keyboard is %s" % kb)
        where = "on the screen" if text is None else "in the query result"
        rows = s.rows() if text is None else text.splitlines()
        screen = "\n".join(rows)
        for t in st.check.text:
            if not self.mgr.guard.expect_matches(rows, tv(t)):
                raise fail(st, "check failed: %r is not %s" % (tv(t), where))
        for t in st.check.not_text:
            if self.mgr.guard.expect_matches(rows, tv(t)):
                raise fail(st, "check failed: %r is %s" % (tv(t), where))
        for rx in st.check.regex:
            if not re.search(trx(rx), screen, re.M):
                raise fail(st, "check failed: no match for /%s/" % trx(rx))
        for name, rxs in st.check.captured.items():
            if name not in vals:
                raise fail(st, "check failed: nothing captured as %s" % name)
            for rx in rxs:
                if not re.search(trx(rx), vals[name], re.M):
                    raise fail(st, "check failed: %s doesn't match /%s/; it is:\n%s"
                               % (name, trx(rx), vals[name]))
        if text is not None or captured_already:
            return                           # query steps and repeats captured already
        for name, c in st.capture.items():
            if isinstance(c, str):
                m = re.search(trx(c), screen, re.M)
                if not m:
                    raise fail(st, "capture %s: no match for /%s/" % (name, trx(c)))
                vals[name] = m.group(1).strip()
            elif "rows" in c:
                m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*", str(c["rows"]))
                a, b = int(m.group(1)), int(m.group(2) or m.group(1))
                vals[name] = "\n".join(r.rstrip() for r in rows[a - 1:b]).strip("\n")
            else:
                try:
                    g = s.resolve(tv(str(c["field"])))
                except FieldError as e:
                    raise fail(st, "capture %s: %s" % (name, e)) from None
                if g[0].hidden:
                    raise fail(st, "capture %s: hidden fields can't be captured" % name)
                vals[name] = s.buf.field_text(g, s.cp).strip()

