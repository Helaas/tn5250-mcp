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
import fnmatch
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


LIBRARY = re.compile(r"^[A-Z$#@][A-Z0-9_$#@.]{0,9}$")


def check_libl(libl) -> list[str]:
    """A library list: up to 250 library names (uppercased)."""
    if isinstance(libl, str):
        libl = [x for x in re.split(r"[\s,]+", libl) if x]
    out = [str(x).strip().upper() for x in libl]
    if not out or len(out) > 250 or not all(LIBRARY.match(x) for x in out):
        raise HostServerError("a library list is up to 250 library names, e.g. "
                              "[\"MYLIB\", \"QGPL\"] (got %r)" % (libl,))
    return out


PARAM_TYPE = re.compile(r"^(char|varchar)\((\d+)\)$|^(packed|zoned)\((\d+),(\d+)\)$|"
                        r"^int\((5|10|20)\)$|^uns\((5|10)\)$|^float\((4|8)\)$|^ind$")


def check_param(p, i: int = 1) -> dict:
    """A program parameter: {type, value, out}. Types as in RPG: char(n),
    varchar(n), packed(d,p), zoned(d,p), int(5|10|20), uns(5|10), float(4|8), ind."""
    if not isinstance(p, dict) or "type" not in p or set(p) - {"type", "value", "out"}:
        raise HostServerError("parameter %d: {type, value, out} (got %r)" % (i, p))
    t = str(p["type"]).strip().lower().replace(" ", "")
    m = PARAM_TYPE.match(t)
    if not m:
        raise HostServerError("parameter %d: type is char(n), varchar(n), packed(d,p), "
                              "zoned(d,p), int(5|10|20), uns(5|10), float(4|8) or ind (got %r)"
                              % (i, p["type"]))
    if (m.group(2) and not 1 <= int(m.group(2)) <= 32000) or (
            m.group(4) and not (1 <= int(m.group(4)) <= 63 and int(m.group(5)) <= int(m.group(4)))):
        raise HostServerError("parameter %d: size out of range in %r" % (i, p["type"]))
    v = p.get("value")
    if v is not None and m.group(3) or v is not None and t.startswith(("int", "uns", "float")):
        try:
            float(str(v))
        except ValueError:
            raise HostServerError("parameter %d: %r is not a number" % (i, v)) from None
    return {"type": t, "value": v, "out": bool(p.get("out"))}


