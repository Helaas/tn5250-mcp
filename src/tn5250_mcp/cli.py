"""tn5250-run: check and run screen flows, run CL commands and compile, from a
shell or CI, no MCP client.

    tn5250-run check flows/
    tn5250-run run --host pub400.com --flows flows/ data_entry_test lib=MYLIB

Sign-on: --signon env uses $T5250_USER / $T5250_PASSWORD (CI secrets),
--signon viewer prints a viewer link and waits for a person, --signon auto
(default) picks env when $T5250_PASSWORD is set and the viewer otherwise.
Exit codes: 0 passed, 1 flow failed, 2 bad flows or arguments, 3 connection
or sign-on problem.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .flows import FlowConfigError, FlowError, FlowSet
from .guard import DEFAULT_BLOCKED, Guard
from .profiles import ProfileError
from .terminal import Terminal, TerminalError


def _env_flows() -> list[str]:
    return [x for x in os.environ.get("T5250_FLOWS", "").split(os.pathsep) if x]


def _parse_params(items: list[str]) -> dict[str, str]:
    out = {}
    for it in items:
        if "=" not in it:
            raise SystemExit("tn5250-run: parameters are name=value, got %r" % it)
        k, v = it.split("=", 1)
        out[k.strip()] = v
    return out


def cmd_check(a) -> int:
    try:
        fs = FlowSet.load(a.paths or _env_flows())
    except FlowConfigError as e:
        print("invalid: %s" % e, file=sys.stderr)
        return 2
    for f in fs.flows.values():
        print(f.summary() + ("" if f.expose else " (helper, not a tool)"))
    print("%d flows OK" % len(fs.flows), file=sys.stderr)
    return 0


def cmd_run(a) -> int:
    try:
        fs = FlowSet.load(a.flows or _env_flows())
    except FlowConfigError as e:
        print("invalid flows: %s" % e, file=sys.stderr)
        return 2
    if a.flow not in fs.flows:
        print("no flow %r; flows: %s" % (a.flow, ", ".join(fs.flows)), file=sys.stderr)
        return 2
    params = _parse_params(a.params)
    guard = Guard(require_check=False,
                  blocked=([] if a.no_default_blocks else list(DEFAULT_BLOCKED)) + a.block)
    common = dict(flows=fs, log_dir=a.log_dir, guard=guard, viewer_port=a.viewer_port,
                  pace=a.pace, jt400=a.jt400, java=a.java)
    if a.profile:
        try:
            t = Terminal.from_profile(a.profile, a.profiles or ["profiles.yaml"], **common)
        except ProfileError as e:
            print("profile: %s" % e, file=sys.stderr)
            return 2
    elif a.host:
        t = Terminal(a.host, a.port, tls=not a.no_tls, verify_tls=not a.insecure,
                     device_name=a.device_name, codepage=a.codepage, **common)
    else:
        print("give --host or --profile", file=sys.stderr)
        return 2
    say = lambda m: print(m, file=sys.stderr)  # noqa: E731
    try:
        t.connect(run_after_connect=False)
        if a.viewer_port is not None:
            print("viewer: %s" % t.viewer_url, file=sys.stderr)
        mode = a.signon
        if mode == "auto":
            mode = "env" if os.environ.get("T5250_PASSWORD") else "viewer"
        if mode != "none" and t.profile and t.profile.after_connect:
            t.run_after_connect(wait_for_viewer=600 if mode == "viewer" else 0, announce=say)
        elif mode != "none" and t.on_signon():
            if mode == "env":
                t.signon()
            else:
                t.signon_in_viewer(announce=lambda m: print(m, file=sys.stderr))
    except (TerminalError, FlowError, OSError) as e:
        print("could not get a signed-on session: %s" % e, file=sys.stderr)
        t.close()
        return 3
    code = 0
    try:
        res = t.run(a.flow, **params)
        if a.json:
            print(json.dumps({"flow": res.flow, "ok": True, "captures": res.captures,
                              "steps": res.log}))
        else:
            print("\n".join(res.log))
            for k, v in res.captures.items():
                print("%s=%s" % (k, v))
            if a.screen:
                print(res.screen)
            print("PASSED %s" % a.flow, file=sys.stderr)
    except FlowError as e:
        code = 1
        if a.json:
            print(json.dumps({"flow": a.flow, "ok": False, "error": e.args[0],
                              "step": e.step, "in_flow": e.flow, "screen": e.screen}))
        else:
            print("FAILED %s" % e, file=sys.stderr)
    except (TerminalError, OSError) as e:
        code = 3
        print("FAILED %s" % e, file=sys.stderr)
    finally:
        if a.signoff:
            try:
                print("sign-off: %s" % t.signoff(), file=sys.stderr)
            except (TerminalError, OSError) as e:
                print("sign-off: %s" % e, file=sys.stderr)
        t.close()
    return code


def _host(a, **policy):
    """A host-server connection for cmd/compile: profile or --host/--user;
    password from the keychain, $T5250_PASSWORD, or a prompt (a person at a
    terminal). An expired or rejected password is not retried."""
    import getpass
    from .credentials import CredentialStore
    from .hostservers import HostServers
    from .profiles import ProfileSet
    host, user, tls, libl = a.host, a.user, not a.no_tls, a.libl
    if a.profile:
        p = ProfileSet.load(a.profiles or ["profiles.yaml"]).get(a.profile)
        host, user, tls = p.host, user or p.user, p.tls
        libl = libl or (",".join(p.libl) if p.libl else None)
    if not host or not user:
        raise SystemExit("give --profile, or --host and --user")
    if not a.jt400:
        raise SystemExit("give --jt400 /path/to/jt400.jar (JTOpen) or set $T5250_JT400")
    creds = CredentialStore(keychain=True)
    pw = creds.get(host, user) or os.environ.get("T5250_PASSWORD")
    if not pw:
        if not sys.stdin.isatty():
            raise SystemExit("no password: set $T5250_PASSWORD, or run in a terminal")
        pw = getpass.getpass("IBM i password for %s on %s: " % (user.upper(), host))
    hs = HostServers(a.jt400, a.java, guard=Guard(), **policy)
    hs.connect("cli", host, user, pw, tls, [x for x in (libl or "").split(",") if x] or None)
    return hs


def cmd_cmd(a) -> int:
    from .hostservers import HostServerError
    try:
        hs = _host(a, allow_commands=True)
        res = hs.command("cli", a.command)
    except HostServerError as e:
        print("FAILED %s" % e, file=sys.stderr)
        return 3
    if a.json:
        print(json.dumps(res))
    else:
        for m in res.get("messages", []):
            print("%s (severity %s) %s" % (m.get("id") or "-", m.get("severity"), m.get("text")))
        print("%s (job %s)" % ("ok" if res["ok"] else "FAILED", res.get("job")), file=sys.stderr)
    hs.close()
    return 0 if res["ok"] else 1


def cmd_compile(a) -> int:
    from .hostservers import HostServerError
    try:
        hs = _host(a, compile_libs=["*"])
        res = hs.compile("cli", a.source, a.object, a.type, a.options or "")
    except HostServerError as e:
        print("FAILED %s" % e, file=sys.stderr)
        return 3
    if a.json:
        print(json.dumps(res))
    else:
        for d in res["diagnostics"]:
            print("%s:%d:%d: %s %d %s %s" % (d["file"], d["line"], d["column"],
                                             d["severity_name"], d["severity"], d["id"],
                                             d["text"]))
        for m in res["messages"]:
            if (m.get("severity") or 0) >= 10:
                print("%s (severity %s) %s" % (m.get("id"), m.get("severity"), m.get("text")),
                      file=sys.stderr)
        if res.get("note"):
            print("note: %s" % res["note"], file=sys.stderr)
        print("%s %s" % ("compiled" if res["ok"] else "FAILED", res["object"]), file=sys.stderr)
    hs.close()
    return 0 if res["ok"] else 1


def _host_args(x) -> None:
    x.add_argument("--profile", default=os.environ.get("T5250_PROFILE"))
    x.add_argument("--profiles", action="append",
                   default=[v for v in os.environ.get("T5250_PROFILES", "").split(os.pathsep)
                            if v])
    x.add_argument("--host", default=os.environ.get("T5250_HOST"))
    x.add_argument("--user", default=os.environ.get("T5250_USER"))
    x.add_argument("--no-tls", action="store_true", default=os.environ.get("T5250_TLS") == "0")
    x.add_argument("--libl", help="library list, comma separated (default: the profile's)")
    x.add_argument("--jt400", default=os.environ.get("T5250_JT400"))
    x.add_argument("--java", default=os.environ.get("T5250_JAVA"))
    x.add_argument("--json", action="store_true")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="tn5250-run", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    k = sub.add_parser("cmd", help="run a CL command through the host servers")
    k.add_argument("command")
    _host_args(k)
    k.set_defaults(fn=cmd_cmd)

    m = sub.add_parser("compile", help="compile a source and print its errors")
    m.add_argument("source", help="IFS path, or LIB/FILE(MEMBER)")
    m.add_argument("object", help="LIB/NAME to create")
    m.add_argument("--type", help="rpg, sqlrpg, rpgmod, sqlrpgmod, cl, clmod, dspf, prtf, pf, lf")
    m.add_argument("--options", help="extra compile parameters")
    _host_args(m)
    m.set_defaults(fn=cmd_compile)

    c = sub.add_parser("check", help="validate flow files and list the flows")
    c.add_argument("paths", nargs="*", help="YAML files or folders (default $T5250_FLOWS)")
    c.set_defaults(fn=cmd_check)

    r = sub.add_parser("run", help="connect, sign on and run one flow")
    r.add_argument("flow")
    r.add_argument("params", nargs="*", metavar="name=value")
    r.add_argument("--flows", action="append", default=[],
                   help="YAML file or folder (repeatable; default $T5250_FLOWS)")
    r.add_argument("--host", default=os.environ.get("T5250_HOST"))
    r.add_argument("--jt400", default=os.environ.get("T5250_JT400"),
                   help="jt400.jar (JTOpen): lets flows run query steps")
    r.add_argument("--java", default=os.environ.get("T5250_JAVA"))
    r.add_argument("--profile", default=os.environ.get("T5250_PROFILE"),
                   help="saved connection profile (instead of --host and friends)")
    r.add_argument("--profiles", action="append",
                   default=[x for x in os.environ.get("T5250_PROFILES", "").split(os.pathsep)
                            if x],
                   help="profiles file or folder (default ./profiles.yaml, $T5250_PROFILES)")
    r.add_argument("--port", type=int, default=int(os.environ.get("T5250_PORT", "992")))
    r.add_argument("--no-tls", action="store_true", default=os.environ.get("T5250_TLS") == "0")
    r.add_argument("--insecure", action="store_true",
                   default=os.environ.get("T5250_VERIFY_TLS") == "0",
                   help="don't verify the host's TLS certificate")
    r.add_argument("--device-name", default=os.environ.get("T5250_DEVICE_NAME"))
    r.add_argument("--codepage", default=os.environ.get("T5250_CODEPAGE", "cp037"))
    r.add_argument("--signon", choices=["auto", "env", "viewer", "none"], default="auto")
    r.add_argument("--signoff", action="store_true", help="sign off when done")
    r.add_argument("--viewer-port", type=int, default=None,
                   help="start the live viewer on this port (0 = any free port)")
    r.add_argument("--log-dir", default=os.environ.get("T5250_LOG_DIR"))
    r.add_argument("--pace", type=float, default=float(os.environ.get("T5250_PACE", "0")),
                   help="seconds to wait after each step (to watch in the viewer)")
    r.add_argument("--block", action="append", default=[], help="extra regex to refuse")
    r.add_argument("--no-default-blocks", action="store_true")
    r.add_argument("--json", action="store_true", help="print the result as JSON")
    r.add_argument("--screen", action="store_true", help="print the final screen")
    r.set_defaults(fn=cmd_run)

    a = p.parse_args(argv)
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
