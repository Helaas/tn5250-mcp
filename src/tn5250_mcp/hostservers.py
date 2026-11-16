"""SQL and IFS access to IBM i through its host servers (database and file
servers), using the IBM Toolbox for Java (jt400 / JTOpen) that the user
supplies. tn5250-mcp doesn't ship it: get JTOpen (open source, IBM Public
License) and pass --jt400 /path/to/jt400.jar.

A small Java helper (java/T5250Helper.java, shipped compiled as
java/t5250-helper.jar, so any Java 11+ runtime runs it; no compiler needed)
does the work. No Java installed? `pip install "tn5250-mcp[java]"` brings one
(jdk4py, OpenJDK as a Python package), which is then used automatically; this module starts it, talks JSON
lines to it, and applies the rules:

- queries run on a connection the database server itself keeps read-only;
- statements that change data need --allow-sql-write;
- IFS writes need --ifs-write-root and stay under those folders;
- the block list applies to SQL text and IFS write paths;
- passwords come from the viewer prompt or the environment, never from the
  agent, and are never logged.
"""

from __future__ import annotations

import base64
import collections
import json
import os
import posixpath
import re
import shutil
import subprocess
import threading
from importlib import resources
from pathlib import Path

JTOPEN_URL = "https://github.com/IBM/JTOpen/releases"
HELPER = "T5250Helper.java"
HELPER_JAR = "t5250-helper.jar"
_JAVA_HOMEBREW = ["/opt/homebrew/opt/openjdk/bin/java", "/usr/local/opt/openjdk/bin/java"]


class HostServerError(Exception):
    """A host-server request failed or was refused; the message says why.
    `security` is set when IBM i refused the user or password."""

    def __init__(self, message: str, security: bool = False, expired: bool = False):
        super().__init__(message)
        self.security, self.expired = security, expired


def java_version(java: str) -> int:
    try:
        out = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return 0
    m = re.search(r'version "(\d+)(?:\.(\d+))?', out.stderr + out.stdout)
    if not m:
        return 0
    major = int(m.group(1))
    return int(m.group(2) or 0) if major == 1 else major       # "1.8" -> 8


def jdk4py_java() -> str | None:
    """The Java runtime of the jdk4py package (pip install "tn5250-mcp[java]")."""
    try:
        import jdk4py
    except ImportError:
        return None
    java = str(jdk4py.JAVA)
    return java if os.path.exists(java) and tuple(jdk4py.JAVA_VERSION) >= (11,) else None


def find_java(explicit: str | None = None) -> str:
    """A Java 11+ runtime: --java / $T5250_JAVA, $JAVA_HOME, the jdk4py
    package, macOS java_home, Homebrew, then PATH."""
    if explicit:
        if os.path.exists(explicit) and java_version(explicit) >= 11:
            return explicit
        raise HostServerError("%s is not a Java 11 or newer runtime" % explicit)
    cands = []
    if os.environ.get("T5250_JAVA"):
        cands.append(os.environ["T5250_JAVA"])
    if os.environ.get("JAVA_HOME"):
        cands.append(os.path.join(os.environ["JAVA_HOME"], "bin", "java"))
    bundled = jdk4py_java()
    if bundled:
        cands.append(bundled)
    if os.path.exists("/usr/libexec/java_home"):
        try:
            home = subprocess.run(["/usr/libexec/java_home", "-v", "11+"], capture_output=True,
                                  text=True, timeout=20).stdout.strip()
            if home:
                cands.append(os.path.join(home, "bin", "java"))
        except (OSError, subprocess.SubprocessError):
            pass
    cands += sorted((str(p) for p in Path("/opt/homebrew/opt").glob("openjdk@*/bin/java")),
                    reverse=True) if Path("/opt/homebrew/opt").exists() else []
    cands += _JAVA_HOMEBREW
    if shutil.which("java"):
        cands.append(shutil.which("java"))
    seen = set()
    for c in cands:
        if c in seen or not os.path.exists(c):
            continue
        seen.add(c)
        if java_version(c) >= 11:
            return c
    raise HostServerError("no Java 11 or newer runtime found: pip install \"tn5250-mcp[java]\" "
                          "brings one, or install one (e.g. brew install openjdk@21), or pass "
                          "--java /path/to/bin/java")


