"""Session registry, guard enforcement, logging and text rendering. Shared by
the MCP server and the web viewer."""

from __future__ import annotations

import datetime as _dt
import os
import secrets as _secrets
import threading
import time

from .guard import Guard
from .session import FieldError, ProtocolError, Session, norm_key


class GuardError(Exception):
    """A request refused by a safety check; the message says why."""


def render(snap: dict, show_fields: bool = True) -> str:
    cols = snap["cols"]
    out = []
    out.append("session %s @ %s | screen_id %s | keyboard %s | cursor %d,%d%s" % (
        snap["session"], snap["host"], snap["screen_id"], snap["keyboard"].upper(),
        snap["cursor"]["row"], snap["cursor"]["col"],
        " | MESSAGE WAITING" if snap["message_waiting"] else ""))
    if snap["keyboard"] == "error":
        out.append("operator error: %r -- press Reset to continue" % snap["message_line"])
    elif snap["keyboard"] == "busy":
        out.append("host is still working (X SYSTEM); call wait")
    elif snap["keyboard"] == "disconnected":
        out.append("disconnected%s" % (": " + snap["error"] if snap["error"] else ""))
    tens = "".join(str(i // 10 % 10) if i % 10 == 0 else " " for i in range(1, cols + 1))
    ones = "".join(str(i % 10) for i in range(1, cols + 1))
    out.append("    " + tens)
    out.append("    " + ones)
    for i, line in enumerate(snap["text"], 1):
        out.append("%02d |%s" % (i, line.rstrip()))
    if show_fields:
        if snap["fields"]:
            out.append("input fields (id = row,col; refer to a field by id, #index or label):")
            for f in snap["fields"]:
                flags = [f["type"]]
                if f["hidden"]:
                    flags.append("hidden")
                if f["mandatory_entry"]:
                    flags.append("mandatory")
                if f["mandatory_fill"]:
                    flags.append("must-fill")
                if f["right_adjust"]:
                    flags.append("right-adjust-" + f["right_adjust"])
                if f["monocase"]:
                    flags.append("uppercase")
                if f["auto_enter"]:
                    flags.append("auto-enter")
                if f["modified"]:
                    flags.append("modified")
                if len(f["segments"]) > 1:
                    flags.append("%d lines" % len(f["segments"]))
                if f["hidden"]:
                    val = "(hidden, %s)" % ("has a value" if f["has_value"] else "empty")
                else:
                    val = repr(f["value"])
                out.append("  #%-2d %-6s len %-4d %-24s %s  [%s]" % (
                    f["index"], f["id"], f["length"], (f["label"] or "-")[:24], val,
                    ", ".join(flags)))
        else:
            out.append("no input fields on this screen")
    return "\n".join(out)


class Manager:
    def __init__(self, guard: Guard, log_dir: str | None = None,
                 default_codepage: str = "cp037"):
        self.guard = guard
        self.log_dir = log_dir
        self.default_codepage = default_codepage
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()
        self.viewer_url: str | None = None
        self.viewer_seen: dict[str, float] = {}     # session -> last viewer poll
        self.prompts: dict[str, dict] = {}          # session -> open secret prompt

    # -- secrets typed by a person in the viewer -----------------------------
    def viewer_watching(self, name: str, within: float = 3.0) -> bool:
        """Is somebody looking at this session in the viewer right now?"""
        return time.monotonic() - self.viewer_seen.get(name, -1e9) < within

    def ask_secret(self, name: str, label: str, timeout: float = 120.0) -> str:
        """Show a password prompt in the viewer for this session and wait for
        the person to answer. The value never reaches the agent or the log."""
        p = {"id": _secrets.token_hex(6), "label": label, "value": None,
             "cancelled": False, "event": threading.Event()}
        with self.lock:
            if name in self.prompts:
                raise GuardError("a password prompt is already open in the viewer")
            self.prompts[name] = p
        try:
            if not p["event"].wait(timeout):
                raise GuardError("nobody entered %s in the viewer within %ds" % (label, timeout))
            if p["cancelled"] or not p["value"]:
                raise GuardError("%s was not entered (cancelled in the viewer)" % label)
            return p["value"]
        finally:
            with self.lock:
                if self.prompts.get(name) is p:
                    del self.prompts[name]

    def open_prompt(self, name: str) -> dict | None:
        p = self.prompts.get(name)
        return {"id": p["id"], "label": p["label"]} if p else None

    def answer_prompt(self, name: str, pid: str, value: str | None) -> bool:
        with self.lock:
            p = self.prompts.get(name)
            if not p or p["id"] != pid:
                return False
            p["value"], p["cancelled"] = value, value is None
            p["event"].set()
            return True

    # -- logging -------------------------------------------------------------
    def _logger(self, name: str):
        if not self.log_dir:
            return lambda *a: None
        os.makedirs(self.log_dir, exist_ok=True)
        path = os.path.join(self.log_dir, "%s-%s.log" % (
            name, _dt.datetime.now().strftime("%Y%m%d-%H%M%S")))
        lock = threading.Lock()

        def log(kind: str, text: str) -> None:
            with lock, open(path, "a", encoding="utf-8") as fh:
                fh.write("[%s] %s\n%s\n\n" % (_dt.datetime.now().isoformat(timespec="seconds"),
                                              kind, text))
        return log

    def log(self, s: Session, kind: str, text: str) -> None:
        s.log(kind, text)

    @staticmethod
    def masked(s: Session, values: dict[str, str], mask=()) -> dict[str, str]:
        """Values for the log: hidden fields and known secrets become ***."""
        out = {}
        for ref, v in values.items():
            try:
                hidden = s.resolve(ref)[0].hidden
            except FieldError:
                hidden = False
            out[ref] = "***" if hidden or any(m and m in v for m in mask) else v
        return out

    # -- sessions ------------------------------------------------------------
    def get(self, name: str) -> Session:
        s = self.sessions.get(name)
        if not s:
            raise GuardError("no session %r (open one with connect). Sessions: %s" % (
                name, ", ".join(self.sessions) or "none"))
        return s

    def connect(self, name: str, host: str, port: int, tls: bool, verify_tls: bool,
                device_name: str | None, wide: bool, codepage: str | None,
                timeout: float) -> Session:
        try:
            self.guard.check_host(host)
        except PermissionError as e:
            raise GuardError(str(e)) from None
        with self.lock:
            old = self.sessions.get(name)
            if old and old.connected:
                raise GuardError("session %r is already connected to %s; disconnect it "
                                 "first or pick another session name" % (name, old.host))
            s = Session(name, host, port, tls=tls, verify_tls=verify_tls,
                        device_name=device_name, wide=wide,
                        codepage=codepage or self.default_codepage, log=self._logger(name))
            self.sessions[name] = s
        s.connect(timeout=timeout)
        s.log("connect", "%s:%d tls=%s" % (host, port, tls))
        s.wait(timeout=timeout)
        return s

    def disconnect(self, name: str) -> None:
        s = self.get(name)
        s.close()
        s.log("disconnect", "")
        with self.lock:
            self.sessions.pop(name, None)

    # -- guard -----------------------------------------------------------------
    def check_screen(self, s: Session, expect: str | None, screen_id: str | None) -> None:
        if self.guard.require_check and not expect and not screen_id:
            raise GuardError(
                "refused: pass `expect` (text that must be on the current screen, "
                "optionally 'row:text') or `screen_id` (from your last get_screen) so "
                "the action can't land on the wrong screen")
        if screen_id and screen_id != s.screen_id():
            raise GuardError(
                "refused: the screen changed since screen_id %s (now %s). Someone or "
                "something else updated it; call get_screen and decide again."
                % (screen_id, s.screen_id()))
        if expect and not self.guard.expect_matches(s.rows(), expect):
            raise GuardError("refused: expected %r on the screen, but it isn't there. "
                             "Row 1 now reads %r." % (expect, s.rows()[0].strip()))

    # -- actions ---------------------------------------------------------------
    def fill(self, s: Session, values: dict[str, str], expect=None, screen_id=None,
             from_viewer: bool = False, mask=()) -> None:
        if not from_viewer:
            try:
                self.guard.check_values(values)
            except PermissionError as e:
                raise GuardError(str(e)) from None
        with s.cond:
            if not from_viewer:
                self.check_screen(s, expect, screen_id)
            try:
                s.fill(values, from_viewer=from_viewer)
            except (FieldError, ProtocolError) as e:
                raise GuardError(str(e)) from None
        s.log("fill" + (" (viewer)" if from_viewer else ""),
              repr(self.masked(s, values, mask)))

    def press(self, s: Session, key: str, values: dict[str, str] | None = None,
              cursor: str | None = None, expect=None, screen_id=None,
              wait: bool = True, wait_for: str | None = None, timeout: float = 30.0,
              from_viewer: bool = False, mask=()) -> bool:
        values = values or {}
        if not from_viewer:
            try:
                self.guard.check_values(values)
            except PermissionError as e:
                raise GuardError(str(e)) from None
        with s.cond:
            if not from_viewer and norm_key(key) != "reset":
                self.check_screen(s, expect, screen_id)
            try:
                if values:
                    s.fill(values, from_viewer=from_viewer)
                if cursor:
                    s.set_cursor(cursor, from_viewer=from_viewer)
                s.press(key)
            except (FieldError, ProtocolError) as e:
                raise GuardError(str(e)) from None
        s.log("press %s%s" % (key, " (viewer)" if from_viewer else ""),
              "values=%r cursor=%r" % (self.masked(s, values, mask), cursor))
        ok = True
        if wait and norm_key(key) != "reset":
            time.sleep(0.05)
            ok = s.wait(timeout=timeout, text=wait_for)
        text = render(s.snapshot())
        for m in mask:
            if m:
                text = text.replace(m, "***")
        s.log("screen", text)
        return ok
