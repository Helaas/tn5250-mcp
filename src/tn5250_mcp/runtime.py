"""Why a program call failed, at which source line.

An unmonitored error in a program called through the host servers ends with
"CEE9901 Application error. RNX9998 unmonitored", which says nothing: the
RPG runtime couldn't send its inquiry (RNQxxxx) in a server job. The real
error is earlier in the job log: e.g. MCH1211 (divide by zero) from the
program, with its module, procedure and statement number. This finds that
message, the RPG status code (from the RNQ message the runtime tried to
send), and maps the statement to a line of the module's source: for ILE RPG
the statement number is the line in the compile listing, which is the
source with its /COPY and /INCLUDE members expanded.
"""

from __future__ import annotations

import posixpath
import re

from .hostservers import HostServerError

_UNMON = re.compile(r"unmonitored by (?P<pgm>[A-Z0-9_$#@]+) at statement (?P<stmt>\d+|\*N)")
_RNQ = re.compile(r"\b(RN[QX]\d{4})\b")
_COPY = re.compile(r"^.{0,7}?/(?:copy|include)\s+(?P<ref>\S+)", re.I)
SYSTEM_LIBS = ("QSYS", "QSYS2", "QDEVTOOLS", "QTCP", "QHTTPSVR", "QSQL")


def _joblog_since_request(hs, name: str, job: str) -> list[dict]:
    """The job's messages from its last client request (CPF5C61 for a program
    call, CPF5C62 for a command) on."""
    res = hs.helper.call("query", name=name, params=[job], max_rows=80, sql=(
        "SELECT MESSAGE_ID, MESSAGE_TYPE, SEVERITY, VARCHAR(MESSAGE_TEXT, 1000), FROM_LIBRARY, "
        "FROM_PROGRAM, FROM_MODULE, FROM_PROCEDURE, FROM_INSTRUCTION, TO_PROGRAM "
        "FROM TABLE(QSYS2.JOBLOG_INFO(CAST(? AS VARCHAR(28)))) X "
        "ORDER BY ORDINAL_POSITION DESC FETCH FIRST 80 ROWS ONLY"))
    out = []
    for r in res["rows"]:
        if r[0] in ("CPF5C61", "CPF5C62"):     # client request: run program / run command
            break
        out.append({"id": r[0], "type": (r[1] or "").lower(), "severity": int(r[2] or 0),
                    "text": r[3] or "", "from_library": r[4], "from_program": r[5],
                    "module": r[6], "procedure": r[7], "statement": r[8], "to_program": r[9]})
    out.reverse()
    return out


def find_error(msgs: list[dict]) -> dict | None:
    """The message that started it, and the RPG status."""
    origin = next((m for m in msgs if m["type"] == "escape" and m["module"]
                   and str(m["statement"] or "").isdigit()
                   and (m["from_library"] or "") not in SYSTEM_LIBS), None)
    unmon = next((m for m in msgs if _UNMON.search(m["text"])), None)
    if origin is None and unmon is None:
        esc = [m for m in msgs if m["type"] == "escape"]
        if not esc:
            return None
        origin = esc[0]
    err = dict(origin or unmon)
    if origin is None:                    # only the "unmonitored by X at statement N" text
        u = _UNMON.search(unmon["text"])
        err.update(from_program=u.group("pgm"), statement=u.group("stmt"), module=None)
    found = []
    for m in msgs:
        for hit in _RNQ.findall(m["text"]) + ([m["id"]] if m["id"] else []):
            if hit.startswith(("RNQ", "RNX")) and hit != "RNX9998" and hit not in found:
                found.append(hit)
    # 00202/00211/00231: "the call ended in error", from the caller; a specific one wins
    rnq = next((h for h in found if h[3:] not in ("0202", "0211", "0231")),
               found[0] if found else None)
    stmt = str(err.get("statement") or "")
    return {"id": err["id"], "text": err["text"], "library": err.get("from_library"),
            "program": err.get("from_program"), "module": err.get("module"),
            "procedure": err.get("procedure"),
            "statement": int(stmt) if stmt.isdigit() else None,
            "rpg_message": rnq, "rpg_status": "0" + rnq[3:] if rnq else None}


# ----------------------------------------------------------------- statement -> line
def _read_member(hs, name, lib, file, mbr) -> list[str]:
    return [t for _, _, t in hs.helper.call("read_source", name=name, lib=lib, file=file,
                                           member=mbr)["lines"]]


def _read_stream(hs, name, path) -> list[str]:
    return hs.helper.call("ifs_read", name=name, path=path, binary=False,
                          max_bytes=2_000_000)["text"].splitlines()


def _resolve(ref: str, src: dict) -> dict | None:
    ref = ref.strip("'\"")
    if src["kind"] == "stream" or "/" in ref and "," not in ref:
        if not ref.startswith("/"):
            if src["kind"] != "stream":
                return None
            ref = posixpath.normpath(posixpath.join(posixpath.dirname(src["path"]), ref))
        return {"kind": "stream", "path": ref, "label": ref}
    lib, file, mbr = src.get("lib"), "QRPGLESRC", ref
    if "," in ref:
        lf, mbr = ref.split(",", 1)
        lib, _, file = lf.rpartition("/")
        lib = lib or src.get("lib")
        file = file or "QRPGLESRC"
    lib, file, mbr = (x.upper() for x in (lib, file, mbr))
    return {"kind": "member", "lib": lib, "file": file, "member": mbr,
            "label": "%s/%s(%s)" % (lib, file, mbr)}