class Helper:
    """The Java helper process. Requests are serialized; if the process dies
    it is started again on the next request (connections are then gone)."""

    def __init__(self, command: list[str]):
        self.command = command
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.ids = 0
        self.stderr = collections.deque(maxlen=50)
        self.version = None
        self.generation = 0          # +1 per process start: connections don't survive one

    def _start(self) -> None:
        try:
            self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, encoding="utf-8", bufsize=1)
        except OSError as e:
            raise HostServerError("could not start the Java helper: %s" % e) from None
        self.generation += 1
        threading.Thread(target=self._drain, args=(self.proc,), daemon=True).start()
        line = self.proc.stdout.readline()
        if not line:
            self.proc.wait(timeout=10)
            raise HostServerError("the Java helper did not start:\n" + "\n".join(self.stderr))
        hello = json.loads(line)
        self.version = hello.get("jt400")

    def _drain(self, proc) -> None:
        for line in proc.stderr:
            self.stderr.append(line.rstrip())

    def call(self, op: str, **args):
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()
            self.ids += 1
            req = dict(args, op=op, id=self.ids)
            try:
                self.proc.stdin.write(json.dumps(req) + "\n")
                self.proc.stdin.flush()
                line = self.proc.stdout.readline()
            except OSError as e:
                raise HostServerError("the Java helper stopped: %s" % e) from None
            if not line:
                tail = "\n".join(list(self.stderr)[-5:])
                self.proc = None
                raise HostServerError("the Java helper stopped" + (":\n" + tail if tail else ""))
            resp = json.loads(line)
            if not resp.get("ok"):
                raise HostServerError(resp.get("error") or "request failed",
                                      security=bool(resp.get("security")),
                                      expired=bool(resp.get("expired")))
            return resp.get("result")

    def close(self) -> None:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.stdin.close()
                    self.proc.wait(timeout=10)
                except (OSError, subprocess.SubprocessError):
                    self.proc.kill()
            self.proc = None


def helper_command(jar: str, java: str | None = None) -> list[str]:
    if not os.path.isfile(jar):
        raise HostServerError("jt400 jar not found: %s (get JTOpen: %s)" % (jar, JTOPEN_URL))
    here = resources.files("tn5250_mcp").joinpath("java")
    base = [find_java(java), "-Xshare:auto", "-Djava.awt.headless=true"]
    helper_jar = here.joinpath(HELPER_JAR)
    if helper_jar.is_file():                  # compiled: runs on any Java 11+ runtime
        return base + ["-cp", str(helper_jar) + os.pathsep + jar, "T5250Helper"]
    # a source checkout without the jar: Java compiles the helper as it starts
    # (needs a full JDK; see scripts/build_helper.py)
    return base + ["-cp", jar, str(here.joinpath(HELPER))]


def norm_ifs_path(path: str) -> str:
    """Absolute, normalized IFS path; no '..' tricks."""
    if not path or not path.startswith("/"):
        raise HostServerError("IFS paths are absolute, e.g. /home/MYUSER/file.txt")
    if "\x00" in path:
        raise HostServerError("bad IFS path")
    norm = posixpath.normpath(path)
    if norm.startswith("//"):
        norm = "/" + norm.lstrip("/")
    if any(p == ".." for p in path.split("/")):
        raise HostServerError("'..' is not allowed in IFS paths")
    return norm


def under(path: str, root: str) -> bool:
    """Is `path` inside `root`? The IFS root file system ignores case;
    /QOpenSys doesn't."""
    case = path.startswith("/QOpenSys") or root.startswith("/QOpenSys")
    p, r = (path, root) if case else (path.lower(), root.lower())
    r = r.rstrip("/") or "/"
    return p == r or p.startswith(r if r == "/" else r + "/")


