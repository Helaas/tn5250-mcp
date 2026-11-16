"""MCP server exposing 5250 sessions as tools."""

from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
from typing import Annotated, Any

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from .flows import Flow, FlowConfigError, FlowError, FlowRunner, FlowSet
from .credentials import CredentialStore
from .guard import DEFAULT_BLOCKED, Guard
from .hostservers import HostServerError, HostServers, format_table, host_login
from .manager import GuardError, Manager, render
from .profiles import ProfileError, ProfileSet
from .session import ALL_KEYS

INSTRUCTIONS = """\
Drive IBM i (AS/400) 5250 green-screen sessions.

Workflow: connect -> get_screen -> fill / press -> read the screen that press returns.
- SQL and IFS tools (sql_query, ifs_list, ifs_read, ...), when offered, work
  through the IBM i host servers, not the screen: host_connect first. They
  are faster and exact for reading data; use the screen for applications.
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
                 "wait", "viewer", "keys", "record_start", "record_stop", "list_profiles",
                 "get_session_log", "host_connect", "host_disconnect", "sql_query",
                 "sql_execute", "ifs_list", "ifs_read", "ifs_write", "forget_password",
                 "run_command", "compile", "job_log", "spooled_files", "spooled_file",
                 "call_program", "build_project", "pase", "source_members", "member_read",
                 "member_write"}

Session_ = Annotated[str, Field(description="session name; lets you run several side by side")]
Conn_ = Annotated[str, Field(description="name of the host-server connection")]
Params_ = Annotated[list[str | int | float | None] | None, Field(
    description="values for ? parameter markers, in order")]
ProgParams_ = Annotated[list[dict[str, Any]] | None, Field(
    description="the program's parameters in order, each {type, value, out}: type as in "
                "RPG - char(n), varchar(n), packed(d,p), zoned(d,p), int(5|10|20), "
                "uns(5|10), float(4|8), ind; out: true to get its value back after the "
                "call (value is then the input, or left out)")]


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
                 profiles: ProfileSet | None = None, profiles_only: bool = False,
                 hostservers: HostServers | None = None) -> MCPServer:
    mcp = MCPServer("tn5250", instructions=INSTRUCTIONS, version="0.4.0")
    flows = flows if flows is not None else FlowSet()
    runner = FlowRunner(mgr, flows, pace=pace)
    mgr.flow_runner = runner
    profiles = profiles if profiles is not None else ProfileSet()
    mgr.name_taken = lambda n: n in BUILTIN_TOOLS or n in flows.flows

    async def run(fn, *a, **k):
        try:
            return await anyio.to_thread.run_sync(lambda: fn(*a, **k))
        except (GuardError, FlowError, ProfileError, HostServerError) as e:
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
            description="ask for a display device name; * becomes A, B, C... per connection "
                        "(e.g. AGENT*), = a random character")] = None,
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
    async def get_session_log(
        session: Session_ = "main",
        last: Annotated[int, Field(description="how many entries", ge=1, le=400)] = 20,
        screens: Annotated[bool, Field(
            description="also include the screens the host sent (long)")] = False,
    ) -> str:
        """The session's transcript: connects, fills (passwords masked), keys,
        flows, reconnects and recordings, newest last. Useful to retrace what
        happened, also after a disconnect."""
        return await run(mgr.session_log, session, last, screens)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                          idempotent_hint=True))
    async def forget_password(
        host: Annotated[str | None, Field(description="only for this host")] = None,
        user: Annotated[str | None, Field(description="only for this user profile")] = None,
    ) -> str:
        """Forget saved passwords (kept after sign-ons that worked, in memory and,
        if the user chose so, in the system keychain). The next sign-on asks again."""
        n = await run(mgr.creds.forget, host, user)
        return "forgot %d saved password%s" % (n, "" if n == 1 else "s")

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def list_profiles() -> str:
        """List the saved connection profiles you can connect with."""
        if not profiles.profiles:
            return "no saved profiles (the server was started without --profiles)"
        return "\n".join(p.summary() for p in profiles.profiles.values()) + (
            "\n(only profiles can be used to connect)" if profiles_only else "")

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                          idempotent_hint=True))
    async def disconnect(
        session: Session_ = "main",
        signoff: Annotated[bool, Field(
            description="sign off first from the screen's command line, so the job "
                        "doesn't stay active on the host")] = True,
    ) -> str:
        """Sign off (from the command line of the current screen) and close the
        session. A screen without a command line can't be signed off from; the
        result says so."""
        note = await run(mgr.disconnect, session, signoff)
        return "disconnected %s%s" % (session, "; " + note if note else "")

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def list_sessions() -> str:
        """List open sessions and their state."""
        saved = mgr.creds.saved()
        saved_line = ("\nsaved passwords (not shown): %s" % ", ".join(saved)) if saved else ""
        if not mgr.sessions:
            return "no sessions" + saved_line + viewer_hint()
        lines = []
        for name, s in mgr.sessions.items():
            snap = s.snapshot()
            lines.append("%s: %s, keyboard %s, screen_id %s, row 1: %r" % (
                name, s.host, snap["keyboard"], snap["screen_id"], snap["text"][0].strip()))
        return "\n".join(lines) + saved_line + viewer_hint()

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
    if hostservers is not None:
        _add_host_tools(mcp, mgr, hostservers, profiles, profiles_only, read_only, run)
    # recordings saved from the viewer become tools too (clients see them on their
    # next tool listing)
    mgr.on_recorded = lambda text: _register_recorded(mcp, flows, runner, run, text, toolsets,
                                                      flows_only, read_only)
    return mcp


def _add_host_tools(mcp: MCPServer, mgr: Manager, hs: HostServers, profiles: ProfileSet,
                    profiles_only: bool, read_only: bool, run) -> None:
    """SQL and IFS through the IBM i host servers (jt400)."""
    loggers: dict = {}
    mgr.on_password_changed = lambda host, user: hs.close_user(host, user)
    mgr.hostservers = hs                     # flows' query steps use it too

    def log(name, kind, text):
        if name not in loggers:
            loggers[name] = mgr._logger("host-" + name)
        loggers[name](kind, text)
    hs.log = log

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                          open_world_hint=True))
    async def host_connect(
        profile: Annotated[str | None, Field(
            description="saved connection profile (uses its host, TLS and user)")] = None,
        host: Annotated[str | None, Field(description="IBM i host, when not using a profile")]
        = None,
        user: Annotated[str | None, Field(description="user profile")] = None,
        tls: Annotated[bool | None, Field(description="TLS (default true)")] = None,
        libl: Annotated[list[str] | None, Field(
            description="library list for SQL, commands and compiles (default: the "
                        "profile's, else the user's own)")] = None,
        name: Conn_ = "main",
    ) -> str:
        """Open a connection to the IBM i database and file servers for the SQL
        and IFS tools. The user types the password in the viewer (give them
        the viewer link first); you never see it."""
        if profile:
            p = await run(profiles.get, profile)
            if host:
                raise ToolError("connect with either a profile or a host, not both")
            host, use_tls = p.host, p.tls
            user = user or p.user
            libl = libl or p.libl
        else:
            if profiles_only:
                raise ToolError("this server only connects through saved profiles: %s"
                                % (", ".join(profiles.profiles) or "none configured"))
            use_tls = True if tls is None else tls
        if not host or not user:
            raise ToolError("host_connect needs a host and a user (or a profile with a user)")
        info, notes = await run(host_login, mgr, hs, name, host, user, use_tls, libl)
        return "connected %s: %s on %s (%s, %s); jt400 %s%s" % (
            name, info["user"], info["host"], info["release"], "TLS" if use_tls else "plain",
            hs.helper.version, "".join("\nnote: " + n for n in notes))

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                          idempotent_hint=True))
    async def host_disconnect(name: Conn_ = "main") -> str:
        """Close a host-server connection."""
        await run(hs.disconnect, name)
        return "closed %s" % name

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def sql_query(
        sql: Annotated[str, Field(description="SELECT / WITH / VALUES; system naming "
                                              "(MYLIB/TABLE or MYLIB.TABLE), ? markers")],
        params: Params_ = None,
        max_rows: Annotated[int, Field(ge=1, le=1000)] = 100,
        format: Annotated[str, Field(description="table or json")] = "table",
        name: Conn_ = "main",
    ) -> str:
        """Run a query on the IBM i database (Db2 for i). The connection is
        read-only on the server side, so statements that change data fail.
        QSYS2 services are available too (e.g. QSYS2.ACTIVE_JOB_INFO,
        QSYS2.IFS_OBJECT_STATISTICS)."""
        res = await run(hs.query, name, sql, params, max_rows)
        if format == "json":
            return json.dumps(res, ensure_ascii=False)
        return format_table(res)

    if hs.allow_sql_write and not read_only:
        @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                              open_world_hint=True))
        async def sql_execute(
            sql: Annotated[str, Field(description="INSERT, UPDATE, DELETE, CREATE, CALL ...")],
            params: Params_ = None,
            name: Conn_ = "main",
        ) -> str:
            """Run a statement that changes data (autocommit). Checked against the
            block list."""
            res = await run(hs.execute, name, sql, params)
            return format_table(res)

    def messages_text(msgs: list[dict]) -> str:
        return "".join("\n  %s (severity %s) %s" % (m.get("id") or "-", m.get("severity"),
                                                      m.get("text")) for m in msgs)

    if hs.allow_commands and not read_only:
        @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                              open_world_hint=True))
        async def run_command(
            command: Annotated[str, Field(description="a CL command, e.g. DSPOBJD OBJ(MYLIB/"
                                                      "HELLO) OBJTYPE(*PGM)")],
            libl: Annotated[list[str] | None, Field(
                description="library list to set first (kept for later commands)")] = None,
            name: Conn_ = "main",
        ) -> str:
            """Run a CL command through the host servers (not the screen) and return
            IBM i's messages. Interactive commands that show screens don't work
            here; use the 5250 session for those. job_log shows more detail."""
            res = await run(hs.command, name, command, libl)
            return "%s: %s%s%s\njob %s" % (
                "ok" if res["ok"] else "FAILED", command,
                " [libl %s]" % " ".join(res["libl"]) if res.get("libl") else "",
                messages_text(res.get("messages", [])) or "\n  (no messages)", res.get("job"))

    if hs.allow_commands and not read_only:
        @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                              open_world_hint=True))
        async def call_program(
            program: Annotated[str, Field(description="NAME (found through the library "
                                                      "list) or LIB/NAME")],
            params: ProgParams_ = None,
            libl: Annotated[list[str] | None, Field(
                description="library list to set first (kept for later calls)")] = None,
            name: Conn_ = "main",
        ) -> str:
            """Call a program with typed parameters through the host servers (not the
            screen) and return its output parameters and messages. For unit-testing
            programs and service-program wrappers: no screen, no CL string building.
            The program must not show screens."""
            res = await run(hs.call_program, name, program, params, libl)
            lines = ["%s: CALL %s" % ("ok" if res["ok"] else "FAILED", program.upper())]
            for i, (p, v) in enumerate(zip(params or [], res.get("outputs") or []), 1):
                if p.get("out"):
                    lines.append("  parameter %d %s = %s" % (i, p.get("type"), json.dumps(v)))
            msgs = res.get("messages", [])
            if msgs:
                lines.append("messages:" + messages_text(msgs))
            lines.append("job %s" % res.get("job"))
            return "\n".join(lines)

    if hs.compile_libs and hs.ifs_write_roots and not read_only:
        @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                              open_world_hint=True))
        async def build_project(
            folder: Annotated[str, Field(description="local project folder, with "
                                                     "tn5250-project.yaml")],
            rebuild_all: Annotated[bool, Field(description="upload and build everything, "
                                                           "not just what changed")] = False,
            only: Annotated[list[str] | None, Field(
                description="build just these objects (and nothing that depends on them)")]
            = None,
            format: Annotated[str, Field(description="text or json")] = "text",
            name: Conn_ = "main",
        ) -> str:
            """Build an IBM i project from a local folder: upload the sources that
            changed to the project's IFS folder, then create the objects that need it
            (SQL, files, modules, service programs, programs, in that order). Errors
            come back as local file:line:column, so you can fix the file and build
            again. The folder's tn5250-project.yaml says the library and IFS folder;
            see the README for the file names (hello.pgm.rpgle, utils.rpgle, ...)."""
            from .build import BuildError, build, load_project, report_text

            def go():
                try:
                    return build(hs, name, load_project(folder), rebuild_all, only)
                except BuildError as e:
                    raise HostServerError(str(e)) from None
            res = await run(go)
            return (json.dumps(res, ensure_ascii=False) if format == "json"
                    else report_text(res))

    if hs.compile_libs and not read_only:
        async def compile(
            source: Annotated[str, Field(description="IFS stream file (/home/ME/src/hello."
                                                     "rpgle) or member LIB/FILE(MEMBER)")],
            object: Annotated[str, Field(description="the object to create: LIB/NAME")],
            type: Annotated[str | None, Field(
                description="rpg, sqlrpg, rpgmod, sqlrpgmod, cl, clmod, dspf, prtf, pf, lf; "
                            "default from the file extension")] = None,
            options: Annotated[str, Field(
                description="extra compile parameters, e.g. TGTRLS(*CURRENT)")] = "",
            libl: Annotated[list[str] | None, Field(description="library list to compile "
                                                               "with")] = None,
            format: Annotated[str, Field(description="text or json")] = "text",
            name: Conn_ = "main",
        ) -> str:
            res = await run(hs.compile, name, source, object, type, options, libl)
            if format == "json":
                return json.dumps(res, ensure_ascii=False)
            diags = res["diagnostics"]
            errors = [d for d in diags if d["severity"] >= 20 and d["line"] > 0]
            head = "%s %s: %s" % ("compiled" if res["ok"] else "compile FAILED for",
                                  res["object"], res["command"])
            lines = [head]
            if diags:
                lines.append("%d diagnostic%s (%d errors), most severe first:" % (
                    len(diags), "" if len(diags) == 1 else "s", len(errors)))
                lines += ["  %s:%d:%d %s %d %s %s" % (
                    d["file"], d["line"], d["column"], d["severity_name"], d["severity"],
                    d["id"], d["text"]) for d in diags[:100]]
            if res.get("note"):
                lines.append("note: " + res["note"])
            msgs = [m for m in res["messages"] if (m.get("severity") or 0) >= 10]
            if msgs:
                lines.append("messages:" + messages_text(msgs))
            lines.append("job %s (job_log for the full log)" % res.get("job"))
            return "\n".join(lines)

        mcp.add_tool(compile, description=(
            "Compile a source into an object, only into these libraries: %s. Uses "
            "OPTION(*EVENTF) and returns the compiler's errors as file:line:column, "
            "severity, message ID and text, most severe first, so you can fix the source "
            "(e.g. with ifs_write) and compile again." % ", ".join(hs.compile_libs)),
            annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                        open_world_hint=True))

    if hs.allow_shell and not read_only:
        @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                              open_world_hint=True))
        async def pase(
            command: Annotated[str, Field(description="a shell command or script, e.g. "
                                                      "\"cd src && git status\"")],
            cwd: Annotated[str | None, Field(description="IFS folder to run in (default: "
                                                         "your home folder)")] = None,
            timeout: Annotated[int, Field(ge=1, le=3600, description="seconds; then the "
                                                                    "command is killed")] = 60,
            libl: Annotated[list[str] | None, Field(
                description="library list to set first (for `system` and DB2 calls)")] = None,
            name: Conn_ = "main",
        ) -> str:
            """Run a command in the IBM i PASE shell (/QOpenSys/usr/bin/sh) and return
            its exit code, stdout and stderr. /QOpenSys/pkgs/bin comes first on the
            PATH, so open-source tools (git, make, python3, node, ...) work when
            installed. Runs in the command job: `system "CL command"` sees the
            library list. Nothing interactive: stdin is empty."""
            res = await run(hs.pase, name, command, cwd, timeout, libl)
            parts = ["exit %d%s%s" % (res["exit_code"], " (timed out after %ds)" % timeout
                                      if res["timed_out"] else "",
                                      ", output truncated" if res["truncated"] else "")]
            if res["stdout"]:
                parts.append("--- stdout\n" + res["stdout"].rstrip("\n"))
            if res["stderr"]:
                parts.append("--- stderr\n" + res["stderr"].rstrip("\n"))
            return "\n".join(parts)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def source_members(
        lib: Annotated[str, Field(description="library")],
        file: Annotated[str | None, Field(description="source file, e.g. QRPGLESRC; leave "
                                                      "out to list the library's source "
                                                      "files")] = None,
        pattern: Annotated[str | None, Field(description="member names, e.g. ORD*")] = None,
        name: Conn_ = "main",
    ) -> str:
        """List a library's source files, or the members of one source file with
        their type, text, size and last change."""
        if file:
            res = await run(hs.members, name, lib, file, pattern)
        else:
            res = await run(hs.source_files, name, lib)
        return format_table(res) if res["rows"] else "(none)"

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def member_read(
        member: Annotated[str, Field(description="LIB/FILE(MEMBER), e.g. "
                                                 "MYLIB/QRPGLESRC(HELLO)")],
        numbers: Annotated[bool, Field(description="show each line's sequence number and "
                                                   "date (SEU style)")] = False,
        name: Conn_ = "main",
    ) -> str:
        """Read a source member as text. Compile it with compile (source
        LIB/FILE(MEMBER)); change it with member_write."""
        res = await run(hs.member_read, name, member)
        head = "%s: %d lines, %d wide%s" % (res["member"], len(res["lines"]), res["width"],
                                           ", truncated" if res.get("truncated") else "")
        if numbers:
            body = ["%7s %06d %s" % (q, d, t) for q, d, t in res["lines"]]
        else:
            body = [t for _, _, t in res["lines"]]
        return "\n".join([head] + body)

    if hs.member_write_libs and not read_only:
        @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                              open_world_hint=True))
        async def member_write(
            member: Annotated[str, Field(description="LIB/FILE(MEMBER)")],
            text: Annotated[str, Field(description="the whole new source")],
            type: Annotated[str | None, Field(description="source type for a new member "
                                                          "(RPGLE, SQLRPGLE, CLLE, DSPF, "
                                                          "PF, ...) or to change it")] = None,
            description: Annotated[str | None, Field(description="member text (50 "
                                                                 "characters)")] = None,
            name: Conn_ = "main",
        ) -> str:
            """Replace a source member's text, creating the member if it doesn't exist.
            Unchanged lines keep their date, changed lines get today's (as SEU
            does). Lines may not be wider than the source file."""
            res = await run(hs.member_write, name, member, text, type, description)
            return "%s %s: %d lines (%d new or changed), %d wide" % (
                "created" if res["created"] else "wrote", res["member"], res["lines"],
                res["changed_lines"], res["width"])

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def job_log(
        job: Annotated[str | None, Field(
            description="number/user/name; default: the job that ran this connection's "
                        "commands and compiles")] = None,
        min_severity: Annotated[int, Field(ge=0, le=99)] = 0,
        last: Annotated[int, Field(ge=1, le=1000)] = 50,
        name: Conn_ = "main",
    ) -> str:
        """A job's log (its messages), newest last: why a command, compile or
        batch job failed."""
        res = await run(hs.job_log, name, job, min_severity, last)
        return "job log of %s:\n%s" % (res["job"], format_table(res, max_width=120))

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def spooled_files(
        job: Annotated[str | None, Field(
            description="number/user/name; default: your newest spooled files")] = None,
        last: Annotated[int, Field(ge=1, le=500)] = 20,
        name: Conn_ = "main",
    ) -> str:
        """List spooled files (printed output: reports, compile listings, job logs)."""
        res = await run(hs.spooled_files, name, job, last)
        return format_table(res)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def spooled_file(
        job: Annotated[str, Field(description="number/user/name of the job that made it")],
        file: Annotated[str, Field(description="spooled file name, e.g. QSYSPRT")],
        number: Annotated[str, Field(description="spooled file number, or *LAST")] = "*LAST",
        max_lines: Annotated[int, Field(ge=1, le=20000)] = 2000,
        name: Conn_ = "main",
    ) -> str:
        """Read a spooled file as text, e.g. to check a report a program printed."""
        res = await run(hs.spooled_file, name, job, file, number, max_lines)
        return "\n".join(res["lines"]) + ("\n(more lines not shown)" if res["truncated"]
                                            else "")

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def ifs_list(
        path: Annotated[str, Field(description="absolute IFS path, e.g. /home/MYUSER")],
        name: Conn_ = "main",
    ) -> str:
        """List an IFS directory (or describe one file)."""
        res = await run(hs.ifs_list, name, path)
        lines = ["%s%s" % (res["path"], " (directory)" if res["dir"] else "")]
        for e in res["entries"]:
            lines.append("  %-40s %s" % (e["name"] + ("/" if e["dir"] else ""),
                                         "" if e["dir"] else "%d bytes" % e["size"]))
        if res.get("truncated"):
            lines.append("  ... more entries not shown")
        return "\n".join(lines)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def ifs_read(
        path: Annotated[str, Field(description="absolute IFS path of a stream file")],
        max_bytes: Annotated[int, Field(ge=1, le=1_000_000)] = 100_000,
        binary: Annotated[bool, Field(description="return base64 instead of text")] = False,
        name: Conn_ = "main",
    ) -> str:
        """Read a stream file from the IFS. Text is converted from the file's
        CCSID; binary content comes back as base64."""
        res = await run(hs.ifs_read, name, path, max_bytes, binary)
        head = "%s: %d bytes, CCSID %s%s\n" % (path, res["size"], res["ccsid"],
                                               ", truncated" if res["truncated"] else "")
        return head + (res["base64"] if binary else res["text"])

    if hs.ifs_write_roots and not read_only:
        async def ifs_write(
            path: Annotated[str, Field(description="absolute IFS path inside an allowed "
                                                   "folder (see the tool description)")],
            content: Annotated[str, Field(description="text, or base64 when binary")],
            binary: bool = False,
            ccsid: Annotated[int, Field(description="CCSID for a new text file "
                                                    "(1208 = UTF-8, 37 = EBCDIC US)")] = 1208,
            overwrite: bool = False,
            append: bool = False,
            name: Conn_ = "main",
        ) -> str:
            """Write a stream file to the IFS (only inside the folders the server
            allows). Refuses to replace an existing file unless overwrite or
            append is set."""
            res = await run(hs.ifs_write, name, path, content, binary=binary, ccsid=ccsid,
                            overwrite=overwrite, append=append)
            return "wrote %s: %d bytes, CCSID %s" % (res["path"], res["size"], res["ccsid"])

        mcp.add_tool(ifs_write, description=(
            "Write a stream file to the IFS, only inside: %s. Refuses to replace an existing "
            "file unless overwrite or append is set. Text is written in the given CCSID "
            "(1208 = UTF-8)." % ", ".join(hs.ifs_write_roots)),
            annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                        open_world_hint=True))


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
    p.add_argument("--password-cache-hours", type=float,
                   default=float(os.environ.get("T5250_PASSWORD_CACHE_HOURS", "8")),
                   help="keep typed passwords in memory this long after last use (0 = off)")
    p.add_argument("--no-remember", action="store_true",
                   default=os.environ.get("T5250_NO_REMEMBER") == "1",
                   help="never offer to save passwords in the system keychain")
    p.add_argument("--jt400", default=os.environ.get("T5250_JT400"),
                   help="path to jt400.jar (JTOpen) to enable the SQL and IFS tools")
    p.add_argument("--java", default=os.environ.get("T5250_JAVA"),
                   help="Java 11+ runtime for the jt400 helper (default: found automatically)")
    p.add_argument("--allow-sql-write", action="store_true",
                   default=os.environ.get("T5250_ALLOW_SQL_WRITE") == "1",
                   help="offer sql_execute (statements that change data)")
    p.add_argument("--allow-commands", action="store_true",
                   default=os.environ.get("T5250_ALLOW_COMMANDS") == "1",
                   help="offer run_command (CL commands through the host servers)")
    p.add_argument("--command-allow", default=os.environ.get("T5250_COMMAND_ALLOW", ""),
                   help="with --allow-commands: only these commands (e.g. 'DSP*,WRK*,CALL')")
    p.add_argument("--allow-compile", default=os.environ.get("T5250_ALLOW_COMPILE", ""),
                   help="offer compile, into these libraries (comma list, * = any)")
    p.add_argument("--allow-shell", action="store_true",
                   default=os.environ.get("T5250_ALLOW_SHELL") == "1",
                   help="offer pase (shell commands in PASE)")
    p.add_argument("--allow-member-write", default=os.environ.get("T5250_ALLOW_MEMBER_WRITE", ""),
                   help="offer member_write, in these libraries (comma list, * = any)")
    p.add_argument("--ifs-write-root", action="append",
                   default=[x for x in os.environ.get("T5250_IFS_WRITE_ROOT", "").split(
                       os.pathsep) if x],
                   help="offer ifs_write, limited to this IFS folder (repeatable)")
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
                  reconnect_interval=a.reconnect_interval, record_dir=a.record_dir,
                  creds=CredentialStore(a.password_cache_hours, keychain=not a.no_remember))
    if mgr.creds.keychain:
        print("tn5250-mcp: passwords can be remembered in the system keychain", file=sys.stderr)

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
        hostservers = None
        if a.jt400:
            hostservers = HostServers(
                a.jt400, a.java, allow_sql_write=a.allow_sql_write,
                ifs_write_roots=a.ifs_write_root, guard=guard,
                allow_commands=a.allow_commands,
                command_allow=[x for x in a.command_allow.split(",") if x.strip()],
                compile_libs=[x for x in a.allow_compile.split(",") if x.strip()],
                allow_shell=a.allow_shell,
                member_write_libs=[x for x in a.allow_member_write.split(",") if x.strip()])
            print("tn5250-mcp: SQL/IFS tools on (%s)" % a.jt400, file=sys.stderr)
        server = build_server(mgr, flowset, toolsets, a.flows_only, a.read_only, a.pace,
                              profileset, a.profiles_only, hostservers)
        if profileset:
            profileset.validate_flows(flowset, mgr.flow_runner)
            print("tn5250-mcp: profiles: %s" % ", ".join(profileset.profiles), file=sys.stderr)
    except (FlowConfigError, ProfileError, HostServerError) as e:
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
