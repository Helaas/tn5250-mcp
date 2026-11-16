"""Record what a person (in the viewer) or the agent does on a session and
turn it into a screen flow.

Every key press becomes a step: `expect` is the title of the screen it was
pressed on, `fill` the fields typed (by label where the label is unique),
`check` the title of the screen that came back. Typed values become
parameters with the recorded value as default, so the flow can be replayed
as is or called with other values. Passwords (hidden fields) are never
recorded: they become a `secret` parameter, typed in the viewer at run time.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .flows import NAME_RE, FlowConfigError, FlowSet
from .session import FieldError, Session, norm_key

CURSOR_KEYS = {"f4", "help", "f1"}          # keys that act on the cursor position
_JUNK = re.compile(r"^(\d{1,2}[/.:-]\d{1,2}[/.:-]\d{1,4}|\d{1,2}:\d{2}(:\d{2})?|"
                   r"System:?|Page \d+|More\.*|Bottom)$", re.I)


def screen_title(rows: list[str]) -> str | None:
    """A short text that identifies the screen, as an `expect` ("row:text"):
    the longest piece of the first non-blank row near the top, leaving out
    dates, times and system names."""
    for r in [*range(min(3, len(rows))), *range(3, len(rows))]:
        chunks = [c.strip(" .:") for c in re.split(r"\s{2,}", rows[r].strip())]
        chunks = [c for c in chunks if len(c) >= 3 and not _JUNK.match(c)]
        if chunks:
            return "%d:%s" % (r + 1, max(chunks, key=len))
    return None


def _esc(text: str) -> str:
    """Literal text inside a flow template."""
    return text.replace("{", "{{").replace("}", "}}")


def _snake(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    if not s or not s[0].isalpha():
        s = "field_" + s
    return s[:40].rstrip("_")


@dataclass
class _Fill:
    ref: str              # label (unique on that screen) or "row,col"
    value: str | None     # None for hidden fields: never stored
    hidden: bool
    numeric: bool
    length: int


@dataclass
class _Step:
    expect: str | None
    fills: list[_Fill]
    key: str
    cursor: str | None
    check: str | None = None
    keyboard_error: bool = False


@dataclass
class Recorder:
    session: str
    steps: list[_Step] = field(default_factory=list)
    pending: list[_Fill] = field(default_factory=list)

    # -- capturing (called by Manager under the session lock) ---------------
    def _fills(self, s: Session, values: dict[str, str]) -> list[_Fill]:
        rows = s.rows()
        out = []
        for ref, value in values.items():
            try:
                g = s.resolve(ref)
            except FieldError:
                continue                       # the action itself will fail and say why
            f0 = g[0]
            r, c = s.buf.rowcol(f0.start)
            label = s.buf.label_for(f0, rows)
            use = "%d,%d" % (r, c)
            if label:
                try:
                    if s.resolve(label)[0] is f0:
                        use = label
                except FieldError:
                    pass
            out.append(_Fill(use, None if f0.hidden else value, f0.hidden,
                             f0.shift in ("numeric_only", "digits_only", "signed_numeric")
                             or f0.signed, sum(f.length for f in g)))
        return out

    def on_fill(self, s: Session, values: dict[str, str]) -> None:
        self.pending.extend(self._fills(s, values))

    def before_press(self, s: Session, key: str, values: dict[str, str],
                     cursor: str | None) -> _Step:
        fills = self.pending + self._fills(s, values)
        self.pending = []
        cur = None
        if cursor and norm_key(key) in CURSOR_KEYS:
            cur = cursor
            try:
                g = s.resolve(cursor)
                lab = s.buf.label_for(g[0], s.rows())
                if lab and s.resolve(lab)[0] is g[0]:
                    cur = lab
            except FieldError:
                pass
        reset = norm_key(key) == "reset"
        return _Step(None if reset else screen_title(s.rows()), fills, key, cur)

    def after_press(self, s: Session, step: _Step) -> None:
        if norm_key(step.key) != "reset":
            step.keyboard_error = s.keyboard_state() == "error"
            step.check = None if step.keyboard_error else screen_title(s.rows())
        self.steps.append(step)
        s.recording_steps = len(self.steps)

    # -- output ------------------------------------------------------------
    def to_doc(self, name: str, description: str, params: str = "auto") -> dict:
        if not NAME_RE.match(name):
            raise FlowConfigError("flow name %r must be lower_snake_case" % name)
        if not self.steps:
            raise FlowConfigError("nothing was recorded: no keys were pressed")
        pdefs: dict[str, dict] = {}
        steps = []
        first_expect = None
        for i, st in enumerate(self.steps, 1):
            d: dict = {"name": "step %d" % i}
            if st.expect:
                d["expect"] = _esc(st.expect)
                first_expect = first_expect or st.expect
            fill = {}
            for f in st.fills:
                if f.hidden:
                    pdefs.setdefault("password", {"type": "secret", "env": "T5250_PASSWORD",
                                                  "prompt": "Password"})
                    fill[_esc(f.ref)] = "{password}"
                elif params == "auto" and f.value:
                    base = _snake(f.ref if not re.fullmatch(r"\d+,\d+", f.ref)
                                  else "field_" + f.ref.replace(",", "_"))
                    pname, k = base, 2
                    while pname in pdefs or pname in ("session", "password"):
                        pname, k = "%s_%d" % (base, k), k + 1
                    if f.numeric and re.fullmatch(r"[+-]?\d+", f.value.strip()):
                        pdefs[pname] = {"type": "integer", "default": int(f.value)}
                    else:
                        pdefs[pname] = {"type": "string", "max_length": f.length,
                                        "default": f.value}
                    fill[_esc(f.ref)] = "{%s}" % pname
                else:
                    fill[_esc(f.ref)] = _esc(f.value or "")
            if fill:
                d["fill"] = fill
            if st.cursor:
                d["cursor"] = _esc(st.cursor)
            d["press"] = st.key
            if st.keyboard_error:
                d["check"] = {"keyboard": "error"}
            elif st.check:
                d["check"] = {"text": _esc(st.check)}
            if (fill or (st.cursor) or norm_key(st.key) != "reset") and "expect" not in d:
                d["expect"] = _esc(first_expect or "")   # never act without a screen check
                if not d["expect"]:
                    raise FlowConfigError("step %d: no text on the screen to check against" % i)
            steps.append(d)
        flow: dict = {"description": description or "Recorded flow."}
        if pdefs:
            flow["params"] = pdefs
        flow["steps"] = steps
        return {"toolset": "recorded", "flows": {name: flow}}

    def to_yaml(self, name: str, description: str, params: str = "auto") -> str:
        doc = self.to_doc(name, description, params)
        fs = FlowSet()
        fs.add_doc(doc, "recording")
        fs.validate()                               # what we hand out must load
        head = ("# Recorded on session %s. Review the expect/check texts and the\n"
                "# parameters before relying on it.\n" % self.session)
        return head + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100)

    def save(self, text: str, directory: str, name: str) -> str:
        Path(directory).mkdir(parents=True, exist_ok=True)
        path = os.path.join(directory, name + ".yaml")
        try:
            with open(path, "x", encoding="utf-8") as fh:
                fh.write(text)
        except FileExistsError:
            raise FlowConfigError("%s already exists; pick another name" % path) from None
        return path