class HostServers:
    """Named host-server connections plus the rules around them. Used by the
    MCP server and directly from Python:

        hs = HostServers("jt400.jar")
        hs.connect("main", "pub400.com", "MYUSER", password=os.environ["PW"])
        print(hs.query("main", "SELECT * FROM QSYS2.SYSTABLES FETCH FIRST 5 ROWS ONLY"))
    """

    def __init__(self, jar: str | None = None, java: str | None = None, *,
                 allow_sql_write: bool = False, ifs_write_roots: list[str] | None = None,
                 guard=None, log=None, command: list[str] | None = None,
                 max_rows: int = 1000, max_read: int = 1_000_000):
        self.command = command or helper_command(jar, java)
        self.helper = Helper(self.command)
        self.allow_sql_write = allow_sql_write
        self.ifs_write_roots = [norm_ifs_path(r) for r in (ifs_write_roots or [])]
        self.guard = guard
        self.log = log or (lambda name, kind, text: None)
        self.max_rows, self.max_read = max_rows, max_read
        self.conns: dict[str, dict] = {}

    # -- connections ----------------------------------------------------------
    def connect(self, name: str, host: str, user: str, password: str, tls: bool = True) -> dict:
        if self.guard:
            try:
                self.guard.check_host(host)
            except PermissionError as e:
                raise HostServerError(str(e)) from None
        info = self.helper.call("connect", name=name, host=host, user=user.upper(),
                                password=password, tls=tls)
        info["_host"] = host
        info["_gen"] = self.helper.generation
        self.conns[name] = info
        self.log(name, "host connect", "%s as %s (%s, %s)" % (
            host, info.get("user"), info.get("release"), "TLS" if tls else "plain"))
        return info

    def change_password(self, host: str, user: str, old: str, new: str,
                        tls: bool = True) -> None:
        """Change a user's password (e.g. an expired one). Never logged."""
        self.helper.call("change_password", host=host, user=user.upper(), old=old, new=new,
                         tls=tls)
        self.log("host-" + user.upper(), "password changed", "%s on %s" % (user.upper(), host))

    def close_user(self, host: str, user: str) -> list[str]:
        """Close the connections of this user on this host (after a password
        change they hold the old password, and would sign on with it again)."""
        names = [n for n, i in self.conns.items()
                 if i.get("_host", "").lower() == host.lower()
                 and str(i.get("user", "")).upper() == user.upper()]
        for n in names:
            try:
                self.disconnect(n)
            except HostServerError:
                self.conns.pop(n, None)
        return names

    def disconnect(self, name: str) -> None:
        self.helper.call("close", name=name)
        self.conns.pop(name, None)
        self.log(name, "host disconnect", "")

    def connected(self, name: str) -> bool:
        """Open, and in the helper process that is running now."""
        info = self.conns.get(name)
        return bool(info) and info.get("_gen") == self.helper.generation and \
            self.helper.proc is not None and self.helper.proc.poll() is None

    def _need(self, name: str) -> None:
        if name in self.conns and not self.connected(name):
            self.conns.pop(name, None)
            raise HostServerError("the connection %r was lost (the Java helper restarted); "
                                  "call host_connect again" % name)
        if name not in self.conns:
            raise HostServerError("no host-server connection %r; use host_connect first%s" % (
                name, " (open: %s)" % ", ".join(self.conns) if self.conns else ""))

    def close(self) -> None:
        self.helper.close()
        self.conns.clear()

    # -- SQL --------------------------------------------------------------------
    def _check_sql(self, sql: str) -> None:
        if not sql.strip():
            raise HostServerError("empty SQL statement")
        if self.guard:
            try:
                self.guard.check_values({"sql": sql})
            except PermissionError as e:
                raise HostServerError(str(e)) from None

    def query(self, name: str, sql: str, params: list | None = None,
              max_rows: int = 100) -> dict:
        """Run a query on a read-only connection: the database server refuses
        anything that would change data."""
        self._need(name)
        self._check_sql(sql)
        try:
            res = self.helper.call("query", name=name, sql=sql, params=params or [],
                                   max_rows=max(1, min(max_rows, self.max_rows)))
        except HostServerError as e:
            if re.search(r"42505|42987|SQL0817|SQL7008", str(e)):
                raise HostServerError(
                    "sql_query is read-only, and this statement would change data%s (%s)" % (
                        "; use sql_execute" if self.allow_sql_write else
                        "; changing data is off on this server", e)) from None
            raise
        self.log(name, "sql query", "%s\n-> %s" % (sql, "%d rows%s" % (
            len(res.get("rows", [])), " (more not shown)" if res.get("truncated") else "")
            if "rows" in res else res))
        return res

    def execute(self, name: str, sql: str, params: list | None = None) -> dict:
        self._need(name)
        if not self.allow_sql_write:
            raise HostServerError("changing data with SQL is off on this server (start it with "
                                  "--allow-sql-write); use sql_query for queries")
        self._check_sql(sql)
        res = self.helper.call("execute", name=name, sql=sql, params=params or [],
                               max_rows=self.max_rows)
        self.log(name, "sql execute", "%s\n-> %s" % (sql, res.get("update_count", "result set")))
        return res

    # -- IFS --------------------------------------------------------------------
    def ifs_list(self, name: str, path: str, max_entries: int = 500) -> dict:
        self._need(name)
        res = self.helper.call("ifs_list", name=name, path=norm_ifs_path(path),
                               max_entries=max_entries)
        self.log(name, "ifs list", path)
        return res

    def ifs_read(self, name: str, path: str, max_bytes: int = 100_000,
                 binary: bool = False) -> dict:
        self._need(name)
        res = self.helper.call("ifs_read", name=name, path=norm_ifs_path(path),
                               max_bytes=max(1, min(max_bytes, self.max_read)), binary=binary)
        self.log(name, "ifs read", "%s (%s bytes%s)" % (path, res.get("size"),
                                                        ", truncated" if res.get("truncated")
                                                        else ""))
        return res

    def ifs_write(self, name: str, path: str, content: str, *, binary: bool = False,
                  ccsid: int = 1208, overwrite: bool = False, append: bool = False) -> dict:
        self._need(name)
        p = norm_ifs_path(path)
        if not self.ifs_write_roots:
            raise HostServerError("writing to the IFS is off on this server (start it with "
                                  "--ifs-write-root /some/folder)")
        if not any(under(p, r) for r in self.ifs_write_roots):
            raise HostServerError("%s is outside the folders this server may write to: %s"
                                  % (p, ", ".join(self.ifs_write_roots)))
        if self.guard:
            try:
                self.guard.check_values({"path": p})
            except PermissionError as e:
                raise HostServerError(str(e)) from None
        args = dict(name=name, path=p, overwrite=overwrite, append=append)
        if binary:
            try:
                base64.b64decode(content, validate=True)
            except ValueError:
                raise HostServerError("binary content must be base64") from None
            args["base64"] = content
        else:
            args.update(text=content, ccsid=ccsid)
        res = self.helper.call("ifs_write", **args)
        self.log(name, "ifs write", "%s (%s bytes%s)" % (p, res.get("size"),
                                                         ", appended" if append else ""))
        return res


