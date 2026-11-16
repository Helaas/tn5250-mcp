"""What an object looks like, for writing code against it: a file's record
formats and fields (with RPG types) and keys, a program's or service
program's modules, sources and exports, a binding directory's entries, a
message file's messages, a data area's value; and what a message ID means.

Files come from DSPFFD/DSPFD outfiles (in the helper's QTEMP, see the Java
`outfile` op), the rest from the QSYS2 catalog services.
"""

from __future__ import annotations

import re

from .hostservers import HostServerError

NAME = re.compile(r"^[A-Z$#@][A-Z0-9_$#@.]{0,9}$")
TYPES = ("*FILE", "*PGM", "*SRVPGM", "*BNDDIR", "*MSGF", "*DTAARA", "*MODULE")
# message ID prefix -> message file, so a lookup reads one file (about a second)
# instead of every message file on the system (20 seconds and more)
MESSAGE_FILES = [
    (("RNF", "RNS"), "QDEVTOOLS", "QRPGLEMSG"),
    (("RNX", "RNQ"), "QSYS", "QRNXMSG"),
    (("SQL", "SQ"), "QSYS", "QSQLMSG"),
    (("CEE",), "QSYS", "QCEEMSG"),
    (("CP", "MCH", "CPD", "CPF", "CPA", "CPC", "CPI", "CPX", "TCP"), "QSYS", "QCPFMSG"),
]
_cache: dict[tuple, dict] = {}


def split_object(obj: str) -> tuple[str, str]:
    lib, _, nm = obj.strip().upper().rpartition("/")
    lib = lib or "*LIBL"
    if not NAME.match(nm) or not (lib == "*LIBL" or NAME.match(lib)):
        raise HostServerError("object is NAME or LIB/NAME, e.g. MYLIB/CUSTMAST (got %r)" % obj)
    return lib, nm


def find(hs, name: str, obj: str, objtype: str | None = None) -> list[dict]:
    lib, nm = split_object(obj)
    t = (objtype or "*ALL").upper()
    if t != "*ALL" and not t.startswith("*"):
        t = "*" + t
    res = hs.query(name, "SELECT OBJNAME, OBJTYPE, OBJATTRIBUTE, OBJTEXT, OBJLONGSCHEMA "
                   "FROM TABLE(QSYS2.OBJECT_STATISTICS(?, ?, ?)) X", [lib, t, nm], max_rows=20)
    return [{"name": r[0], "type": r[1], "attribute": r[2] or "", "text": r[3] or "",
             "lib": r[4]} for r in res["rows"]]


