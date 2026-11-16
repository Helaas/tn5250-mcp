"""MCP server exposing 5250 sessions as tools."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Annotated

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from .guard import DEFAULT_BLOCKED, Guard
from .manager import GuardError, Manager, render
from .session import ALL_KEYS

INSTRUCTIONS = """\
Drive IBM i (AS/400) 5250 green-screen sessions.

Workflow: connect -> get_screen -> fill / press -> read the screen that press returns.
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
"""

Session_ = Annotated[str, Field(description="session name; lets you run several side by side")]


def build_server(mgr: Manager) -> MCPServer:
    mcp = MCPServer("tn5250", instructions=INSTRUCTIONS, version="0.1.0")

    async def run(fn, *a, **k):
        try:
            return await anyio.to_thread.run_sync(lambda: fn(*a, **k))
        except GuardError as e:
            raise ToolError(str(e)) from None
        except (OSError, ConnectionError) as e:
            raise ToolError("connection problem: %s" % e) from None

    def viewer_hint() -> str:
        return ("\nviewer for the human (watch / sign on): %s" % mgr.viewer_url
                if mgr.viewer_url else "")

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                          open_world_hint=True))
    async def connect(
        host: Annotated[str, Field(description="IBM i host name, e.g. pub400.com")],
        port: Annotated[int, Field(description="992 for TLS, 23 for plain telnet")] = 992,
        tls: bool = True,
        session: Session_ = "main",
        verify_tls: bool = True,
        device_name: Annotated[str | None, Field(
            description="ask for a specific display device name")] = None,
        wide: Annotated[bool, Field(description="27x132 display (3477) instead of 24x80")] = False,
        codepage: Annotated[str | None, Field(
            description="EBCDIC code page, e.g. cp037 (US), cp273 (DE), cp500 (intl)")] = None,
    ) -> str:
        """Open a 5250 session and return the first screen (usually Sign On)."""
        s = await run(mgr.connect, session, host, port, tls, verify_tls, device_name, wide,
                      codepage, 20.0)
        return render(s.snapshot()) + viewer_hint()

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

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
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

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                          open_world_hint=True))
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

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def keys() -> str:
        """List the key names press accepts."""
        return ", ".join(ALL_KEYS)

    return mcp


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
    a = p.parse_args(argv)

    blocked = ([] if a.no_default_blocks else list(DEFAULT_BLOCKED)) + list(a.block)
    guard = Guard(require_check=not a.no_require_check,
                  allowed_hosts=[h.strip() for h in a.allowed_hosts.split(",") if h.strip()],
                  blocked=blocked)
    mgr = Manager(guard, log_dir=a.log_dir, default_codepage=a.codepage)

    if a.viewer_port:
        from .viewer import start_viewer
        try:
            mgr.viewer_url = start_viewer(mgr, a.viewer_port)
            print("tn5250-mcp viewer: %s" % mgr.viewer_url, file=sys.stderr)
        except OSError as e:
            print("tn5250-mcp: viewer not started (%s)" % e, file=sys.stderr)

    server = build_server(mgr)
    if a.transport == "stdio":
        server.run("stdio")
    else:
        print("tn5250-mcp: MCP endpoint http://%s:%d/mcp" % (a.host, a.port), file=sys.stderr)
        server.run("streamable-http", host=a.host, port=a.port)


if __name__ == "__main__":
    main()
