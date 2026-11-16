"""MCP server exposing 5250 sessions as tools."""

from __future__ import annotations

import argparse
import inspect
import os
import sys
from typing import Annotated, Any

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from .flows import Flow, FlowConfigError, FlowError, FlowRunner, FlowSet
from .guard import DEFAULT_BLOCKED, Guard
from .manager import GuardError, Manager, render
from .profiles import ProfileError, ProfileSet
from .session import ALL_KEYS

INSTRUCTIONS = """\
Drive IBM i (AS/400) 5250 green-screen sessions.

Workflow: connect -> get_screen -> fill / press -> read the screen that press returns.
- connect with a saved profile ({"profile": name}; list_profiles shows them) when
  there are any; a profile may sign on by itself after connecting.
- Every fill/press must carry `expect` (text that must be on the current screen,
  or "row:text") or `screen_id` (from the last screen you read). If the screen
  changed - because the host moved on or the human typed in the viewer - the
  call is refused so nothing lands on the wrong screen. Re-read and decide again.
- Fields are referred to by id ("row,col"), "#index" or label as listed under the
  screen. Values are placed per the field's rules (right-adjust, signed numbers,
  uppercase); press sends only fields you changed.
- Signing on: never type passwords. Give the user the viewer link (viewer tool)
  and let them sign on there, then continue. Hidden fields are never shown to you.
- keyboard ERROR means an operator error: read the message line, press Reset.
  keyboard BUSY means the host is still working: call wait.
- Other tools may be screen flows defined by the user (whole procedures such as
  "add an order"). When one fits the task, use it instead of fill/press: it
  checks every screen on the way and validates its parameters. A flow that
  needs a password (e.g. a sign-on flow) asks the user for it in the viewer;
  give them the viewer link first.
- If the host drops the connection, the server reconnects by itself; the
  session then starts over at the sign-on screen. Call wait, then sign on again.
- record_start / record_stop turn what you (and the user in the viewer) do on a
  session into a reusable screen flow (YAML).
- press refuses keys a real terminal would refuse (mandatory entry, mandatory
  fill, wrong check digit) and says which field to fix; nothing is sent then.
"""

BUILTIN_TOOLS = {"connect", "disconnect", "list_sessions", "get_screen", "fill", "press",
                 "wait", "viewer", "keys", "record_start", "record_stop", "list_profiles"}

Session_ = Annotated[str, Field(description="session name; lets you run several side by side")]


def _flow_annotation(p) -> Any:
    """Type + JSON-schema hints for a flow parameter. Constraints are only
    advertised here; FlowRunner enforces them (one set of rules everywhere)."""
    hints: dict[str, Any] = {}
    if p.type == "boolean":
        t: Any = bool
    elif p.type == "integer":
        t = int
    elif p.type == "number":
        t = float
    else:
        t = str
    for k, js in (("min_length", "minLength"), ("max_length", "maxLength"),
                  ("pattern", "pattern"), ("enum", "enum"), ("min", "minimum"),
                  ("max", "maximum")):
        v = getattr(p, k)
        if v is not None and not (p.upper and k in ("pattern", "enum")):
            hints[js] = v
    desc = p.description
    if p.upper:
        desc = (desc + " " if desc else "") + "(uppercased)"
    if p.decimals is not None:
        desc = (desc + " " if desc else "") + "(max %d decimals)" % p.decimals
    return Annotated[t, Field(description=desc or None, json_schema_extra=hints or None)]


def _start_screen(f: Flow, flows: FlowSet) -> list[str]:
    """What the first screen-checking step expects, following calls."""
    for st in f.steps:
        if st.call:
            got = _start_screen(flows.flows[st.call], flows)
            if got:
                return got
        elif st.expect or st.when:
            return st.expect or st.when
    return []


def _flow_description(f: Flow, flows: FlowSet) -> str:
    start = _start_screen(f, flows)
    secret = sorted({p.prompt or p.name for p in f.secrets(flows)})
    return "%s\n\n(screen flow%s%s)%s" % (
        f.description, "; starts on a screen showing %s" % " + ".join(map(repr, start))
        if start else "", "; read-only" if f.read_only else "",
        ("\nNeeds %s, which the user types in the viewer: give them the viewer link and ask "
         "them to keep it open before you run this." % " and ".join(secret)) if secret else "")


