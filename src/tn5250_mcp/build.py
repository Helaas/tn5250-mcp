"""Build an IBM i project from a local folder: upload the sources that changed
to the IFS, then create the objects in dependency order, and only the ones
that need it. Errors come back with local file paths.

tn5250-project.yaml in the project folder:

    project: hello
    lib: MYLIB                       # objects are created here
    libl: [MYLIB, QGPL]              # library list for building (default: [lib])
    ifs: /home/ME/projects/hello     # the folder is mirrored here
    src: src                         # local source folder (default: the project folder)
    profile: pub400                  # for tn5250-run build (optional)
    bnddir: HELLO                    # optional binding directory, gets the service programs
    srvpgms:                         # service programs from modules
      UTILS: {modules: [UTILS], bnd: utils.bnd}      # or export: all
    programs:                        # programs from modules (CRTPGM)
      MAIN: {modules: [MAIN], srvpgms: [UTILS]}
    options: {rpg: "TGTRLS(*CURRENT)"}   # extra compile parameters per type

Source files by name (like IBM's Bob):
    name.pgm.rpgle / .pgm.sqlrpgle / .pgm.clle   program NAME (one source)
    name.rpgle / .sqlrpgle / .clle               module NAME
    name.pf / .lf / .dspf / .prtf                file NAME (DDS)
    name.sql / .table / .view / .index / .sqlprc / .sqludf / .sqltrg   run with RUNSQLSTM
    name.bnd                                     binder source (srvpgms: bnd)
    name.rpgleinc / .rpginc / .inc / .cpy / .clinc   include (no object)
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .compiler import KINDS, NAME, build_command
from .hostservers import HostServerError, check_libl, under

PROJECT_FILE = "tn5250-project.yaml"
STATE_FILE = ".tn5250-build.json"
PROJECT_KEYS = {"project", "lib", "libl", "ifs", "src", "profile", "bnddir", "srvpgms",
                "programs", "options"}
INCLUDE_EXT = {".rpgleinc", ".rpginc", ".inc", ".cpy", ".clinc"}
SQL_EXT = {".sql", ".table", ".view", ".index", ".sqlprc", ".sqludf", ".sqltrg", ".sqlseq"}
# (suffix, kind, phase); longer suffixes first. Phases: 0 sql, 1 pf, 2 lf, 3 dspf/prtf,
# 4 modules, 5 service programs, 6 programs
CONVENTIONS = [
    (".pgm.rpgle", "rpg", 6), (".pgm.sqlrpgle", "sqlrpg", 6), (".pgm.clle", "cl", 6),
    (".rpgle", "rpgmod", 4), (".sqlrpgle", "sqlrpgmod", 4), (".clle", "clmod", 4),
    (".pf", "pf", 1), (".lf", "lf", 2), (".dspf", "dspf", 3), (".prtf", "prtf", 3),
]
_COPY = re.compile(r"^\s*(?:/|\*\*)?(?:copy|include)\s+['\"]?([^'\"\s]+)", re.I | re.M)
_DCLF = re.compile(r"^\s*dcl-f\s+(\w+)", re.I | re.M)
_FSPEC = re.compile(r"^.{5}[Ff]([A-Za-z$#@][\w$#@]{0,9})", re.M)
_PFILE = re.compile(r"\b[PJ]FILE\(([^)]*)\)", re.I)
OBJTYPE = {"rpg": "*PGM", "sqlrpg": "*PGM", "cl": "*PGM", "pgm": "*PGM",
           "rpgmod": "*MODULE", "sqlrpgmod": "*MODULE", "clmod": "*MODULE",
           "srvpgm": "*SRVPGM"}
RPG_KINDS = ("rpg", "rpgmod", "sqlrpg", "sqlrpgmod")
FILE_KINDS = ("sql", "pf", "lf", "dspf", "prtf")


class BuildError(ValueError):
    """The project definition is wrong (not: something failed to compile)."""


@dataclass
class Target:
    name: str
    kind: str                         # rpg, rpgmod, ..., sql, srvpgm, pgm
    phase: int
    source: str | None = None         # path relative to the project folder
    modules: list[str] = field(default_factory=list)
    srvpgms: list[str] = field(default_factory=list)
    bnd: str | None = None
    export_all: bool = False

    @property
    def objtype(self) -> str:
        return OBJTYPE.get(self.kind, "*FILE")

    @property
    def key(self) -> str:
        """What the state file and dependencies go by: *PGM:MAIN, *MODULE:MAIN, ..."""
        return "SQL:" + self.source if self.kind == "sql" else self.objtype + ":" + self.name

    def deps(self) -> set[str]:
        return {"*MODULE:" + m for m in self.modules} | {"*SRVPGM:" + s for s in self.srvpgms}


@dataclass
class Project:
    root: Path
    name: str
    lib: str
    libl: list[str]
    ifs: str
    src: Path
    profile: str | None
    bnddir: str | None
    options: dict[str, str]
    targets: dict[str, Target]
    includes: list[str]               # relative paths of include files
    uploads: list[str]                # every file that goes to the IFS

    def remote(self, rel: str) -> str:
        return posixpath.join(self.ifs, rel.replace("\\", "/"))

    def local(self, remote_path: str) -> str:
        if remote_path.startswith(self.ifs.rstrip("/") + "/"):
            return str(self.root / remote_path[len(self.ifs.rstrip("/")) + 1:])
        return remote_path


def _obj(name: str, where: str) -> str:
    n = name.upper()
    if not NAME.match(n):
        raise BuildError("%s: %r is not an IBM i object name (10 characters at most)"
                         % (where, name))
    return n


def load_project(folder: str | Path) -> Project:
    root = Path(folder).resolve()
    f = root / PROJECT_FILE
    try:
        doc = yaml.safe_load(f.read_text("utf-8"))
    except OSError:
        raise BuildError("no %s in %s" % (PROJECT_FILE, root)) from None
    except yaml.YAMLError as e:
        raise BuildError("%s: not valid YAML: %s" % (f, e)) from None
    if not isinstance(doc, dict) or set(doc) - PROJECT_KEYS:
        raise BuildError("%s: keys are %s" % (f, ", ".join(sorted(PROJECT_KEYS))))
    for k in ("lib", "ifs"):
        if not doc.get(k):
            raise BuildError("%s: needs %s" % (f, k))
    lib = _obj(str(doc["lib"]), "lib")
    try:
        libl = check_libl(doc.get("libl") or [lib])
    except HostServerError as e:
        raise BuildError("%s: libl: %s" % (f, e)) from None
    ifs = str(doc["ifs"]).rstrip("/")
    if not ifs.startswith("/") or ".." in ifs.split("/"):
        raise BuildError("%s: ifs is an absolute IFS folder" % f)
    src = (root / str(doc.get("src") or ".")).resolve()
    if root not in (src, *src.parents):
        raise BuildError("%s: src must be inside the project folder" % f)
    targets: dict[str, Target] = {}
    includes, uploads = [], []
    for path in sorted(p for p in src.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        lower = path.name.lower()
        ext = path.suffix.lower()
        if ext in INCLUDE_EXT:
            includes.append(rel)
            uploads.append(rel)
            continue
        if ext == ".bnd":
            uploads.append(rel)
            continue
        if ext in SQL_EXT:
            t = Target(_obj(path.name.split(".")[0], rel), "sql", 0, rel)
        else:
            hit = next(((suf, kind, ph) for suf, kind, ph in CONVENTIONS if lower.endswith(suf)),
                       None)
            if not hit:
                continue
            t = Target(_obj(path.name[:-len(hit[0])].split(".")[0], rel), hit[1], hit[2], rel)
        if t.key in targets:
            raise BuildError("%s and %s would both create %s %s" % (
                targets[t.key].source, rel, t.objtype, t.name))
        targets[t.key] = t
        uploads.append(rel)
    for name, spec in (doc.get("srvpgms") or {}).items():
        n = _obj(str(name), "srvpgms")
        spec = spec or {}
        mods = [_obj(m, "srvpgms.%s" % n) for m in spec.get("modules") or [n]]
        bnd = spec.get("bnd")
        if bnd and not (src / bnd).exists() and not (root / bnd).exists():
            raise BuildError("srvpgms.%s: binder source %s not found" % (n, bnd))
        bnd_rel = None
        if bnd:
            bp = (src / bnd) if (src / bnd).exists() else (root / bnd)
            bnd_rel = bp.resolve().relative_to(root).as_posix()
        t = Target(n, "srvpgm", 5, modules=mods,
                   srvpgms=[_obj(x, n) for x in spec.get("srvpgms") or []],
                   bnd=bnd_rel, export_all=not bnd or spec.get("export") == "all")
        targets[t.key] = t
    for name, spec in (doc.get("programs") or {}).items():
        n = _obj(str(name), "programs")
        spec = spec or {}
        t = Target(n, "pgm", 6, modules=[_obj(m, n) for m in spec.get("modules") or []],
                   srvpgms=[_obj(x, n) for x in spec.get("srvpgms") or []])
        if not t.modules:
            raise BuildError("programs.%s: needs modules" % n)
        if t.key in targets:
            raise BuildError("programs.%s: %s already creates that program"
                             % (n, targets[t.key].source))
        targets[t.key] = t
    if doc.get("bnddir"):
        all_srv = [t.name for t in targets.values() if t.kind == "srvpgm"]
        for t in targets.values():
            if t.kind in ("rpg", "sqlrpg", "cl") and t.phase == 6:
                t.srvpgms = all_srv
    for t in targets.values():
        for m in t.modules:
            if "*MODULE:" + m not in targets:
                raise BuildError("%s uses module %s, but there is no %s.rpgle/.sqlrpgle/.clle"
                                 % (t.name, m, m.lower()))
        for sp in t.srvpgms:
            if "*SRVPGM:" + sp not in targets:
                raise BuildError("%s binds service program %s, which isn't in srvpgms"
                                 % (t.name, sp))
    return Project(root=root, name=str(doc.get("project") or root.name), lib=lib, libl=libl,
                   ifs=ifs, src=src, profile=doc.get("profile"),
                   bnddir=_obj(doc["bnddir"], "bnddir") if doc.get("bnddir") else None,
                   options={str(k): str(v) for k, v in (doc.get("options") or {}).items()},
                   targets=targets, includes=includes, uploads=uploads)


# ----------------------------------------------------------------- what to build
def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stem(ref: str) -> str:
    """utils for 'inc/utils.rpgleinc', 'QRPGLESRC,UTILS' and 'MYLIB/QRPGLESRC,UTILS'."""
    return posixpath.basename(ref).split(",")[-1].split(".")[0].lower()


def _uses(p: Project, t: Target) -> tuple[set[str], set[str]]:
    """Includes (stems) and files (object names) a source refers to."""
    if not t.source:
        return set(), set()
    text = (p.root / t.source).read_text("utf-8", errors="replace")
    incs = {_stem(m.group(1)) for m in _COPY.finditer(text)}
    files = {m.group(1).upper() for m in _DCLF.finditer(text)}
    if t.kind in RPG_KINDS:
        files |= {m.group(1).upper() for m in _FSPEC.finditer(text)}
    if t.kind == "lf":
        files |= {x.split("/")[-1].upper() for m in _PFILE.finditer(text)
                  for x in m.group(1).split()}
    return incs, files


def plan(p: Project, state: dict, rebuild_all: bool = False,
         only: list[str] | None = None) -> tuple[list[str], list[Target]]:
    """Files to upload, and targets to build in order."""
    hashes = {rel: _hash(p.root / rel) for rel in p.uploads}
    old = state.get("uploaded", {})
    upload = [rel for rel in p.uploads if rebuild_all or old.get(rel) != hashes[rel]]
    changed = set(upload)
    built = state.get("built", {})
    dirty: set[str] = set()
    changed_incs = {_stem(r) for r in changed if r in p.includes}
    for key, t in p.targets.items():
        if rebuild_all or key not in built:
            dirty.add(key)
        elif t.source and t.source in changed:
            dirty.add(key)
        elif t.bnd and t.bnd in changed:
            dirty.add(key)
        elif t.source and _uses(p, t)[0] & changed_incs:
            dirty.add(key)
    # follow dependencies: files -> sources that use them; modules -> srvpgms/programs
    uses = {k: _uses(p, t)[1] for k, t in p.targets.items()}
    while True:
        files = {p.targets[k].name for k in dirty if p.targets[k].kind in FILE_KINDS}
        more = {k for k, t in p.targets.items() if k not in dirty and (
            t.deps() & dirty or uses[k] & files)}
        if not more:
            break
        dirty |= more
    if only:
        want = {o.upper() for o in only}
        dirty = {k for k in dirty if p.targets[k].name in want}
    order = sorted((p.targets[k] for k in dirty), key=lambda t: (t.phase, t.name, t.key))
    return upload, order


# ----------------------------------------------------------------- building
def target_command(p: Project, t: Target) -> str:
    lib = p.lib
    if t.kind == "sql":                  # a listing only when there are errors
        return ("RUNSQLSTM SRCSTMF('%s') COMMIT(*NONE) NAMING(*SYS) DFTRDBCOL(%s) "
                "OPTION(*ERRLIST)" % (p.remote(t.source), lib))
    if t.kind == "srvpgm":
        cmd = "CRTSRVPGM SRVPGM(%s/%s) MODULE(%s)" % (lib, t.name,
                                                    " ".join("%s/%s" % (lib, m) for m in t.modules))
        cmd += (" EXPORT(*ALL)" if t.export_all
                else " EXPORT(*SRCFILE) SRCSTMF('%s')" % p.remote(t.bnd))
        if t.srvpgms:
            cmd += " BNDSRVPGM(%s)" % " ".join("(%s/%s)" % (lib, s) for s in t.srvpgms)
        return cmd + " REPLACE(*YES)"
    if t.kind == "pgm":
        cmd = "CRTPGM PGM(%s/%s) MODULE(%s)" % (lib, t.name,
                                              " ".join("%s/%s" % (lib, m) for m in t.modules))
        if t.srvpgms:
            cmd += " BNDSRVPGM(%s)" % " ".join("(%s/%s)" % (lib, s) for s in t.srvpgms)
        return cmd + " REPLACE(*YES)"
    return build_command(compile_source(p, t), "%s/%s" % (lib, t.name), t.kind,
                         compile_options(p, t), listing=False)


DDS_KINDS = ("pf", "lf", "dspf", "prtf")
DDS_FILE = "QTEMP/QDDSSRC"


def compile_source(p: Project, t: Target) -> str:
    """The IFS file, or for DDS a member in QTEMP: CRTPF, CRTLF, CRTDSPF and
    CRTPRTF don't read stream files (CPD0043 on IBM i 7.5), so the source is
    copied into a member of the build's own job first."""
    if t.kind in DDS_KINDS:
        return "%s(%s)" % (DDS_FILE, t.name)
    return p.remote(t.source)


