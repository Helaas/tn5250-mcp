"""Command tools: CL commands with typed parameters, defined in YAML next to
screen flows, that become MCP tools of their own.

They are for the commands a shop already builds and deploys with (its own
compile or promotion commands, a change-management tool's, a build CL
program) when the agent should use those instead of compiling directly:

    commands:
      compile_member:
        description: Compile a member the way this shop does
        params:
          lib: {type: string, upper: true, pattern: '^[A-Z$#@][A-Z0-9_$#@]{0,9}$'}
          member: {type: string, upper: true, pattern: '^[A-Z$#@][A-Z0-9_$#@]{0,9}$'}
          text: {type: string, quote: true, default: ""}     # inserted as 'it''s'
        libl: [MYLIB, QGPL]
        command: SBMJOB CMD(MYBUILD LIB({lib}) MBR({member}) TEXT({text})) JOB({member})
        wait: true            # wait for the job it submits; ok only if it ends normally
        timeout: 600

After the command(s) the tool reads what the work left behind: the
submitted job's spooled files (or, without a submitted job, what landed in
your QPRTJOB job meanwhile), turns compiler listings into errors with line
numbers (see listings.py) and the spooled job log into messages.

Values go into the command as CL tokens: without `quote: true` a value may
only hold name-like characters (letters, digits, _ $ # @ . * / , + - :),
so a parameter can't add parameters or commands. Every command goes through
the server's command rules (--allow-commands, --command-allow, the block
list), with the SBMJOB'd command checked too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .flows import (NAME_RE, FlowConfigError, Param, _parse_param, _strs, _template_names,
                    fill_template)
from .hostservers import HostServerError, check_libl
from .listings import parse_joblog, parse_listing

COMMAND_KEYS = {"description", "toolset", "params", "command", "libl", "wait", "timeout",
                "spooled", "delete_spooled", "expose"}
CL_TOKEN = re.compile(r"^[A-Za-z0-9_$#@.*/,+:\-]*$")
_JOB = re.compile(r"\b(\d{6}/[A-Z0-9_$#@.]{1,10}/[A-Z0-9_$#@.]{1,10})\b")


@dataclass
class CommandTool:
    name: str
    description: str
    params: dict[str, Param]
    quote: set[str]
    commands: list[str]
    libl: list[str] | None = None
    wait: bool = False
    timeout: int = 600
    spooled: bool = True
    delete_spooled: bool = False
    toolset: str | None = None
    expose: bool = True
    source: str = ""
    read_only: bool = False          # (they run commands: never read-only)


def parse_commands(doc: dict, source: str, default_toolset=None) -> dict[str, CommandTool]:
    out: dict[str, CommandTool] = {}
    spec_all = doc.get("commands")
    if not isinstance(spec_all, dict):
        raise FlowConfigError("%s: 'commands:' must be a mapping of tool names" % source)
    for name, spec in spec_all.items():
        where = "%s: command tool %s" % (source, name)
        if not NAME_RE.match(str(name)):
            raise FlowConfigError("%s: names must be lower_snake_case" % where)
        if not isinstance(spec, dict):
            raise FlowConfigError("%s: must be a mapping" % where)
        bad = set(spec) - COMMAND_KEYS
        if bad:
            raise FlowConfigError("%s: unknown keys %s" % (where, sorted(bad)))
        if not spec.get("description"):
            raise FlowConfigError("%s: needs a description (the agent reads it)" % where)
        params, quote = {}, set()
        for k, v in (spec.get("params") or {}).items():
            v = dict(v) if isinstance(v, dict) else v
            if isinstance(v, dict) and v.pop("quote", False):
                quote.add(str(k))
            if str(k) in ("connection", "format"):
                raise FlowConfigError("%s: %s is taken (the tool's own parameter)" % (where, k))
            p = _parse_param(str(k), v, where)
            if p.type == "secret":
                raise FlowConfigError("%s: parameter %s: secrets can't go into a command"
                                      % (where, k))
            params[str(k)] = p
        cmds = spec.get("command")
        cmds = _strs(cmds, "%s: command" % where) if cmds else []
        if not cmds:
            raise FlowConfigError("%s: needs command (a CL command, or a list of them)" % where)
        for c in cmds:
            unknown = _template_names(c) - set(params)
            if unknown:
                raise FlowConfigError("%s: {%s} is not a parameter" % (where,
                                                                       ", ".join(sorted(unknown))))
        libl = None
        if spec.get("libl"):
            try:
                libl = check_libl(spec["libl"])
            except HostServerError as e:
                raise FlowConfigError("%s: libl: %s" % (where, e)) from None
        timeout = spec.get("timeout", 600)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 86400:
            raise FlowConfigError("%s: timeout is seconds (1-86400)" % where)
        out[str(name)] = CommandTool(
            name=str(name), description=str(spec["description"]).strip(), params=params,
            quote=quote, commands=cmds, libl=libl, wait=bool(spec.get("wait", False)),
            timeout=timeout, spooled=bool(spec.get("spooled", True)),
            delete_spooled=bool(spec.get("delete_spooled", False)),
            toolset=spec.get("toolset", default_toolset), expose=bool(spec.get("expose", True)),
            source=source)
    return out


def bind(tool: CommandTool, raw: dict[str, Any]) -> dict[str, str]:
    """Parameter values as CL text: checked, defaults filled, quoted or tokens."""
    extra = set(raw) - set(tool.params)
    if extra:
        raise HostServerError("%s has no parameter %s" % (tool.name, ", ".join(sorted(extra))))
    vals = {}
    for n, p in tool.params.items():
        if n in raw and raw[n] is not None:
            v = raw[n]
        elif not p.required:
            v = p.default
        else:
            raise HostServerError("%s needs %s" % (tool.name, n))
        try:
            text = p.coerce(v)
        except ValueError as e:
            raise HostServerError(str(e)) from None
        if n in tool.quote:
            text = "'" + text.replace("'", "''") + "'"
        elif not CL_TOKEN.match(text):
            raise HostServerError("%s: %r can't go into the command as it is (only letters, "
                                  "digits and _ $ # @ . * / , + - :); a parameter that takes "
                                  "free text needs quote: true" % (n, text))
        vals[n] = text
    return vals


def expand(tool: CommandTool, raw: dict[str, Any]) -> list[str]:
    vals = bind(tool, raw)
    return [fill_template(c, vals) for c in tool.commands]


def _digits(ts: str) -> str:
    return re.sub(r"\D", "", str(ts or ""))[:14]


def run_tool(hs, name: str, tool: CommandTool, raw: dict[str, Any], say=lambda m: None) -> dict:
    """Run the tool's commands, wait for a submitted job when asked, and read
    the listings and job log it left. Returns a report (see result_text)."""
    cmds = expand(tool, raw)
    since = hs.server_timestamp(name)
    ran, ok = [], True
    for cmd in cmds:
        say("running %s" % cmd)
        r = hs.command(name, cmd, tool.libl)
        ran.append({"command": cmd, "ok": bool(r.get("ok")), "messages": r.get("messages", [])})
        if not r.get("ok"):
            ok = False
            break
    job = None
    for r in reversed(ran):
        for m in r["messages"]:
            if m.get("id") == "CPC1221":
                hit = _JOB.search(m.get("text") or "")
                if hit:
                    job = hit.group(1)
                    break
        if job:
            break
    out: dict[str, Any] = {"tool": tool.name, "commands": ran, "job": job, "end_code": None,
                           "end_message": None, "timed_out": False, "note": None}
    if tool.wait and ok:
        if not job:
            out["note"] = ("wait: no submitted job in the messages (CPC1221); the command "
                           "may have run in the command job itself")
        else:
            say("waiting for job %s" % job)
            ended = hs.wait_for_job(name, job, since, tool.timeout)
            out.update(end_code=ended["end_code"], end_message=ended["message"],
                       timed_out=not ended["ended"])
            if not ended["ended"] or (ended["end_code"] or 0) >= 20:
                ok = False
    diags, joblog, spooled = [], [], []
    if tool.spooled and not out["timed_out"]:
        try:
            rows = hs.spooled_files(name, job=job, last=50)["rows"]
        except HostServerError as e:
            rows = []
            out["note"] = (out["note"] or "") + " spooled files: %s" % e
        if not job:            # output of commands run here lands in the user's QPRTJOB
            rows = [r for r in rows if str(r[2]).upper().endswith("/QPRTJOB")
                    and _digits(r[6]) >= _digits(since)]
        for r in rows[:20]:
            file, number, sjob = str(r[0]).strip(), str(r[1]), str(r[2])
            try:
                lines = hs.spooled_file(name, sjob, file, number)["lines"]
            except HostServerError:
                continue
            if file == "QPJOBLOG":
                joblog = parse_joblog(lines)
                spooled.append({"file": file, "number": number, "job": sjob, "kind": "job log"})
            else:
                lst = parse_listing(lines)
                diags += lst["diagnostics"]
                spooled.append({"file": file, "number": number, "job": sjob,
                                "kind": "%s listing" % lst["compiler"] if lst["compiler"]
                                else "spooled file", "source": lst["source"],
                                "errors": len(lst["diagnostics"])})
            if tool.delete_spooled:
                hs._run_command(name, "DLTSPLF FILE(%s) JOB(%s) SPLNBR(%s)"
                                % (file, sjob, number), None)
    # SQL RPG: the RPG step compiles the precompiler's copy in QTEMP; point at the source
    real = next((s["source"] for s in spooled if s.get("source")
                 and not s["source"].upper().startswith("QTEMP/")), None)
    for d in diags:
        if real and str(d.get("file") or "").upper().startswith("QTEMP/QSQLTEMP"):
            d["file"] = real
    diags.sort(key=lambda d: (d["line"] == 0, -d["severity"], str(d["file"]), d["line"]))
    out.update(ok=ok, diagnostics=diags, spooled=spooled,
               job_log=[m for m in joblog if m["severity"] >= 20
                        or m["type"] in ("Escape", "Diagnostic")])
    return out


def result_text(r: dict) -> str:
    head = "%s: %s" % ("ok" if r["ok"] else "FAILED", r["tool"])
    if r["job"]:
        head += " (job %s%s)" % (r["job"], ", timed out waiting" if r["timed_out"] else
                                 ", end code %s" % r["end_code"] if r["end_code"] is not None
                                 else "")
    lines = [head]
    for c in r["commands"]:
        lines.append("%s %s" % ("ran" if c["ok"] else "FAILED", c["command"]))
        for m in c["messages"]:
            if (m.get("severity") or 0) >= 10 or not c["ok"]:
                lines.append("  %s (severity %s) %s" % (m.get("id") or "-", m.get("severity"),
                                                        m.get("text")))
    if r.get("note"):
        lines.append("note: " + r["note"].strip())
    errs = r["diagnostics"]
    if errs:
        lines.append("errors from the listings, most severe first:")
        for d in errs[:100]:
            where = "%s:%d" % (d["file"] or "?", d["line"]) if d["line"] else (d["file"] or "?")
            if d["line"] and d.get("column"):
                where += ":%d" % d["column"]
            lines.append("  %s: %s %d %s %s" % (where, d["severity_name"], d["severity"],
                                                d["id"], d["text"]))
    if r["job_log"]:
        lines.append("job log:")
        lines += ["  %s (%s, severity %d) %s" % (m["id"] or "-", m["type"].lower(),
                                                 m["severity"], m["text"]) for m in r["job_log"]]
    if r["spooled"]:
        lines.append("read: " + ", ".join("%s %s (%s)" % (s["file"], s["number"], s["kind"])
                                          for s in r["spooled"]))
    return "\n".join(lines)