def build_server(mgr: Manager, flows: FlowSet | None = None,
                 toolsets: list[str] | None = None, flows_only: bool = False,
                 read_only: bool = False, pace: float = 0.0,
                 profiles: ProfileSet | None = None, profiles_only: bool = False) -> MCPServer:
    mcp = MCPServer("tn5250", instructions=INSTRUCTIONS, version="0.4.0")
    flows = flows if flows is not None else FlowSet()
    runner = FlowRunner(mgr, flows, pace=pace)
    mgr.flow_runner = runner
    profiles = profiles if profiles is not None else ProfileSet()
    mgr.name_taken = lambda n: n in BUILTIN_TOOLS or n in flows.flows

    async def run(fn, *a, **k):
        try:
            return await anyio.to_thread.run_sync(lambda: fn(*a, **k))
        except (GuardError, FlowError, ProfileError) as e:
            raise ToolError(str(e)) from None
        except (OSError, ConnectionError) as e:
            raise ToolError("connection problem: %s" % e) from None

    def viewer_hint() -> str:
        return ("\nviewer for the human (watch / sign on): %s" % mgr.viewer_url
                if mgr.viewer_url else "")

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                          open_world_hint=True))
    async def connect(
        profile: Annotated[str | None, Field(
            description="saved connection profile (see list_profiles); then leave out the "
                        "connection details")] = None,
        host: Annotated[str | None, Field(
            description="IBM i host name, e.g. pub400.com (when not using a profile)")] = None,
        port: Annotated[int | None, Field(
            description="992 for TLS (default), 23 for plain telnet")] = None,
        tls: bool | None = None,
        session: Session_ = "main",
        verify_tls: bool | None = None,
        device_name: Annotated[str | None, Field(
            description="ask for a specific display device name")] = None,
        wide: Annotated[bool | None, Field(
            description="27x132 display (3477) instead of 24x80")] = None,
        codepage: Annotated[str | None, Field(
            description="EBCDIC code page, e.g. cp037 (US), cp273 (DE), cp500 (intl)")] = None,
    ) -> str:
        """Open a 5250 session and return the first screen: Sign On, or the screen
        after the profile's after-connect flow (e.g. a menu once signed on)."""
        details = {k: v for k, v in dict(host=host, port=port, tls=tls, verify_tls=verify_tls,
                                         device_name=device_name, wide=wide,
                                         codepage=codepage).items() if v is not None}
        note = None
        if profile:
            if details:
                raise ToolError("connect with either a profile or connection details, not "
                                "both (profile %s already has: %s)" % (
                                    profile, ", ".join(sorted(details))))
            p = await run(profiles.get, profile)
            s, note = await run(mgr.connect_profile, session, p, 20.0)
        else:
            if profiles_only:
                raise ToolError("this server only connects through saved profiles: %s" % (
                    ", ".join(profiles.profiles) or "none configured"))
            if not host:
                raise ToolError("give a profile (%s) or a host" % (
                    ", ".join(profiles.profiles) or "no profiles configured"))
            use_tls = True if tls is None else tls
            s = await run(mgr.connect, session, host, port or (992 if use_tls else 23), use_tls,
                          True if verify_tls is None else verify_tls, device_name, bool(wide),
                          codepage, 20.0)
        head = ("%s\n" % note) if note else ""
        return head + render(s.snapshot()) + viewer_hint()

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def list_profiles() -> str:
        """List the saved connection profiles you can connect with."""
        if not profiles.profiles:
            return "no saved profiles (the server was started without --profiles)"
        return "\n".join(p.summary() for p in profiles.profiles.values()) + (
            "\n(only profiles can be used to connect)" if profiles_only else "")

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                          idempotent_hint=True))
    async def disconnect(session: Session_ = "main") -> str:
        """Close a session (does not sign off; use the host's sign-off first if you
        want a clean end, e.g. SIGNOFF on a command line)."""
        await run(mgr.disconnect, session)
        return "disconnected %s" % session

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def list_sessions() -> str:
        """List open sessions and their state."""
        if not mgr.sessions:
            return "no sessions" + viewer_hint()
        lines = []
        for name, s in mgr.sessions.items():
            snap = s.snapshot()
            lines.append("%s: %s, keyboard %s, screen_id %s, row 1: %r" % (
                name, s.host, snap["keyboard"], snap["screen_id"], snap["text"][0].strip()))
        return "\n".join(lines) + viewer_hint()

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def get_screen(session: Session_ = "main",
                         fields: Annotated[bool, Field(
                             description="include the input field table")] = True) -> str:
        """Read the current screen: text with row/column rulers, cursor, keyboard
        state, screen_id and the input fields."""
        s = await run(mgr.get, session)
        return render(s.snapshot(), show_fields=fields)

    async def fill(
        values: Annotated[dict[str, str], Field(
            description='field -> value, e.g. {"5,25": "QSECOFR", "Option": "2"}; '
                        'an empty string clears the field')],
        expect: Annotated[str | None, Field(
            description='text that must be on screen, or "row:text"')] = None,
        screen_id: Annotated[str | None, Field(
            description="screen_id from your last read; refused if the screen changed")] = None,
        session: Session_ = "main",
    ) -> str:
        """Type values into input fields without sending anything to the host."""
        s = await run(mgr.get, session)
        await run(mgr.fill, s, values, expect, screen_id)
        return render(s.snapshot())

    async def press(
        key: Annotated[str, Field(description="Enter, F1-F24, PageUp, PageDown, Help, "
                                              "Clear, Print, SysReq, Attn, Reset")],
        values: Annotated[dict[str, str] | None, Field(
            description="fields to fill first (same as fill)")] = None,
        cursor: Annotated[str | None, Field(
            description='put the cursor here first: "row,col", "#index" or a label '
                        '(for F4 prompts, cursor-sensitive options, help)')] = None,
        expect: Annotated[str | None, Field(
            description='text that must be on screen, or "row:text"')] = None,
        screen_id: Annotated[str | None, Field(
            description="screen_id from your last read; refused if the screen changed")] = None,
        wait_for: Annotated[str | None, Field(
            description="keep waiting until this text appears")] = None,
        timeout: float = 30.0,
        session: Session_ = "main",
    ) -> str:
        """Send a key to the host (after optional fills) and return the screen the
        host answers with. Reset only clears an operator error locally."""
        s = await run(mgr.get, session)
        ok = await run(mgr.press, s, key, values, cursor, expect, screen_id, True, wait_for,
                       timeout)
        head = "" if ok else "timed out after %.0fs waiting for the host%s\n" % (
            timeout, " / %r" % wait_for if wait_for else "")
        return head + render(s.snapshot())

    if not (flows_only or read_only):
        mcp.add_tool(fill, annotations=ToolAnnotations(read_only_hint=False,
                                                       destructive_hint=False))
        mcp.add_tool(press, annotations=ToolAnnotations(read_only_hint=False,
                                                        destructive_hint=True,
                                                        open_world_hint=True))

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def wait(
        text: Annotated[str | None, Field(description="wait until this text is on screen")] = None,
        timeout: float = 30.0,
        session: Session_ = "main",
    ) -> str:
        """Wait until the host is waiting for input (and optionally for some text),
        e.g. after a long-running command or while the user signs on."""
        s = await run(mgr.get, session)
        ok = await run(s.wait, timeout, text)
        return ("" if ok else "timed out after %.0fs\n" % timeout) + render(s.snapshot())

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def viewer() -> str:
        """Link to the live web viewer, where the human can watch, sign on or take
        over. Give it to the user when a password is needed."""
        return mgr.viewer_url or "the viewer is disabled (--viewer-port 0)"

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
    async def record_start(session: Session_ = "main") -> str:
        """Start recording a screen flow on this session: every key pressed from
        now on (by you or by the user in the viewer) becomes a step. Passwords
        are never recorded. Finish with record_stop."""
        s = await run(mgr.get, session)
        await run(mgr.record_start, s)
        return "recording %s; press keys as usual, then call record_stop" % session

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
    async def record_stop(
        name: Annotated[str | None, Field(
            description="flow name (lower_snake_case); leave out to discard the recording")] = None,
        description: Annotated[str, Field(
            description="what the flow does; the agent reads it when choosing tools")] = "",
        save: Annotated[bool, Field(
            description="write <name>.yaml to the server's record folder")] = True,
        params: Annotated[str, Field(
            description="auto: typed values become parameters (recorded value = default); "
                        "none: keep them literal")] = "auto",
        session: Session_ = "main",
        ctx: Context | None = None,
    ) -> str:
        """Stop recording and return the flow as YAML (saved when the server has a
        record folder). The flow is available as a tool right away. Review it
        before relying on it."""
        s = await run(mgr.get, session)
        if name and (name in BUILTIN_TOOLS or name in flows.flows):
            raise ToolError("there is already a tool or flow called %r; pick another name"
                            % name)
        text, path = await run(mgr.record_stop, s, name, description, save,
                               "none" if params == "none" else "auto")
        if text is None:
            return "recording discarded"
        where = ("saved to %s" % path) if path else (
            "not saved (start the server with --record-dir to save recordings)")
        added = _register_recorded(mcp, flows, runner, run, text, toolsets, flows_only,
                                   read_only)
        if added and ctx is not None:
            for notify in (lambda: ctx.notify_tools_changed(),
                           lambda: ctx.request_context.session.send_tool_list_changed()):
                try:
                    await notify()
                except Exception:  # noqa: BLE001 - clients without list_changed support
                    pass
        return "%s; %s\n\n%s" % (where, "available now as the tool %s" % name if added
                                  else "not offered as a tool on this server", text)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def keys() -> str:
        """List the key names press accepts."""
        return ", ".join(ALL_KEYS)

    for f in flows.select(toolsets, read_only, allow_secrets=bool(mgr.viewer_url)):
        _add_flow_tool(mcp, f, flows, runner, run)
    # recordings saved from the viewer become tools too (clients see them on their
    # next tool listing)
    mgr.on_recorded = lambda text: _register_recorded(mcp, flows, runner, run, text, toolsets,
                                                      flows_only, read_only)
    return mcp


