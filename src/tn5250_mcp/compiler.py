"""Compiling IBM i sources and turning the compiler's event file into a list of
errors an agent (or a person) can act on: file, line, column, severity,
message ID and text.

The compile command gets OPTION(*EVENTF); the compiler then writes its
diagnostics to member <object> of file EVFEVENT in the object's library, in
the format IBM's tools (and Code for IBM i) read.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

NAME = re.compile(r"^[A-Z$#@][A-Z0-9_$#@.]{0,9}$")

# type -> (command, object keyword, extra parameters, accepts DBGVIEW)
KINDS = {
    "rpg":       ("CRTBNDRPG",  "PGM",    "", True),
    "sqlrpg":    ("CRTSQLRPGI", "OBJ",    "OBJTYPE(*PGM)", True),
    "rpgmod":    ("CRTRPGMOD",  "MODULE", "", True),
    "sqlrpgmod": ("CRTSQLRPGI", "OBJ",    "OBJTYPE(*MODULE)", True),
    "cl":        ("CRTBNDCL",   "PGM",    "", True),
    "clmod":     ("CRTCLMOD",   "MODULE", "", True),
    "dspf":      ("CRTDSPF",    "FILE",   "", False),
    "prtf":      ("CRTPRTF",    "FILE",   "", False),
    "pf":        ("CRTPF",      "FILE",   "", False),
    "lf":        ("CRTLF",      "FILE",   "", False),
}
# file extension (as Code for IBM i names them) -> type
EXTENSIONS = {
    ".rpgle": "rpg", ".sqlrpgle": "sqlrpg", ".clle": "cl", ".clp": "cl",
    ".dspf": "dspf", ".prtf": "prtf", ".pf": "pf", ".lf": "lf",
}
# member source type (SRCTYPE) -> type
MEMBER_TYPES = {"RPGLE": "rpg", "SQLRPGLE": "sqlrpg", "CLLE": "cl", "CLP": "cl",
                "DSPF": "dspf", "PRTF": "prtf", "PF": "pf", "LF": "lf"}
SEVERITY = {"I": "info", "W": "warning", "E": "error", "S": "severe", "T": "terminal",
            "U": "unrecoverable"}


class CompileError(ValueError):
    """The compile request itself is wrong (not: the source has errors)."""


def split_object(obj: str) -> tuple[str, str]:
    lib, _, name = obj.strip().upper().partition("/")
    if not name or not NAME.match(lib) or not NAME.match(name):
        raise CompileError("object is LIB/NAME, e.g. MYLIB/HELLO (got %r)" % obj)
    return lib, name


def source_clause(source: str) -> str:
    """SRCSTMF('/path') for an IFS stream file, or SRCFILE(LIB/FILE)
    SRCMBR(MBR) for a source member written LIB/FILE(MBR)."""
    s = source.strip()
    if s.startswith("/"):
        if "'" in s or ".." in s.split("/"):
            raise CompileError("bad source path %r" % source)
        return "SRCSTMF('%s')" % posixpath.normpath(s)
    m = re.fullmatch(r"([A-Za-z0-9_$#@.]{1,10})/([A-Za-z0-9_$#@.]{1,10})\(([A-Za-z0-9_$#@.]{1,10})\)",
                     s)
    if not m:
        raise CompileError("source is an IFS path (/home/me/hello.rpgle) or a member "
                           "LIB/FILE(MEMBER) (got %r)" % source)
    lib, file, mbr = (x.upper() for x in m.groups())
    return "SRCFILE(%s/%s) SRCMBR(%s)" % (lib, file, mbr)


def kind_for(source: str, kind: str | None) -> str:
    if kind:
        if kind not in KINDS:
            raise CompileError("type is one of %s" % ", ".join(KINDS))
        return kind
    ext = posixpath.splitext(source.strip().lower())[1]
    if ext in EXTENSIONS:
        return EXTENSIONS[ext]
    raise CompileError("can't tell the source type from %r (by its extension, or a "
                       "member's source type); pass type (%s)"
                       % (source, ", ".join(KINDS)))


def build_command(source: str, obj: str, kind: str | None = None, options: str = "",
                  listing: bool = True) -> str:
    """listing=False: no compile listing (spooled file) unless there are errors;
    the event file has the errors either way."""
    k = kind_for(source, kind)
    cmd, kw, extra, dbg = KINDS[k]
    lib, name = split_object(obj)
    if options and re.search(r"\bOPTION\s*\(", options, re.I):
        raise CompileError("options may not set OPTION: *EVENTF is needed for the errors")
    dds = cmd in ("CRTDSPF", "CRTPRTF", "CRTPF", "CRTLF")
    parts = [cmd, "%s(%s/%s)" % (kw, lib, name), source_clause(source),
             "OPTION(*EVENTF *NOSRC *NOLIST)" if dds and not listing else "OPTION(*EVENTF)"]
    if extra:
        parts.append(extra)
    # stream files are usually UTF-8 (1208), which RPG can't compile with its default
    # TGTCCSID(*SRC) (RNS9380): compile in the job's CCSID, as Code for IBM i does
    if source.strip().startswith("/"):
        if cmd in ("CRTBNDRPG", "CRTRPGMOD") and not re.search(r"\bTGTCCSID\s*\(", options or "",
                                                               re.I):
            parts.append("TGTCCSID(*JOB)")
        if cmd == "CRTSQLRPGI" and not re.search(r"\bCVTCCSID\s*\(", options or "", re.I):
            parts.append("CVTCCSID(*JOB)")
    if dbg and not re.search(r"\bDBGVIEW\s*\(", options or "", re.I):
        parts.append("DBGVIEW(*SOURCE)")
    if not dds and not listing and not re.search(r"\bOUTPUT\s*\(", options or "", re.I):
        parts.append("OUTPUT(*NONE)")
    if options:
        parts.append(options.strip())
    return " ".join(parts)


@dataclass
class Diagnostic:
    file: str
    line: int
    column: int
    severity: int
    severity_name: str
    id: str
    text: str

    def __str__(self) -> str:
        return "%s:%d:%d: %s %d %s %s" % (self.file, self.line, self.column,
                                           self.severity_name, self.severity, self.id, self.text)


def parse_events(records: list[str]) -> list[Diagnostic]:
    """ERROR records of an event file, with the file names from FILEID."""
    files: dict[str, str] = {}
    out: list[Diagnostic] = []
    for rec in records:
        parts = rec.split()
        if not parts:
            continue
        if parts[0] == "FILEID" and len(parts) >= 6:
            files[parts[2]] = parts[5]
        elif parts[0] == "ERROR" and len(parts) >= 13:
            f = rec.split(None, 13)
            try:
                d = Diagnostic(file=files.get(f[2], f[2]), line=int(f[5]), column=int(f[6]),
                               severity=int(f[11]), severity_name=SEVERITY.get(f[10], f[10]),
                               id=f[9], text=(f[13] if len(f) > 13 else "").strip())
            except (ValueError, IndexError):
                continue
            out.append(d)
    seen = set()
    unique = []
    # real errors first (most severe, then by place); the compiler's summary (line 0) last
    for d in sorted(out, key=lambda d: (d.line == 0, -d.severity, d.file, d.line, d.column)):
        k = (d.file, d.line, d.column, d.id)
        if k not in seen:
            seen.add(k)
            unique.append(d)
    return unique
