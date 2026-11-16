"""Local web viewer: the human watches the session live, signs on, or takes
over. Bound to 127.0.0.1 and protected by a random token (against other web
pages poking localhost) plus a Host-header check (against DNS rebinding)."""

from __future__ import annotations

import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse

from .manager import GuardError, Manager
from .screen import describe_attr


def _cells(s) -> list[list[list]]:
    """Per row, runs of [text, color, underline, reverse, hidden]."""
    snap_rows = s.rows()
    attrs = s.cell_attrs()
    cols = s.buf.cols
    out = []
    for r, line in enumerate(snap_rows):
        runs: list[list] = []
        for c, ch in enumerate(line):
            a = attrs[r * cols + c]
            raw = s.buf.data[r * cols + c]
            d = describe_attr(a)
            if 0x20 <= raw <= 0x3F:      # the attribute position itself is blank
                key = ("green", False, False, False)
            else:
                key = (d["color"], d["underline"], d["reverse"], d["hidden"])
            if runs and tuple(runs[-1][1:]) == key:
                runs[-1][0] += ch
            else:
                runs.append([ch, *key])
        out.append(runs)
    return out


def start_viewer(mgr: Manager, port: int) -> str:
    """Serve the viewer on 127.0.0.1:port. If that port is taken (another
    stdio server from a second MCP client, say), fall back to a free port."""
    token = os.environ.get("T5250_VIEWER_TOKEN") or secrets.token_urlsafe(18)
    page = resources.files("tn5250_mcp").joinpath("viewer.html").read_text("utf-8")
    allowed_hosts: set[str] = set()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):   # keep stdio clean for MCP
            pass

        def _ok_request(self) -> bool:
            if self.headers.get("Host") not in allowed_hosts:
                self.send_error(403, "bad host")
                return False
            q = parse_qs(urlparse(self.path).query)
            if self.headers.get("X-Token") != token and q.get("token", [""])[0] != token:
                self.send_error(403, "bad token")
                return False
            return True

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._ok_request():
                return
            u = urlparse(self.path)
            if u.path == "/":
                body = page.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; style-src 'unsafe-inline'; "
                                 "script-src 'unsafe-inline'; connect-src 'self'")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/state":
                name = parse_qs(u.query).get("session", [""])[0]
                names = list(mgr.sessions)
                if not names:
                    return self._json({"sessions": [], "screen": None})
                s = mgr.sessions.get(name) or mgr.sessions[names[0]]
                with s.cond:
                    snap = s.snapshot()
                    snap["cells"] = _cells(s)
                for f in snap["fields"]:
                    f["value"] = "" if f["hidden"] else f["value"]
                self._json({"sessions": names, "screen": snap})
            else:
                self.send_error(404)

        def do_POST(self):
            if not self._ok_request():
                return
            u = urlparse(self.path)
            try:
                n = int(self.headers.get("Content-Length", "0"))
                req = json.loads(self.rfile.read(n) or b"{}")
                s = mgr.get(req.get("session", ""))
                if u.path == "/api/reconnect":
                    if s.connected:
                        return self._json({"ok": True})
                    mgr.connect(s.name, s.host, s.port, s.tls, s.verify_tls, s.device_name,
                                s.wide, s.cp.name, 20.0)
                    self._json({"ok": True})
                elif u.path == "/api/press":
                    mgr.press(s, req["key"], values=req.get("values") or {},
                              cursor=req.get("cursor"), wait=False, from_viewer=True)
                    self._json({"ok": True})
                else:
                    self.send_error(404)
            except GuardError as e:
                self._json({"ok": False, "error": str(e)}, 409)
            except OSError as e:
                self._json({"ok": False, "error": "connection problem: %s" % e}, 502)
            except (KeyError, ValueError) as e:
                self._json({"ok": False, "error": "bad request: %s" % e}, 400)

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    except OSError:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    allowed_hosts.update({"127.0.0.1:%d" % port, "localhost:%d" % port})
    threading.Thread(target=srv.serve_forever, name="5250-viewer", daemon=True).start()
    return "http://127.0.0.1:%d/?token=%s" % (port, token)