def _register_recorded(mcp, flows: FlowSet, runner: FlowRunner, run, text: str,
                       toolsets, flows_only: bool, read_only: bool) -> bool:
    """Add a just-recorded flow to the running server."""
    import yaml
    doc = yaml.safe_load(text)
    flows.add_doc(doc, "recording")
    flows.validate()
    added = False
    for name in doc["flows"]:
        f = flows.flows[name]
        if f in flows.select(toolsets, read_only, allow_secrets=bool(runner.mgr.viewer_url)):
            _add_flow_tool(mcp, f, flows, runner, run)
            added = True
    return added


def _add_flow_tool(mcp: MCPServer, f: Flow, flows: FlowSet, runner: FlowRunner,
                   run) -> None:
    if f.name in BUILTIN_TOOLS:
        raise FlowConfigError("%s: flow %s has the same name as a built-in tool"
                              % (f.source, f.name))

    async def tool(**kw):
        session = kw.pop("session", "main")
        s = await run(runner.mgr.get, session)
        res = await run(runner.run, s, f.name, kw)
        return str(res)

    params = [inspect.Parameter(p.name, inspect.Parameter.KEYWORD_ONLY,
                                annotation=_flow_annotation(p),
                                default=inspect.Parameter.empty if p.required else p.default)
              for p in f.params.values() if p.type != "secret"]
    params.append(inspect.Parameter("session", inspect.Parameter.KEYWORD_ONLY,
                                    annotation=Session_, default="main"))
    tool.__signature__ = inspect.Signature(params, return_annotation=str)
    tool.__name__ = f.name
    mcp.add_tool(tool, name=f.name, description=_flow_description(f, flows),
                 annotations=ToolAnnotations(read_only_hint=f.read_only,
                                             destructive_hint=not f.read_only,
                                             open_world_hint=True))