# -- text output for the agent ---------------------------------------------------
def format_table(res: dict, max_width: int = 40) -> str:
    if "rows" not in res:
        n = res.get("update_count")
        return "done (%s)" % ("%d row%s changed" % (n, "" if n == 1 else "s") if n and n > 0
                              else "no rows changed")
    cols = res["columns"]
    rows = [["NULL" if v is None else str(v).replace("\n", " ") for v in r] for r in res["rows"]]
    cut = lambda s: s if len(s) <= max_width else s[:max_width - 1] + "…"  # noqa: E731
    cells = [[cut(c) for c in cols]] + [[cut(v) for v in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(cols))]
    line = lambda r: "  ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip()  # noqa: E731
    out = [line(cells[0]), "  ".join("-" * w for w in widths)] + [line(r) for r in cells[1:]]
    out.append("(%d row%s%s)" % (len(rows), "" if len(rows) == 1 else "s",
                                 "; more rows not shown, raise max_rows or narrow the query"
                                 if res.get("truncated") else ""))
    return "\n".join(out)


# -- signing on (shared by the MCP tools and screen flows) ------------------------
def host_password(mgr, name: str, user: str, host: str) -> tuple[str, str, bool]:
    """Password for a host-server connection, and where it came from: saved
    (from an earlier sign-on of this user on this host), typed in the viewer
    when somebody has it open, or $T5250_PASSWORD. Never from the agent."""
    saved = mgr.creds.get(host, user)
    if saved:
        return saved, "saved", False
    if mgr.viewer_url and mgr.viewer_watching_any():
        v, remember = mgr.ask_secret("host:" + name, "IBM i password for %s on %s"
                                     % (user.upper(), host), offer_remember=True)
        return v, "typed", remember
    if os.environ.get("T5250_PASSWORD"):
        return os.environ["T5250_PASSWORD"], "env", False
    if mgr.viewer_url:
        raise HostServerError("the password for %s is typed by a person: give the user the "
                              "viewer link (%s), ask them to keep it open, then call "
                              "host_connect again" % (user, mgr.viewer_url))
    raise HostServerError("no password: set $T5250_PASSWORD, or run with the viewer on")