# ----------------------------------------------------------------- files
def rpg_type(f: dict) -> str:
    t, b, d, p = f["WHFLDT"], int(f["WHFLDB"] or 0), int(f["WHFLDD"] or 0), int(f["WHFLDP"] or 0)
    var = f.get("WHVARL") == "Y"
    if t == "A":
        return "varchar(%d)" % (b - 2) if var else "char(%d)" % b
    if t == "P":
        return "packed(%d:%d)" % (d, p)
    if t in ("S", "Y"):
        return "zoned(%d:%d)" % (d, p)
    if t == "B":
        return "bindec(%d:%d)" % (d, p)
    if t == "F":
        return "float(%d)" % b
    if t == "L":
        return "date(%s)" % (f.get("WHFMT") or "*ISO")
    if t == "T":
        return "time(%s)" % (f.get("WHFMT") or "*ISO")
    if t == "Z":
        return "timestamp"
    if t == "G":
        return "vargraph(%d)" % ((b - 2) // 2) if var else "graph(%d)" % (b // 2)
    if t == "H":
        return "char(%d) hex" % b
    if t in ("O", "J", "E"):
        return "char(%d) dbcs" % b
    return "%s(%d)" % (t, b)


def _rows(res: dict) -> list[dict]:
    return [dict(zip(res["columns"], r)) for r in res["rows"]]


def describe_file(hs, name: str, lib: str, file: str, attribute: str = "") -> dict:
    ffd = _rows(hs.helper.call("outfile", name=name, command="DSPFFD FILE(%s/%s)" % (lib, file)))
    formats: dict[str, dict] = {}
    for f in ffd:
        fmt = formats.setdefault(f["WHNAME"], {"name": f["WHNAME"], "text": f["WHTEXT"] or "",
                                               "length": int(f["WHRLEN"] or 0), "fields": []})
        heading = " ".join(x for x in (f.get("WHCHD1"), f.get("WHCHD2"), f.get("WHCHD3")) if x)
        field = {"name": f["WHFLDE"], "type": rpg_type(f), "dds": f["WHFLDT"],
                 "length": int(f["WHFLDB"] or 0), "digits": int(f["WHFLDD"] or 0),
                 "decimals": int(f["WHFLDP"] or 0), "text": f.get("WHFTXT") or "",
                 "heading": heading if heading != f["WHFLDE"] else "",
                 "alias": f.get("WHALIS") or "", "null": f.get("WHNULL") == "Y",
                 "usage": f.get("WHFIOB") or ""}
        if f["WHFTYP"] == "D" and f.get("WHDROW") not in (None, "", "0"):
            field["row"], field["col"] = int(f["WHDROW"]), int(f["WHDCOL"])
        fmt["fields"].append(field)
    kind = {"P": "physical file", "L": "logical file", "D": "display file",
            "R": "printer file"}.get(ffd[0]["WHFTYP"] if ffd else "", "file")
    out = {"object": "%s/%s" % (lib, file), "type": "*FILE", "attribute": attribute,
           "kind": kind, "formats": list(formats.values()), "keys": [], "unique": False}
    if ffd and ffd[0]["WHFTYP"] in ("P", "L"):
        try:
            acc = _rows(hs.helper.call("outfile", name=name,
                                       command="DSPFD FILE(%s/%s) TYPE(*ACCPTH)" % (lib, file)))
        except HostServerError:
            acc = []
        keys = sorted((a for a in acc if a.get("APKEYF")), key=lambda a: int(a["APKEYN"] or 0))
        out["keys"] = [{"field": a["APKEYF"], "descending": a.get("APKSEQ") == "D"}
                       for a in keys]
        out["unique"] = any(a.get("APUNIQ") == "Y" for a in acc)
    return out


# ----------------------------------------------------------------- programs
def describe_program(hs, name: str, lib: str, pgm: str, objtype: str) -> dict:
    mods = hs.query(name, "SELECT BOUND_MODULE_LIBRARY, BOUND_MODULE, MODULE_ATTRIBUTE, "
                    "SOURCE_FILE_LIBRARY, SOURCE_FILE, SOURCE_FILE_MEMBER, "
                    "SOURCE_STREAM_FILE_PATH, SOURCE_CHANGE_TIMESTAMP, DEBUG_DATA "
                    "FROM QSYS2.BOUND_MODULE_INFO WHERE PROGRAM_LIBRARY = ? AND "
                    "PROGRAM_NAME = ? AND OBJECT_TYPE = ?", [lib, pgm, objtype], max_rows=500)
    modules = []
    for r in mods["rows"]:
        src = r[6] or ("%s/%s(%s)" % (r[3], r[4], r[5]) if r[4] else None)
        modules.append({"module": r[1], "attribute": r[2], "source": src,
                        "source_changed": r[7], "debug_data": r[8]})
    out = {"object": "%s/%s" % (lib, pgm), "type": objtype, "modules": modules,
           "exports": [], "service_programs": []}
    try:
        srv = hs.query(name, "SELECT BOUND_SERVICE_PROGRAM_LIBRARY, BOUND_SERVICE_PROGRAM "
                       "FROM QSYS2.BOUND_SRVPGM_INFO WHERE PROGRAM_LIBRARY = ? AND "
                       "PROGRAM_NAME = ? AND OBJECT_TYPE = ?", [lib, pgm, objtype], max_rows=500)
        out["service_programs"] = ["%s/%s" % (r[0], r[1]) for r in srv["rows"]
                                   if r[0] not in ("QSYS",)]
    except HostServerError:
        pass
    if objtype == "*SRVPGM":
        exp = hs.query(name, "SELECT SYMBOL_NAME, SYMBOL_USAGE FROM "
                       "QSYS2.PROGRAM_EXPORT_IMPORT_INFO WHERE PROGRAM_LIBRARY = ? AND "
                       "PROGRAM_NAME = ? AND OBJECT_TYPE = '*SRVPGM' AND SYMBOL_USAGE IN "
                       "('*PROCEXP', '*DATAEXP')", [lib, pgm], max_rows=2000)
        out["exports"] = [{"symbol": r[0], "kind": "procedure" if r[1] == "*PROCEXP"
                           else "data"} for r in exp["rows"]]
    return out


# ----------------------------------------------------------------- the rest
def describe_bnddir(hs, name, lib, obj) -> dict:
    res = hs.query(name, "SELECT ENTRY_LIBRARY, ENTRY, ENTRY_TYPE, ENTRY_ACTIVATION FROM "
                   "QSYS2.BINDING_DIRECTORY_INFO WHERE BINDING_DIRECTORY_LIBRARY = ? AND "
                   "BINDING_DIRECTORY = ?", [lib, obj], max_rows=2000)
    return {"object": "%s/%s" % (lib, obj), "type": "*BNDDIR",
            "entries": [{"object": "%s/%s" % (r[0], r[1]), "type": r[2], "activation": r[3]}
                        for r in res["rows"]]}


def describe_msgf(hs, name, lib, obj, pattern: str | None = None) -> dict:
    sql = ("SELECT MESSAGE_ID, SEVERITY, VARCHAR(MESSAGE_TEXT, 200) FROM QSYS2.MESSAGE_FILE_DATA "
           "WHERE MESSAGE_FILE_LIBRARY = ? AND MESSAGE_FILE = ?")
    params = [lib, obj]
    if pattern:
        sql += " AND MESSAGE_ID LIKE ?"
        params.append(pattern.upper().replace("*", "%"))
    # no ORDER BY (a message file is in ID order) and OPTIMIZE FOR: otherwise the
    # optimizer's estimate for a big file like QCPFMSG trips query time limits (SQL0666)
    res = hs.query(name, sql + " FETCH FIRST 501 ROWS ONLY OPTIMIZE FOR 501 ROWS", params,
                   max_rows=500)
    return {"object": "%s/%s" % (lib, obj), "type": "*MSGF", "truncated": res.get("truncated"),
            "messages": [{"id": r[0], "severity": int(r[1] or 0), "text": r[2]}
                         for r in res["rows"]]}


def describe_dtaara(hs, name, lib, obj) -> dict:
    res = hs.query(name, "SELECT DATA_AREA_TYPE, LENGTH, DECIMAL_POSITIONS, "
                   "VARCHAR(DATA_AREA_VALUE, 2000) FROM QSYS2.DATA_AREA_INFO WHERE "
                   "DATA_AREA_LIBRARY = ? AND DATA_AREA_NAME = ?", [lib, obj], max_rows=1)
    r = res["rows"][0] if res["rows"] else [None] * 4
    return {"object": "%s/%s" % (lib, obj), "type": "*DTAARA", "data_type": r[0],
            "length": r[1], "decimals": r[2], "value": r[3]}


def describe(hs, name: str, obj: str, objtype: str | None = None,
             pattern: str | None = None) -> list[dict]:
    """Describe an object (all of its types when there are several, e.g. a
    program and a file of the same name)."""
    hs._need(name)
    found = find(hs, name, obj, objtype)
    if not found:
        raise HostServerError("%s%s not found" % (obj.upper(), " (%s)" % objtype if objtype
                                                   else ""))
    out = []
    for o in found:
        lib, nm, t = o["lib"], o["name"], o["type"]
        if t == "*FILE":
            d = describe_file(hs, name, lib, nm, o["attribute"])
        elif t in ("*PGM", "*SRVPGM"):
            d = describe_program(hs, name, lib, nm, t)
        elif t == "*BNDDIR":
            d = describe_bnddir(hs, name, lib, nm)
        elif t == "*MSGF":
            d = describe_msgf(hs, name, lib, nm, pattern)
        elif t == "*DTAARA":
            d = describe_dtaara(hs, name, lib, nm)
        else:
            d = {"object": "%s/%s" % (lib, nm), "type": t}
        d.update(text=o["text"], attribute=o["attribute"])
        out.append(d)
    return out


def describe_text(items: list[dict]) -> str:
    out = []
    for d in items:
        head = "%s %s%s%s" % (d["object"], d["type"], " " + d["attribute"] if d.get("attribute")
                              else "", " - " + d["text"] if d.get("text") else "")
        out.append(head)
        if d["type"] == "*FILE":
            out.append("  %s, %d record format%s" % (d["kind"], len(d["formats"]),
                                                     "" if len(d["formats"]) == 1 else "s"))
            for f in d["formats"]:
                out.append("  format %s (%d bytes)%s" % (f["name"], f["length"],
                                                         " - " + f["text"] if f["text"] else ""))
                for x in f["fields"]:
                    extra = [s for s in (x["text"] or x["heading"],
                                         "alias " + x["alias"] if x["alias"] else "",
                                         "null-capable" if x["null"] else "") if s]
                    if d["kind"] in ("display file", "printer file"):
                        extra.insert(0, {"B": "both", "I": "input", "O": "output",
                                         "H": "hidden", "P": "program-to-system",
                                         "M": "message"}.get(x["usage"], x["usage"]))
                        if "row" in x:
                            extra.insert(1, "at %d,%d" % (x["row"], x["col"]))
                    out.append("    %-10s %-16s %s" % (x["name"], x["type"], "; ".join(extra)))
            if d["keys"]:
                out.append("  keys: %s%s" % (", ".join(k["field"] + (" (descending)"
                                                                     if k["descending"] else "")
                                                       for k in d["keys"]),
                                             " (unique)" if d["unique"] else ""))
        elif d["type"] in ("*PGM", "*SRVPGM"):
            for m in d["modules"]:
                out.append("  module %s (%s) from %s" % (m["module"], m["attribute"],
                                                         m["source"] or "?"))
            if d["service_programs"]:
                out.append("  binds " + ", ".join(d["service_programs"]))
            if d["type"] == "*SRVPGM":
                procs = [e["symbol"] for e in d["exports"] if e["kind"] == "procedure"]
                data = [e["symbol"] for e in d["exports"] if e["kind"] == "data"]
                out.append("  exports procedures: %s" % (", ".join(procs) or "none"))
                if data:
                    out.append("  exports data: %s" % ", ".join(data))
        elif d["type"] == "*BNDDIR":
            out += ["  %s %s" % (e["object"], e["type"]) for e in d["entries"]] or ["  (empty)"]
        elif d["type"] == "*MSGF":
            out += ["  %s %2d %s" % (m["id"], m["severity"], m["text"]) for m in d["messages"]]
        elif d["type"] == "*DTAARA":
            out.append("  %s, length %s%s: %r" % (d["data_type"], d["length"],
                                                  ", %s decimals" % d["decimals"]
                                                  if str(d["decimals"] or 0) != "0" else "",
                                                  d["value"]))
    return "\n".join(out)


# ----------------------------------------------------------------- messages
def message_file_for(mid: str) -> tuple[str, str] | None:
    for prefixes, lib, msgf in MESSAGE_FILES:
        if mid.startswith(prefixes):
            return lib, msgf
    return None


def explain_message(hs, name: str, mid: str, file: str | None = None) -> dict:
    """A message's text, cause and recovery (its second-level text)."""
    mid = mid.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9]{2}[0-9A-F]{4}", mid):
        raise HostServerError("a message ID is like RNF7030 or CPF9801 (got %r)" % mid)
    where = split_object(file) if file else message_file_for(mid)
    if not where:
        raise HostServerError("don't know which message file holds %s; pass file "
                              "(LIB/MSGF)" % mid)
    key = (where, mid)
    if key not in _cache:
        res = hs.query(name, "SELECT SEVERITY, VARCHAR(MESSAGE_TEXT, 500), "
                       "VARCHAR(MESSAGE_SECOND_LEVEL_TEXT, 4000) FROM QSYS2.MESSAGE_FILE_DATA "
                       "WHERE MESSAGE_FILE_LIBRARY = ? AND MESSAGE_FILE = ? AND MESSAGE_ID = ?",
                       [where[0], where[1], mid], max_rows=1)
        if not res["rows"]:
            raise HostServerError("%s is not in %s/%s" % (mid, where[0], where[1]))
        sev, text, second = res["rows"][0]
        cause = recovery = None
        if second:
            m = re.search(r"Cause\s*\.(?:\s*\.)*\s*:\s*(.*?)(?:Recovery\s*\.(?:\s*\.)*\s*:\s*(.*))?$",
                          second, re.S)
            if m:
                cause, recovery = (m.group(1) or "").strip() or None, (m.group(2) or "").strip() or None
        _cache[key] = {"id": mid, "file": "%s/%s" % where, "severity": int(sev or 0),
                       "text": text, "cause": cause, "recovery": recovery,
                       "help": None if cause else second}
    return _cache[key]


def explain_text(m: dict) -> str:
    lines = ["%s (severity %d, %s): %s" % (m["id"], m["severity"], m["file"], m["text"])]
    if m["cause"]:
        lines.append("Cause: " + " ".join(m["cause"].split()))
    if m["recovery"]:
        lines.append("Recovery: " + " ".join(m["recovery"].split()))
    if m.get("help"):
        lines.append(" ".join(m["help"].split()))
    return "\n".join(lines)