def _env_list(name: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="tn5250-mcp", description="5250 MCP server")
    p.add_argument("--transport", choices=["stdio", "http"],
                   default=os.environ.get("T5250_TRANSPORT", "stdio"))
    p.add_argument("--host", default="127.0.0.1", help="HTTP transport bind address")
    p.add_argument("--port", type=int, default=int(os.environ.get("T5250_PORT", "8250")),
                   help="HTTP transport port (MCP endpoint at /mcp)")
    p.add_argument("--viewer-port", type=int,
                   default=int(os.environ.get("T5250_VIEWER_PORT", "8251")),
                   help="live viewer port on 127.0.0.1; 0 disables it")
    p.add_argument("--allowed-hosts", default=os.environ.get("T5250_ALLOWED_HOSTS", ""),
                   help="comma-separated host patterns connect may use (default: any)")
    p.add_argument("--block", action="append", default=_env_list("T5250_BLOCK"),
                   help="extra regex of commands to refuse (repeatable)")
    p.add_argument("--no-default-blocks", action="store_true",
                   default=os.environ.get("T5250_NO_DEFAULT_BLOCKS") == "1")
    p.add_argument("--no-require-check", action="store_true",
                   default=os.environ.get("T5250_REQUIRE_CHECK") == "0",
                   help="don't require expect/screen_id on fill and press")
    p.add_argument("--log-dir", default=os.environ.get("T5250_LOG_DIR"),
                   help="write a transcript of every session here")
    p.add_argument("--codepage", default=os.environ.get("T5250_CODEPAGE", "cp037"))
    p.add_argument("--reconnect-attempts", type=int,
                   default=int(os.environ.get("T5250_RECONNECT_ATTEMPTS", "10")),
                   help="reconnect this many times when the host drops a session (0 = off)")
    p.add_argument("--reconnect-interval", type=float,
                   default=float(os.environ.get("T5250_RECONNECT_INTERVAL", "5")),
                   help="seconds between reconnect attempts")
    p.add_argument("--record-dir", default=os.environ.get("T5250_RECORD_DIR"),
                   help="folder where record_stop saves recorded flows")
    p.add_argument("--profiles", action="append",
                   default=[x for x in os.environ.get("T5250_PROFILES", "").split(os.pathsep)
                            if x],
                   help="YAML file or folder of saved connection profiles (repeatable)")
    p.add_argument("--profiles-only", action="store_true",
                   default=os.environ.get("T5250_PROFILES_ONLY") == "1",
                   help="connect only through saved profiles (no free host names)")
    p.add_argument("--flows", action="append",
                   default=[x for x in os.environ.get("T5250_FLOWS", "").split(os.pathsep) if x],
                   help="YAML file or folder of screen flows to offer as tools (repeatable)")
    p.add_argument("--toolsets", default=os.environ.get("T5250_TOOLSETS", ""),
                   help="comma-separated toolsets: only offer flows from these")
    p.add_argument("--flows-only", action="store_true",
                   default=os.environ.get("T5250_FLOWS_ONLY") == "1",
                   help="no fill/press tools: the agent can only read screens and run flows")
    p.add_argument("--pace", type=float, default=float(os.environ.get("T5250_PACE", "0")),
                   help="seconds to wait after each flow step, so a person can follow")
    p.add_argument("--read-only", action="store_true",
                   default=os.environ.get("T5250_READ_ONLY") == "1",
                   help="no fill/press, and only flows marked read_only")
    a = p.parse_args(argv)
    if a.record_dir and os.path.isdir(a.record_dir) and any(
            n.endswith((".yaml", ".yml")) for n in os.listdir(a.record_dir)) and \
            os.path.abspath(a.record_dir) not in {os.path.abspath(x) for x in a.flows}:
        a.flows.append(a.record_dir)          # recorded flows come back after a restart
    try:
        flowset = FlowSet.load(a.flows) if a.flows else None
    except FlowConfigError as e:
        p.exit(2, "tn5250-mcp: %s\n" % e)

    blocked = ([] if a.no_default_blocks else list(DEFAULT_BLOCKED)) + list(a.block)
    guard = Guard(require_check=not a.no_require_check,
                  allowed_hosts=[h.strip() for h in a.allowed_hosts.split(",") if h.strip()],
                  blocked=blocked)
    mgr = Manager(guard, log_dir=a.log_dir, default_codepage=a.codepage,
                  reconnect_attempts=a.reconnect_attempts,
                  reconnect_interval=a.reconnect_interval, record_dir=a.record_dir)

    if a.viewer_port:
        from .viewer import start_viewer
        try:
            mgr.viewer_url = start_viewer(mgr, a.viewer_port)
            print("tn5250-mcp viewer: %s" % mgr.viewer_url, file=sys.stderr)
        except OSError as e:
            print("tn5250-mcp: viewer not started (%s)" % e, file=sys.stderr)

    toolsets = [t.strip() for t in a.toolsets.split(",") if t.strip()] or None
    try:
        profileset = ProfileSet.load(a.profiles) if a.profiles else None
        server = build_server(mgr, flowset, toolsets, a.flows_only, a.read_only, a.pace,
                              profileset, a.profiles_only)
        if profileset:
            profileset.validate_flows(flowset, mgr.flow_runner)
            print("tn5250-mcp: profiles: %s" % ", ".join(profileset.profiles), file=sys.stderr)
    except (FlowConfigError, ProfileError) as e:
        p.exit(2, "tn5250-mcp: %s\n" % e)
    if flowset:
        offered = [f.name for f in flowset.select(toolsets, a.read_only,
                                                   allow_secrets=bool(mgr.viewer_url))]
        print("tn5250-mcp: flow tools: %s" % (", ".join(offered) or "none"), file=sys.stderr)
    if a.transport == "stdio":
        server.run("stdio")
    else:
        print("tn5250-mcp: MCP endpoint http://%s:%d/mcp" % (a.host, a.port), file=sys.stderr)
        server.run("streamable-http", host=a.host, port=a.port)


if __name__ == "__main__":
    main()