def command_name(cmd: str) -> str:
    """CRTBNDRPG for 'QSYS/CRTBNDRPG PGM(...)'."""
    first = cmd.strip().split(None, 1)[0].upper() if cmd.strip() else ""
    return first.rsplit("/", 1)[-1].split("(", 1)[0]


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
                 max_rows: int = 1000, max_read: int = 1_000_000,
                 allow_commands: bool = False, command_allow: list[str] | None = None,
                 compile_libs: list[str] | None = None):
        self.java_command = command or helper_command(jar, java)
        self.helper = Helper(self.java_command)
        self.allow_sql_write = allow_sql_write
        self.ifs_write_roots = [norm_ifs_path(r) for r in (ifs_write_roots or [])]
        self.guard = guard
        self.log = log or (lambda name, kind, text: None)
        self.max_rows, self.max_read = max_rows, max_read
        self.conns: dict[str, dict] = {}
        # CL commands: off unless allowed; optionally only these command names
        self.allow_commands = allow_commands
        self.command_allow = [p.strip().upper() for p in (command_allow or []) if p.strip()]
        # compile: only into these libraries ("*" = any)
        self.compile_libs = [x.strip().upper() for x in (compile_libs or []) if x.strip()]

    # -- connections ----------------------------------------------------------
    def connect(self, name: str, host: str, user: str, password: str, tls: bool = True,
                libl: list[str] | None = None) -> dict:
        if self.guard:
            try:
                self.guard.check_host(host)
            except PermissionError as e:
                raise HostServerError(str(e)) from None
        libl = check_libl(libl) if libl else None
        info = self.helper.call("connect", name=name, host=host, user=user.upper(),
                                password=password, tls=tls, **({"libl": libl} if libl else {}))
        info["_host"] = host
        info["libl"] = libl
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

    # -- CL commands, compiles, job logs, spooled files ---------------------------
    def _guard_command(self, command: str) -> str:
        cmd = command.strip()
        if not cmd:
            raise HostServerError("empty command")
        if self.guard:
            try:
                self.guard.check_values({"command": cmd})
            except PermissionError as e:
                raise HostServerError(str(e)) from None
        return cmd

    def command(self, name: str, command: str, libl: list[str] | None = None) -> dict:
        """Run a CL command (in the connection's remote command job, with the
        library list set first when given). Needs --allow-commands."""
        self._need(name)
        if not self.allow_commands:
            raise HostServerError("running CL commands is off on this server (start it with "
                                  "--allow-commands)")
        cmd = self._guard_command(command)
        verb = command_name(cmd)
        if self.command_allow and not any(fnmatch.fnmatch(verb, p) for p in self.command_allow):
            raise HostServerError("%s is not among the commands this server allows: %s"
                                  % (verb, ", ".join(self.command_allow)))
        return self._run_command(name, cmd, libl)

    def _run_command(self, name: str, cmd: str, libl: list[str] | None) -> dict:
        args = dict(name=name, command=cmd)
        if libl:
            args["libl"] = check_libl(libl)
        res = self.helper.call("command", **args)
        self.conns[name]["_cmdjob"] = res.get("job")
        worst = max((m.get("severity", 0) for m in res.get("messages", [])), default=0)
        self.log(name, "command", "%s%s\n-> %s (%d messages, worst severity %d)" % (
            cmd, " [libl %s]" % " ".join(libl) if libl else "",
            "ok" if res.get("ok") else "FAILED", len(res.get("messages", [])), worst))
        return res

    def call_program(self, name: str, program: str, params: list[dict] | None = None,
                     libl: list[str] | None = None) -> dict:
        """CALL a program with typed parameters (in the command job, with its
        library list) and return the output parameters. Needs --allow-commands
        (and CALL among --command-allow, if that is set)."""
        self._need(name)
        if not self.allow_commands:
            raise HostServerError("calling programs is off on this server (start it with "
                                  "--allow-commands)")
        if self.command_allow and not any(fnmatch.fnmatch("CALL", p) for p in self.command_allow):
            raise HostServerError("CALL is not among the commands this server allows")
        prog = program.strip().upper()
        if not re.fullmatch(r"(?:[A-Z$#@][A-Z0-9_$#@.]{0,9}/)?[A-Z$#@][A-Z0-9_$#@.]{0,9}", prog):
            raise HostServerError("program is NAME or LIB/NAME (got %r)" % program)
        self._guard_command("CALL PGM(%s)" % prog)
        plist = [check_param(p, i) for i, p in enumerate(params or [], 1)]
        if libl:
            libl = check_libl(libl)
        res = self.helper.call("call_program", name=name, program=prog, params=plist,
                               **({"libl": libl} if libl else {}))
        self.conns[name]["_cmdjob"] = res.get("job")
        self.log(name, "call", "%s with %d parameters -> %s" % (
            prog, len(plist), "ok" if res.get("ok") else "FAILED"))
        return res

    def ifs_mkdir(self, name: str, path: str) -> bool:
        """Create a directory (and its parents) for uploads; same rules as writes."""
        self._need(name)
        p = norm_ifs_path(path)
        if not any(under(p, r) for r in self.ifs_write_roots):
            raise HostServerError("%s is outside the folders this server may write to: %s"
                                  % (p, ", ".join(self.ifs_write_roots) or "none"))
        return bool(self.helper.call("ifs_mkdir", name=name, path=p))

    def compile(self, name: str, source: str, obj: str, kind: str | None = None,
                options: str = "", libl: list[str] | None = None,
                listing: bool = True) -> dict:
        """Compile a source (IFS path or LIB/FILE(MBR)) into LIB/NAME and read
        the compiler's event file. Needs --allow-compile with that library."""
        from .compiler import CompileError, build_command, parse_events, split_object
        self._need(name)
        try:
            lib, objname = split_object(obj)
            cmd = build_command(source, obj, kind, options, listing)
        except CompileError as e:
            raise HostServerError(str(e)) from None
        if not self.compile_libs:
            raise HostServerError("compiling is off on this server (start it with "
                                  "--allow-compile LIB[,LIB...])")
        if "*" not in self.compile_libs and lib not in self.compile_libs:
            raise HostServerError("this server compiles only into %s, not %s"
                                  % (", ".join(self.compile_libs), lib))
        cmd = self._guard_command(cmd)
        started = self._server_time(name)
        res = self._run_command(name, cmd, libl)
        events, note = [], None
        try:
            recs = self.helper.call("read_member", name=name, lib=lib, file="EVFEVENT",
                                    member=objname).get("records", [])
            stamp = next((r.split()[2] for r in recs if r.startswith("TIMESTAMP")
                          and len(r.split()) > 2), "")
            if started and stamp and stamp < started:
                note = "the event file is from an earlier compile; see the messages"
            else:
                events = parse_events(recs)
                if not events and not res.get("ok"):
                    errs = [r for r in recs if r.startswith("ERROR")][:5]
                    note = ("the event file has %d records but no errors could be read from "
                            "them%s" % (len(recs), (": " + " | ".join(errs)) if errs else ""))
        except HostServerError:
            note = "no event file was written; see the messages (and job_log)"
        out = {"ok": bool(res.get("ok")), "object": "%s/%s" % (lib, objname), "command": cmd,
               "messages": res.get("messages", []), "job": res.get("job"),
               "diagnostics": [d.__dict__ for d in events], "note": note}
        self.log(name, "compile", "%s -> %s, %d diagnostics" % (
            cmd, "ok" if out["ok"] else "FAILED", len(events)))
        return out

    def _server_time(self, name: str) -> str:
        """The IBM i's clock as YYYYMMDDHHMMSS (to tell old event files from new)."""
        try:
            res = self.helper.call("query", name=name, params=[], max_rows=1, sql=(
                "SELECT VARCHAR_FORMAT(CURRENT TIMESTAMP, 'YYYYMMDDHH24MISS') "
                "FROM SYSIBM.SYSDUMMY1"))
            return str(res["rows"][0][0])
        except (HostServerError, KeyError, IndexError):
            return ""

    def job_log(self, name: str, job: str | None = None, min_severity: int = 0,
                last: int = 50) -> dict:
        """Messages of a job's log (QSYS2.JOBLOG_INFO), newest last. job:
        number/user/name, or None for the job that runs this connection's
        commands and compiles."""
        self._need(name)
        job = (job or self.conns[name].get("_cmdjob") or "").strip()
        if not job:
            raise HostServerError("no command has run on this connection yet; give a job "
                                  "(number/user/name)")
        if not re.fullmatch(r"\d{6}/[A-Za-z0-9_$#@.]{1,10}/[A-Za-z0-9_$#@.]{1,10}", job):
            raise HostServerError("job is number/user/name, e.g. 123456/QUSER/QZRCSRVS")
        res = self.helper.call("query", name=name, params=[job, int(min_severity)],
                               max_rows=max(1, min(last, 1000)), sql=(
            "SELECT MESSAGE_TIMESTAMP, MESSAGE_ID, MESSAGE_TYPE, SEVERITY, "
            "VARCHAR(MESSAGE_TEXT, 1000) AS MESSAGE_TEXT FROM TABLE(QSYS2.JOBLOG_INFO("
            "CAST(? AS VARCHAR(28)))) X WHERE SEVERITY >= ? ORDER BY ORDINAL_POSITION DESC"))
        res["rows"].reverse()
        res["job"] = job
        return res

    def spooled_files(self, name: str, job: str | None = None, last: int = 20) -> dict:
        """The user's newest spooled files (QSYS2.SPOOLED_FILE_INFO: fast, unlike
        the output queue views on a busy system), optionally of one job. Output
        of commands run through the host servers lands in the user's QPRTJOB job."""
        self._need(name)
        sql = ("SELECT SPOOLED_FILE_NAME, SPOOLED_FILE_NUMBER, QUALIFIED_JOB_NAME, STATUS, "
               "TOTAL_PAGES, USER_DATA, CREATION_TIMESTAMP FROM TABLE(QSYS2.SPOOLED_FILE_INFO()) X")
        params: list = []
        if job:
            sql += " WHERE QUALIFIED_JOB_NAME = ?"
            params = [job.strip().upper()]
        sql += " ORDER BY CREATION_TIMESTAMP DESC"
        return self.helper.call("query", name=name, sql=sql, params=params,
                                max_rows=max(1, min(last, 500)))

    def spooled_file(self, name: str, job: str, file: str, number: str = "*LAST",
                     max_lines: int = 2000) -> dict:
        self._need(name)
        res = self.helper.call("spooled_file", name=name, job=job.strip(), file=file.strip(),
                               number=str(number), max_lines=max(1, min(max_lines, 20000)))
        if not res.get("lines"):
            raise HostServerError(
                "no spooled file %s (number %s) in job %s. Note: output of commands run "
                "through the host servers is in your QPRTJOB job; spooled_files lists your "
                "newest spooled files with their jobs" % (file, number, job))
        self.log(name, "spooled file", "%s %s %s (%d lines)" % (job, file, number,
                                                                len(res.get("lines", []))))
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
                tls: bool, libl: list[str] | None = None) -> tuple[dict, list[str]]:
    """Connect with a saved, typed or env password, guarding the profile:
    a rejected saved password is dropped and asked for once more; an expired
    password is never retried, the person chooses a new one in the viewer."""
    notes: list[str] = []
    pw, source, remember = host_password(mgr, name, user, host)
    for attempt in range(3):
        try:
            info = hs.connect(name, host, user, pw, tls, libl)
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