def compile_options(p: Project, t: Target) -> str:
    """The project's options for this type, plus the binding directory for
    programs and the IFS root as include folder for RPG."""
    opts = p.options.get(t.kind, "")
    if p.bnddir and t.kind in ("rpg", "sqlrpg", "cl") and not re.search(r"\bBNDDIR\(", opts, re.I):
        opts += " BNDDIR(%s/%s)" % (p.lib, p.bnddir)
        # the command's default DFTACTGRP(*YES) refuses BNDDIR before the source's
        # ctl-opt is read (RNF9302)
        if t.kind != "sqlrpg" and not re.search(r"\bDFTACTGRP\(", opts, re.I):
            opts += " DFTACTGRP(*NO)"
    if t.kind in RPG_KINDS and p.includes and not re.search(r"\bINCDIR\(", opts, re.I):
        opts += " INCDIR('%s')" % p.ifs
    return opts.strip()


_SQLMSG = re.compile(r"^([A-Z]{2,3}[0-9A-F]{4,5})\s+(\d+)\s+(\d+)\s+(.*)$")


def sql_errors(hs, name: str, t: Target, path: str) -> list[dict]:
    """RUNSQLSTM's errors from its listing (its spooled file, named after the
    source, in the user's QPRTJOB job), which is then deleted."""
    want = t.name[:10]
    try:
        rows = hs.spooled_files(name, last=10).get("rows", [])
        row = next((r for r in rows if str(r[0]).upper() == want
                    and str(r[2]).upper().endswith("/QPRTJOB")), None)
        if not row:
            return []
        lines = hs.spooled_file(name, row[2], row[0], str(row[1]))["lines"]
        hs._run_command(name, "DLTSPLF FILE(%s) JOB(%s) SPLNBR(%s)" % (row[0], row[2], row[1]),
                        None)
    except HostServerError:
        return []
    out = []
    for line in lines:
        m = _SQLMSG.match(line.strip())
        if not m:
            continue
        text = m.group(4).strip()
        pos = re.match(r"Position (\d+)\s+(.*)", text)
        sev = int(m.group(2))
        out.append({"file": path, "line": int(m.group(3)), "column": int(pos.group(1)) if pos
                    else 1, "severity": sev,
                    "severity_name": "info" if sev < 10 else "warning" if sev < 20 else
                    "error" if sev < 30 else "severe" if sev < 40 else "terminal",
                    "id": m.group(1), "text": pos.group(2) if pos else text})
    return out