def _lines(hs, name, src) -> list[str]:
    if src["kind"] == "stream":
        return _read_stream(hs, name, src["path"])
    return _read_member(hs, name, src["lib"], src["file"], src["member"])


def expand(hs, name: str, src: dict, depth: int = 0) -> list[tuple[str, int, str]]:
    """The source as the compiler lists it: [(file, line, text)], copies inline."""
    out = []
    for i, text in enumerate(_lines(hs, name, src), 1):
        out.append((src["label"], i, text))
        m = _COPY.match(text)
        if m and not text.lstrip().startswith("//") and depth < 8:
            inc = _resolve(m.group("ref"), src)
            if inc is None:
                raise HostServerError("can't find %s" % m.group("ref"))
            out += expand(hs, name, inc, depth + 1)
    return out


def map_statement(hs, name: str, lib: str, pgm: str, module: str | None,
                  statement: int) -> dict:
    """{source, file, line, text} for a statement of a program's module, as
    far as it can be told; {note} otherwise."""
    rows = hs.helper.call("query", name=name, params=[lib, pgm], max_rows=500, sql=(
        "SELECT BOUND_MODULE, MODULE_ATTRIBUTE, SOURCE_FILE_LIBRARY, SOURCE_FILE, "
        "SOURCE_FILE_MEMBER, SOURCE_STREAM_FILE_PATH FROM QSYS2.BOUND_MODULE_INFO "
        "WHERE PROGRAM_LIBRARY = ? AND PROGRAM_NAME = ?"))["rows"]
    row = next((r for r in rows if not module or r[0] == module), None)
    if not row:
        return {"note": "no module information for %s/%s" % (lib, pgm)}
    attr = (row[1] or "").upper()
    if row[5]:
        src = {"kind": "stream", "path": row[5], "label": row[5]}
    elif row[3]:
        src = {"kind": "member", "lib": row[2], "file": row[3], "member": row[4],
               "label": "%s/%s(%s)" % (row[2], row[3], row[4])}
    else:
        return {"note": "the module has no source information"}
    out = {"source": src["label"], "module_attribute": attr}
    if attr.startswith("SQL"):
        out["note"] = ("statement %d is a line of the SQL precompiler's output, not of the "
                       "source; look for the code near it" % statement)
        return out
    if attr.startswith("CL"):                 # CL statement numbers are sequence numbers
        if statement % 100 == 0:
            line = statement // 100
            try:
                text = _lines(hs, name, src)[line - 1]
            except (HostServerError, IndexError):
                text = None
            out.update(file=src["label"], line=line, source_line=text)
        else:
            out["note"] = "statement %d (a source sequence number)" % statement
        return out
    try:
        listing = expand(hs, name, src)
    except HostServerError as e:
        out["note"] = "can't read the source to find the line: %s" % e
        return out
    if 1 <= statement <= len(listing):
        f, ln, text = listing[statement - 1]
        out.update(file=f, line=ln, source_line=text.rstrip())
    elif statement % 100 == 0:                 # OPTION(*SRCSTMT): sequence numbers
        out.update(file=src["label"], line=statement // 100,
                   note="statement taken as a sequence number (OPTION(*SRCSTMT))")
    else:
        out["note"] = "statement %d is past the end of the source" % statement
    return out


def runtime_error(hs, name: str, job: str) -> dict | None:
    """The error behind a failed call in `job` (the command job), with its
    source line and the RPG status, or None when the job log shows none."""
    try:
        msgs = _joblog_since_request(hs, name, job)
    except HostServerError:
        return None
    err = find_error(msgs)
    if not err:
        return None
    if err["program"] and err["statement"] and err["library"]:
        try:
            err.update(map_statement(hs, name, err["library"], err["program"], err["module"],
                                     err["statement"]))
        except HostServerError as e:
            err["note"] = "can't map the statement: %s" % e
    if err["rpg_message"]:
        try:
            from .objects import explain_message
            err["rpg_text"] = explain_message(hs, name, err["rpg_message"])["text"]
        except HostServerError:
            pass
    err["job_log"] = [{k: m[k] for k in ("id", "type", "severity", "text")}
                      for m in msgs if m["type"] in ("escape", "diagnostic")]
    return err


def error_text(err: dict) -> str:
    where = ""
    if err.get("file"):
        where = " at %s:%s" % (err["file"], err["line"])
    elif err.get("statement"):
        where = " at statement %s" % err["statement"]
    who = "/".join(x for x in (err.get("library"), err.get("program")) if x)
    if err.get("procedure") and err.get("procedure") != err.get("program"):
        who += " procedure %s" % err["procedure"]
    lines = ["error %s%s in %s: %s" % (err["id"], where, who or "?", err["text"])]
    if err.get("rpg_status"):
        lines.append("  RPG status %s (%s)%s" % (err["rpg_status"], err["rpg_message"],
                                                 ": " + err["rpg_text"] if err.get("rpg_text")
                                                 else ""))
    if err.get("source_line") is not None:
        lines.append("  %d | %s" % (err["line"], err["source_line"].strip()))
    if err.get("note"):
        lines.append("  note: " + err["note"])
    return "\n".join(lines)
