"""tn5250-run: check and run screen flows from a shell or CI, no MCP client.

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
                  pace=a.pace)
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
                t.signoff()
            except (TerminalError, OSError) as e:
                print("sign-off: %s" % e, file=sys.stderr)
        t.close()
    return code


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="tn5250-run", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="validate flow files and list the flows")
    c.add_argument("paths", nargs="*", help="YAML files or folders (default $T5250_FLOWS)")
    c.set_defaults(fn=cmd_check)

    r = sub.add_parser("run", help="connect, sign on and run one flow")
    r.add_argument("flow")
    r.add_argument("params", nargs="*", metavar="name=value")
    r.add_argument("--flows", action="append", default=[],
                   help="YAML file or folder (repeatable; default $T5250_FLOWS)")
    r.add_argument("--host", default=os.environ.get("T5250_HOST"))
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
