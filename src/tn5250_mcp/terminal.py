"""Python API: drive a 5250 session from a script or a test, no MCP client
needed. Same engine, guard and flows as the MCP server.

    from tn5250_mcp import Terminal

    with Terminal("pub400.com", flows=["flows/"]) as t:
        t.signon()                     # $T5250_USER / $T5250_PASSWORD
        r = t.run("add_order", customer="C100", qty=3)
        print(r.captures["order_no"])
"""

from __future__ import annotations

import os
import time
from typing import Any

from .flows import FlowError, FlowResult, FlowRunner, FlowSet
from .guard import Guard
from .manager import GuardError, Manager, render
from .profiles import Profile, ProfileSet
from .session import FieldError, Session

_CONTINUE_SCREENS = ("Display Program Messages", "Press Enter to continue")


class TerminalError(Exception):
    """Something went wrong driving the screen; the message includes the
    screen at the time."""


class Terminal:
    def __init__(self, host: str, port: int = 992, *, tls: bool = True,
                 verify_tls: bool = True, flows: FlowSet | list[str] | None = None,
                 session: str = "main", device_name: str | None = None, wide: bool = False,
                 codepage: str = "cp037", log_dir: str | None = None,
                 guard: Guard | None = None, viewer_port: int | None = None,
                 timeout: float = 20.0, pace: float = 0.0, reconnect_attempts: int = 0,
                 reconnect_interval: float = 5.0, jt400: str | None = None,
                 java: str | None = None):
        self.host, self.port, self.tls, self.verify_tls = host, port, tls, verify_tls
        self.name, self.device_name, self.wide = session, device_name, wide
        self.timeout = timeout
        # Scripts are written by people, so screen checks are optional here;
        # the block list still applies.
        self.mgr = Manager(guard or Guard(require_check=False), log_dir=log_dir,
                           default_codepage=codepage, reconnect_attempts=reconnect_attempts,
                           reconnect_interval=reconnect_interval)
        if flows is None or isinstance(flows, FlowSet):
            self.flows = flows
        else:
            self.flows = FlowSet.load(list(flows))
        self.runner = FlowRunner(self.mgr, self.flows or FlowSet(), pace=pace)
        self.mgr.flow_runner = self.runner
        self.profile: Profile | None = None
        if jt400:                              # SQL for flows' query steps
            from .hostservers import HostServers
            self.mgr.hostservers = HostServers(jt400, java, guard=self.mgr.guard)
        self._viewer_port = viewer_port
        self.s: Session | None = None

    # -- lifecycle ---------------------------------------------------------
    @classmethod
    def from_profile(cls, name: str, profiles: str | list | ProfileSet = "profiles.yaml",
                     **kw: Any) -> "Terminal":
        """A Terminal for a saved connection profile. connect() then runs the
        profile's after_connect flow (pass the flows that define it)."""
        if not isinstance(profiles, ProfileSet):
            profiles = ProfileSet.load([profiles] if isinstance(profiles, (str, os.PathLike))
                                       else list(profiles))
        p = profiles.get(name)
        t = cls(p.host, p.port, tls=p.tls, verify_tls=p.verify_tls, wide=p.wide,
                codepage=p.codepage or kw.pop("codepage", "cp037"), **kw)
        t.profile = p
        return t

    def connect(self, run_after_connect: bool = True) -> "Terminal":
        p = self.profile
        try:
            self.s = self.mgr.connect(self.name, self.host, self.port, self.tls,
                                      self.verify_tls,
                                      p.next_device_name() if p else self.device_name,
                                      self.wide, None, self.timeout)
        except GuardError as e:
            raise TerminalError(str(e)) from None
        self.s.profile = p
        if self._viewer_port is not None:
            self.start_viewer(self._viewer_port)
        if p and p.after_connect and run_after_connect:
            self.run_after_connect()
        return self

    def run_after_connect(self, wait_for_viewer: float = 0.0, announce=print) -> FlowResult:
        """Run the profile's after_connect flow (e.g. sign-on). If it needs a
        password that isn't in the environment, `wait_for_viewer` seconds are
        given for a person to open the viewer and type it there."""
        p = self.profile
        if not p or not p.after_connect:
            raise TerminalError("no after_connect flow (connect with a profile that has one)")
        flow = (self.flows.flows if self.flows else {}).get(p.after_connect["flow"])
        if not flow:
            raise TerminalError("profile %s runs flow %r after connecting; load the flows that "
                                "define it (flows=[...])" % (p.name, p.after_connect["flow"]))
        missing = [sp for sp in flow.secrets(self.flows)
                   if not (sp.env and os.environ.get(sp.env))]
        if missing and wait_for_viewer and not self.mgr.viewer_watching(self.name):
            url = self.start_viewer(self._viewer_port or 0)
            announce("Open the viewer to type the password: %s" % url)
            deadline = time.monotonic() + wait_for_viewer
            while not self.mgr.viewer_watching(self.name):
                if time.monotonic() > deadline:
                    raise TerminalError("nobody opened the viewer within %gs" % wait_for_viewer)
                time.sleep(0.5)
        return self.runner.run(self.session, flow.name, dict(p.after_connect["with"]))

    def close(self) -> None:
        if self.s is not None and self.name in self.mgr.sessions:
            self.mgr.disconnect(self.name)
        self.s = None
        if self.mgr.hostservers is not None:
            self.mgr.hostservers.close()

    def __enter__(self) -> "Terminal":
        return self.connect() if self.s is None else self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def session(self) -> Session:
        if self.s is None:
            raise TerminalError("not connected; call connect() first")
        return self.s

    def start_viewer(self, port: int = 0) -> str:
        """Start the live viewer (port 0 = any free port) and return its URL."""
        if not self.mgr.viewer_url:
            from .viewer import start_viewer
            self.mgr.viewer_url = start_viewer(self.mgr, port)
        return self.mgr.viewer_url

    @property
    def viewer_url(self) -> str | None:
        return self.mgr.viewer_url

    # -- reading -------------------------------------------------------------
    def screen(self, fields: bool = True) -> str:
        return render(self.session.snapshot(), show_fields=fields)

    @property
    def rows(self) -> list[str]:
        return self.session.rows()

    @property
    def text(self) -> str:
        return "\n".join(self.rows)

    @property
    def keyboard(self) -> str:
        return self.session.keyboard_state()

    def on(self, expect: str) -> bool:
        """True if `expect` (text, or "row:text") is on the screen."""
        return self.mgr.guard.expect_matches(self.rows, expect)

    def assert_on(self, expect: str) -> None:
        if not self.on(expect):
            raise TerminalError("expected %r on the screen\n%s" % (expect, self.screen()))

    def on_signon(self) -> bool:
        """A sign-on screen: a hidden (password) input field next to a visible
        one, and the word 'password' or 'sign on' on screen. Works for custom
        sign-on screens too (PUB400's has no 'Sign On' title)."""
        return self.session.on_signon_screen()

    def field(self, ref: str) -> str:
        """Current text of an input field (not for hidden fields)."""
        s = self.session
        try:
            g = s.resolve(ref)
        except FieldError as e:
            raise TerminalError(str(e)) from None
        if g[0].hidden:
            raise TerminalError("field %s is hidden" % ref)
        return s.buf.field_text(g, s.cp).rstrip()

    # -- acting --------------------------------------------------------------
    def fill(self, values: dict[str, str], expect: str | None = None) -> None:
        try:
            self.mgr.fill(self.session, values, expect)
        except GuardError as e:
            raise TerminalError("%s\n%s" % (e, self.screen())) from None

    def press(self, key: str, values: dict[str, str] | None = None, *,
              expect: str | None = None, cursor: str | None = None,
              wait_for: str | None = None, timeout: float = 30.0, _mask=()) -> str:
        """Fill (optional), send a key, wait for the answer, return the screen."""
        try:
            ok = self.mgr.press(self.session, key, values, cursor, expect, None, True,
                                wait_for, timeout, mask=_mask)
        except GuardError as e:
            raise TerminalError("%s\n%s" % (e, self.screen())) from None
        if not ok:
            raise TerminalError("the host didn't answer within %gs%s\n%s" % (
                timeout, " (waiting for %r)" % wait_for if wait_for else "", self.screen()))
        return self.screen()

    def wait(self, text: str | None = None, timeout: float = 30.0) -> bool:
        return self.session.wait(timeout, text)

    def run(self, flow: str, **params: Any) -> FlowResult:
        """Run a screen flow. Raises FlowError (with the screen) on failure."""
        if not self.flows:
            raise TerminalError("no flows loaded (pass flows=[...] to Terminal)")
        return self.runner.run(self.session, flow, params)

    # -- recording -------------------------------------------------------------
    def record_start(self) -> None:
        """Record every key pressed from now on (here or in the viewer)."""
        try:
            self.mgr.record_start(self.session)
        except GuardError as e:
            raise TerminalError(str(e)) from None

    def record_stop(self, name: str | None = None, description: str = "",
                    save_dir: str | None = None, params: str = "auto") -> str | None:
        """Stop recording; return the flow as YAML (also written to
        save_dir/<name>.yaml when given). No name discards the recording."""
        self.mgr.record_dir = save_dir
        try:
            text, _ = self.mgr.record_stop(self.session, name, description, bool(save_dir),
                                           params)
        except GuardError as e:
            raise TerminalError(str(e)) from None
        return text

    # -- sign on / off -------------------------------------------------------
    def signon(self, user: str | None = None, password: str | None = None, *,
               user_env: str = "T5250_USER", password_env: str = "T5250_PASSWORD",
               timeout: float = 60.0) -> str:
        """Sign on with credentials from the arguments or the environment
        (meant for CI secrets). Handles the usual 'press Enter' screens after
        sign-on. Returns the first screen after signing on."""
        user = user or os.environ.get(user_env)
        password = password or os.environ.get(password_env)
        if not user or not password:
            raise TerminalError("sign-on needs $%s and $%s" % (user_env, password_env))
        s = self.session
        s.wait(10)
        if not self.on_signon():
            raise TerminalError("not on a sign-on screen\n%s" % self.screen())
        fields = s.buf.input_fields()
        pw = next((g for g in fields if g[0].hidden), None)
        if pw is None:
            raise TerminalError("no password field on the Sign On screen")
        try:
            ug = s.resolve("user")
            if ug[0].hidden:
                raise FieldError("hidden")
        except FieldError:
            ug = next(g for g in fields if not g[0].hidden)
        ref = lambda g: "%d,%d" % s.buf.rowcol(g[0].start)  # noqa: E731
        self.press("Enter", {ref(ug): user, ref(pw): password}, timeout=timeout,
                   _mask=[password])
        return self._after_signon(timeout)

    def signon_in_viewer(self, timeout: float = 600.0, announce=print) -> str:
        """Let a person sign on in the live viewer; wait until they have."""
        url = self.start_viewer(self._viewer_port or 0)
        announce("Sign on in the viewer: %s" % url)
        deadline = time.monotonic() + timeout
        while self.on_signon():
            if time.monotonic() > deadline:
                raise TerminalError("nobody signed on within %gs" % timeout)
            self.session.wait(2.0)
            time.sleep(0.5)
        return self._after_signon(60.0)

    def _after_signon(self, timeout: float) -> str:
        for _ in range(5):
            self.session.wait(timeout)
            if self.on_signon():
                msg = self.session.snapshot()["message_line"].strip()
                raise TerminalError("sign-on refused: %s" % (msg or "(no message)"))
            if self.keyboard == "error":
                raise TerminalError("sign-on refused: %s"
                                    % self.session.snapshot()["message_line"].strip())
            screen = next((t for t in _CONTINUE_SCREENS if self.on(t)), None)
            if not screen:
                return self.screen()
            self.press("Enter", expect=screen, timeout=timeout)
        raise TerminalError("still not past the sign-on messages\n%s" % self.screen())

    def signoff(self) -> str:
        """Sign off from the current screen's command line, then close.
        Returns what happened ("signed off", or why not)."""
        try:
            note = self.mgr.signoff(self.session)
        except OSError as e:
            note = "not signed off: %s" % e
        self.close()
        return note


__all__ = ["Terminal", "TerminalError", "FlowError", "FlowResult", "FlowSet"]