def host_login(mgr, hs: "HostServers", name: str, host: str, user: str,
                tls: bool) -> tuple[dict, list[str]]:
    """Connect with a saved, typed or env password, guarding the profile:
    a rejected saved password is dropped and asked for once more; an expired
    password is never retried, the person chooses a new one in the viewer."""
    notes: list[str] = []
    pw, source, remember = host_password(mgr, name, user, host)
    for attempt in range(3):
        try:
            info = hs.connect(name, host, user, pw, tls)
            break
        except HostServerError as e:
            if e.expired:
                pw = change_expired_password(mgr, hs, name, host, user, pw, tls, source)
                source = "changed"
                notes.append("the password of %s had expired; it was changed in the viewer"
                             % user.upper())
                continue                     # sign on with the new one
            if e.security and source == "saved" and attempt == 0:
                mgr.creds.invalidate(host, user)     # changed elsewhere? never retry it
                pw, source, remember = host_password(mgr, name, user, host)
                continue
            raise
    if source == "typed":
        mgr.creds.put(host, user, pw, remember=remember)
    days = info.get("expires_in_days")
    if days is not None:
        notes.append("the password of %s expires in %d day%s; change it soon (an expired "
                     "password is changed in the viewer at the next sign-on)"
                     % (user.upper(), days, "" if days == 1 else "s"))
    return info, notes


def change_expired_password(mgr, hs: "HostServers", name: str, host: str, user: str,
                             old: str, tls: bool, source: str) -> str:
    if source == "env" or not (mgr.viewer_url and mgr.viewer_watching_any()):
        raise HostServerError(
            "the password of %s on %s has expired. Nothing was retried. A person has to "
            "choose a new one: give the user the viewer link%s and call host_connect again, "
            "or change it on the system." % (user.upper(), host,
                                             " (%s)" % mgr.viewer_url if mgr.viewer_url else ""))
    message = "The password has expired. Choose a new one."
    for _ in range(3):
        new, _remember = mgr.ask_secret(
            "host:" + name, "New password for %s on %s" % (user.upper(), host),
            kind="new", message=message)
        try:
            hs.change_password(host, user, old, new, tls)
        except HostServerError as e:
            if e.security and "not correct" in str(e).lower():
                raise                        # wrong current password: stop, never retry
            message = "Not accepted: %s" % str(e).splitlines()[0]
            continue
        mgr.creds.replace(host, user, new)
        mgr.on_password_changed(host, user)
        return new
    raise HostServerError("the new password was not accepted three times (%s); nothing else "
                          "was tried" % message)