def load_state(p: Project) -> dict:
    try:
        return json.loads((p.root / STATE_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(p: Project, state: dict) -> None:
    (p.root / STATE_FILE).write_text(json.dumps(state, indent=1, sort_keys=True) + "\n",
                                     encoding="utf-8")


def build(hs, name: str, p: Project, rebuild_all: bool = False,
          only: list[str] | None = None, say=lambda m: None) -> dict:
    """Upload and build. Returns a report: uploaded files, and per object its
    status (built, failed, skipped) with errors in local paths."""
    if not hs.compile_libs or ("*" not in hs.compile_libs and p.lib not in hs.compile_libs):
        raise HostServerError("this server may not create objects in %s (--allow-compile)"
                              % p.lib)
    if not any(under(p.ifs, r) for r in hs.ifs_write_roots):
        raise HostServerError("%s is outside the folders this server may write to: %s "
                              "(--ifs-write-root)" % (p.ifs, ", ".join(hs.ifs_write_roots)
                                                      or "none"))
    state = load_state(p)
    if state.get("ifs") not in (None, p.ifs) or state.get("lib") not in (None, p.lib):
        state = {}                       # built somewhere else before: start over
    upload, todo = plan(p, state, rebuild_all, only)
    if any(t.kind == "sql" for t in todo) and not hs.allow_sql_write:
        raise HostServerError("the project has SQL sources (RUNSQLSTM): that needs "
                              "--allow-sql-write")
    for t in todo:
        if t.kind == "sql":
            hs._check_sql((p.root / t.source).read_text("utf-8"))     # the block list
    made_dirs = set()
    for rel in upload:
        d = posixpath.dirname(p.remote(rel))
        if d not in made_dirs:
            hs.ifs_mkdir(name, d)
            made_dirs.add(d)
        hs.ifs_write(name, p.remote(rel), (p.root / rel).read_text("utf-8"), overwrite=True,
                     ccsid=1208)
        state.setdefault("uploaded", {})[rel] = _hash(p.root / rel)
        say("uploaded %s" % rel)
    state.update(ifs=p.ifs, lib=p.lib)
    save_state(p, state)                 # uploads count even if a compile fails below
    if p.bnddir and any(t.kind == "srvpgm" for t in todo):
        hs._run_command(name, "CRTBNDDIR BNDDIR(%s/%s)" % (p.lib, p.bnddir), p.libl)
    built = state.get("built", {})
    for t in sorted((t for t in todo if t.kind in ("pf", "lf") and t.key in built),
                    key=lambda t: t.kind != "lf"):
        say("deleting %s/%s to create it again" % (p.lib, t.name))
        hs._run_command(name, "DLTF FILE(%s/%s)" % (p.lib, t.name), p.libl)
    results, failed, failed_files = [], set(), set()
    uses = {t.key: _uses(p, t)[1] for t in todo}
    dds_ready = False
    for t in todo:
        need = {k.split(":", 1)[1] for k in t.deps() & failed} | (uses[t.key] & failed_files)
        if need:
            results.append({"object": t.name, "kind": t.kind, "status": "skipped",
                            "reason": "needs %s, which failed" % ", ".join(sorted(need))})
            failed.add(t.key)
            continue
        cmd = target_command(p, t)
        say("building %s (%s)" % (t.name, t.kind))
        if t.kind in DDS_KINDS:
            if not dds_ready:
                hs._run_command(name, "CRTSRCPF FILE(%s) RCDLEN(112)" % DDS_FILE, None)
                dds_ready = True
            lib_, file_ = DDS_FILE.split("/")
            cp = hs._run_command(name, "CPYFRMSTMF FROMSTMF('%s') TOMBR('/QSYS.LIB/%s.LIB/"
                                 "%s.FILE/%s.MBR') MBROPT(*REPLACE)" % (
                                     p.remote(t.source), lib_, file_, t.name), None)
            if not cp.get("ok"):
                res = cp
        if t.kind in KINDS and not (t.kind in DDS_KINDS and not cp.get("ok")):
            res = hs.compile(name, compile_source(p, t), "%s/%s" % (p.lib, t.name), t.kind,
                             compile_options(p, t), p.libl, listing=False)
            ok, diags, msgs = res["ok"], res["diagnostics"], res["messages"]
            if t.kind in DDS_KINDS:              # the member stands for the local file
                for d in diags:
                    if d["file"].upper().startswith(DDS_FILE):
                        d["file"] = p.remote(t.source)
        elif t.kind in DDS_KINDS:
            ok, diags, msgs = False, [], res.get("messages", [])
        else:
            res = hs._run_command(name, hs._guard_command(cmd), p.libl)
            ok, diags, msgs = bool(res.get("ok")), [], res.get("messages", [])
            if t.kind == "sql" and not ok:
                diags = sql_errors(hs, name, t, p.remote(t.source))
        for d in diags:
            d["file"] = p.local(d["file"])
        entry = {"object": t.name, "kind": t.kind, "status": "built" if ok else "failed",
                 "command": cmd, "source": str(p.root / t.source) if t.source else None,
                 "diagnostics": diags,
                 "messages": [m for m in msgs if (m.get("severity") or 0) >= 10]}
        results.append(entry)
        key = t.key
        if ok:
            state.setdefault("built", {})[key] = _hash(p.root / t.source) if t.source else "ok"
            if t.kind == "srvpgm" and p.bnddir:
                hs._run_command(name, "ADDBNDDIRE BNDDIR(%s/%s) OBJ((%s/%s *SRVPGM))"
                                % (p.lib, p.bnddir, p.lib, t.name), p.libl)
        else:
            state.get("built", {}).pop(key, None)
            failed.add(t.key)
            if t.kind in FILE_KINDS:
                failed_files.add(t.name)
        save_state(p, state)
    return {"project": p.name, "lib": p.lib, "ifs": p.ifs, "uploaded": upload,
            "objects": results, "ok": not failed,
            "up_to_date": not upload and not todo}


def report_text(r: dict) -> str:
    lines = []
    if r["up_to_date"]:
        return "%s: everything is up to date" % r["project"]
    if r["uploaded"]:
        lines.append("uploaded %d file%s to %s" % (len(r["uploaded"]),
                                                   "" if len(r["uploaded"]) == 1 else "s",
                                                   r["ifs"]))
    for o in r["objects"]:
        lines.append("%-7s %s/%s (%s)%s" % (o["status"].upper(), r["lib"], o["object"],
                                           o["kind"], ": " + o["reason"] if o.get("reason")
                                           else ""))
        for d in o.get("diagnostics", []):
            if d["line"] > 0 and (o["status"] == "failed" or d["severity"] >= 10):
                lines.append("  %s:%d:%d: %s %d %s %s" % (d["file"], d["line"], d["column"],
                                                         d["severity_name"], d["severity"],
                                                         d["id"], d["text"]))
        if o["status"] == "failed" and not any(d["line"] > 0 for d in o.get("diagnostics", [])):
            for m in o.get("messages", [])[:5]:
                lines.append("  %s (severity %s) %s" % (m.get("id"), m.get("severity"),
                                                        m.get("text")))
    lines.append("%s: %s" % (r["project"], "built" if r["ok"] else "FAILED"))
    return "\n".join(lines)
