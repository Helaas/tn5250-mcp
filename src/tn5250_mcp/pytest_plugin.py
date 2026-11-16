"""pytest plugin: a signed-on 5250 session as a fixture.

Installed automatically with tn5250-mcp. Configure with options or env vars:

    T5250_HOST=pub400.com T5250_USER=... T5250_PASSWORD=... \\
        pytest --t5250-flows flows/

    def test_add_order(t5250):
        r = t5250.run("add_order", customer="C100", qty=3)
        assert r.captures["order_no"]

Tests that use `t5250` are skipped when no host is configured. When a test
fails, the screen at that moment is added to the report.
"""

from __future__ import annotations

import os

import pytest

from .flows import FlowError, FlowSet
from .terminal import Terminal, TerminalError


def pytest_addoption(parser):
    g = parser.getgroup("tn5250", "IBM i 5250 sessions (tn5250-mcp)")
    g.addoption("--t5250-host", default=os.environ.get("T5250_HOST"),
                help="IBM i host ($T5250_HOST); tests using t5250 are skipped without it")
    g.addoption("--t5250-profile", default=os.environ.get("T5250_PROFILE"),
                help="saved connection profile instead of a host ($T5250_PROFILE)")
    g.addoption("--t5250-profiles", action="append",
                default=[x for x in os.environ.get("T5250_PROFILES", "").split(os.pathsep) if x],
                help="profiles file or folder ($T5250_PROFILES, default ./profiles.yaml)")
    g.addoption("--t5250-port", type=int, default=int(os.environ.get("T5250_PORT", "992")))
    g.addoption("--t5250-no-tls", action="store_true",
                default=os.environ.get("T5250_TLS") == "0")
    g.addoption("--t5250-insecure", action="store_true",
                default=os.environ.get("T5250_VERIFY_TLS") == "0",
                help="don't verify the TLS certificate")
    g.addoption("--t5250-flows", action="append",
                default=[x for x in os.environ.get("T5250_FLOWS", "").split(os.pathsep) if x],
                help="flow YAML file or folder (repeatable, $T5250_FLOWS)")
    g.addoption("--t5250-log-dir", default=os.environ.get("T5250_LOG_DIR"),
                help="write session transcripts here (test evidence)")
    g.addoption("--t5250-viewer-port", type=int,
                default=int(os.environ["T5250_VIEWER_PORT"])
                if os.environ.get("T5250_VIEWER_PORT") else None,
                help="start the live viewer; without $T5250_PASSWORD, sign on there by hand")


@pytest.fixture(scope="session")
def t5250_flows(request) -> FlowSet | None:
    paths = request.config.getoption("--t5250-flows")
    return FlowSet.load(paths) if paths else None


@pytest.fixture(scope="session")
def t5250(request, t5250_flows):
    """A connected, signed-on Terminal shared by the test session. Signs off
    at the end."""
    cfg = request.config
    host = cfg.getoption("--t5250-host")
    profile = cfg.getoption("--t5250-profile")
    if not host and not profile:
        pytest.skip("no IBM i host: set $T5250_HOST or $T5250_PROFILE (--t5250-host/-profile)")
    viewer_port = cfg.getoption("--t5250-viewer-port")
    if not os.environ.get("T5250_PASSWORD") and viewer_port is None:
        pytest.skip("no credentials: set $T5250_USER/$T5250_PASSWORD, or "
                    "--t5250-viewer-port to sign on by hand")
    common = dict(flows=t5250_flows, log_dir=cfg.getoption("--t5250-log-dir"),
                  viewer_port=viewer_port)
    if profile:
        t = Terminal.from_profile(profile, cfg.getoption("--t5250-profiles") or
                                  ["profiles.yaml"], **common)
    else:
        t = Terminal(host, cfg.getoption("--t5250-port"),
                     tls=not cfg.getoption("--t5250-no-tls"),
                     verify_tls=not cfg.getoption("--t5250-insecure"), **common)
    t.connect(run_after_connect=False)
    rep = cfg.pluginmanager.get_plugin("terminalreporter")
    say = (lambda m: rep.write_line(m)) if rep else print
    try:
        if t.profile and t.profile.after_connect:
            t.run_after_connect(wait_for_viewer=600 if viewer_port is not None else 0,
                                announce=say)
        elif t.on_signon():
            if os.environ.get("T5250_PASSWORD"):
                t.signon()
            else:
                t.signon_in_viewer(announce=say)
    except (TerminalError, FlowError):
        t.close()
        raise
    yield t
    try:
        t.signoff()
    except (TerminalError, OSError):
        pass
    t.close()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call" and rep.failed:
        t = item.funcargs.get("t5250") if hasattr(item, "funcargs") else None
        if isinstance(t, Terminal) and t.s is not None:
            try:
                rep.sections.append(("5250 screen at failure", t.screen()))
            except Exception:  # noqa: BLE001 - never hide the real failure
                pass
