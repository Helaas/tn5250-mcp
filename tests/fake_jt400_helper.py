"""Stands in for java/T5250Helper.java in offline tests: same JSON-lines
protocol, a tiny table and a folder that plays the IFS."""

import base64
import json
import os
import sys

ROOT = os.environ["FAKE_IFS"]
conns = {}
PASSWORDS = {}
LIBL = []
EVF = {}
MEMBERS = {}
SPOOL = []          # [file, number, job, lines, created]
SUBMITTED = {}      # job -> end code
LISTINGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "listings")


def job_spool(job):
    """Spooled files of a submitted job: tests/listings/<JOBNAME>-<n>-<FILE>.txt"""
    jobname = job.split("/")[-1]
    out = []
    for f in sorted(os.listdir(LISTINGS)):
        parts = f[:-4].split("-")
        if parts[0] == jobname:
            out.append([parts[2], parts[1], job, open(os.path.join(LISTINGS, f)).read().split(
                "\n"), "2026-11-16 12:00:03"])
    return out
SRC = {("BOBLIB", "QRPGLESRC", "HELLO"): [["1.00", 240101, "**free"],
                                         ["2.00", 240101, "dsply 'hi';"],
                                         ["3.00", 240101, "*inlr = *on;"]]}
SRC[("BOBLIB", "QRPGLESRC", "CRASH")] = [[str(i), 240101, t] for i, t in enumerate([
    "**free", "ctl-opt dftactgrp(*no);", "/copy QRPGLESRC,CPY", "dcl-pi *n;", "end-pi;",
    "x = 1 / z;", "*inlr = *on;"], 1)]
SRC[("BOBLIB", "QRPGLESRC", "CPY")] = [["1", 240101, "dcl-s x packed(5:0);"],
                                       ["2", 240101, "dcl-s z packed(5:0);"]]
OBJECTS = {"CUSTMAST": [("*FILE", "PF", "Customers")], "CUSTDSP": [("*FILE", "DSPF", "")],
           "CRASH": [("*PGM", "RPGLE", "crashes")], "UTILS": [("*SRVPGM", "RPGLE", "")],
           "APP": [("*BNDDIR", "", "")], "APPMSG": [("*MSGF", "", "app messages")],
           "CFG": [("*DTAARA", "", "")], "DUP": [("*PGM", "CLLE", ""), ("*FILE", "PF", "")]}
FFD_COLS = ["WHFILE", "WHLIB", "WHFTYP", "WHNAME", "WHTEXT", "WHRLEN", "WHFLDE", "WHFLDB",
            "WHFLDD", "WHFLDP", "WHFTXT", "WHCHD1", "WHCHD2", "WHCHD3", "WHFLDT", "WHFIOB",
            "WHALIS", "WHVARL", "WHNULL", "WHFMT", "WHDROW", "WHDCOL"]
FFD = {"CUSTMAST": [
    ["CUSTMAST", "BOBLIB", "P", "CUSTR", "customer", "50", "CUSTID", "5", "0", "0", "Cust ID",
     "Cust", "ID", "", "A", "B", "", "N", "N", "", "0", "0"],
    ["CUSTMAST", "BOBLIB", "P", "CUSTR", "customer", "50", "NAME", "32", "0", "0", "Name", "NAME",
     "", "", "A", "B", "CUSTOMER_NAME", "Y", "Y", "", "0", "0"],
    ["CUSTMAST", "BOBLIB", "P", "CUSTR", "customer", "50", "BAL", "5", "9", "2", "", "BAL", "",
     "", "P", "B", "", "N", "N", "", "0", "0"],
    ["CUSTMAST", "BOBLIB", "P", "CUSTR", "customer", "50", "SINCE", "10", "0", "0", "", "SINCE",
     "", "", "L", "B", "", "N", "N", "*ISO", "0", "0"]],
    "CUSTDSP": [
    ["CUSTDSP", "BOBLIB", "D", "SCR1", "", "36", "CUSTID", "5", "0", "0", "", "", "", "", "A",
     "B", "", "N", "N", "", "3", "2"],
    ["CUSTDSP", "BOBLIB", "D", "SCR1", "", "36", "OPT", "1", "0", "0", "", "", "", "", "A",
     "I", "", "N", "N", "", "5", "2"]]}
SRC_FILE = os.path.join(ROOT, ".fake-members.json")     # members outlive one helper
if os.path.exists(SRC_FILE):
    SRC = {tuple(k.split("/")): v for k, v in json.load(open(SRC_FILE)).items()}


def save_src():
    json.dump({"/".join(k): v for k, v in SRC.items()}, open(SRC_FILE, "w"))
JOBPOLL = [0]
EXPIRED = set(os.environ.get("FAKE_EXPIRED", "").split(",")) - {""}


class SecurityError(Exception):
    def __init__(self, msg, expired=False, new_rejected=False):
        super().__init__(msg)
        self.expired = expired


def real(p):
    return os.path.join(ROOT, p.lstrip("/"))


def handle(r):
    op = r["op"]
    if op == "change_password":
        if r["old"] != PASSWORDS.get(r["user"], "right-pw"):
            raise SecurityError("CPF2234: Password not correct for user profile %s." % r["user"])
        if len(r["new"]) < 6:
            raise SecurityError("CPF22E5: New password is too short.", new_rejected=True)
        PASSWORDS[r["user"]] = r["new"]
        EXPIRED.discard(r["user"])
        return True
    if op == "connect":
        if r["password"] != PASSWORDS.get(r["user"], "right-pw"):
            raise SecurityError("CPF2234: Password not correct for user profile %s." % r["user"])
        if r["user"] in EXPIRED:
            raise SecurityError("CPF22E3: Password has expired.", expired=True)
        conns[r["name"]] = r["user"]
        LIBL[:] = r.get("libl") or []
        out = {"user": r["user"], "host": r["host"].upper(), "release": "V7R5M0",
               "tls": r["tls"]}
        if os.environ.get("FAKE_WARN_DAYS"):
            out["expires_in_days"] = int(os.environ["FAKE_WARN_DAYS"])
        return out
    if op == "crash":
        os._exit(3)
    if r["name"] not in conns:
        raise Exception("not connected (%s)" % r["name"])
    if op == "close":
        conns.pop(r["name"], None)
        return True
    if op == "command":
        cmd = r["command"]
        if r.get("libl"):
            LIBL[:] = r["libl"]
        res = {"job": "654321/QUSER/QZRCSRVS", "libl": list(LIBL) or None}
        if os.environ.get("FAKE_CMDLOG"):
            with open(os.environ["FAKE_CMDLOG"], "a") as fh:
                fh.write(cmd + "\n")
        if cmd.startswith(("CRTBNDRPG", "CRTSQLRPGI", "CRTBNDCL", "CRTRPGMOD", "CRTCLMOD",
                           "CRTDSPF", "CRTPRTF", "CRTPF", "CRTLF")):
            m = __import__("re").search(r"(PGM|OBJ|MODULE|FILE)\((\w+)/(\w+)\) "
                                        r"(?:SRCSTMF\('([^']+)'\)|SRCFILE\((\w+/\w+)\) "
                                        r"SRCMBR\((\w+)\))", cmd)
            lib, obj, path = m.group(2), m.group(3), m.group(4)
            if path:
                src = open(real(path)).read() if os.path.exists(real(path)) else None
            else:
                path = "%s(%s)" % (m.group(5), m.group(6))
                src = MEMBERS.get(path)
            if src is None:
                return dict(res, ok=False, messages=[{"id": "CPF9897", "severity": 40,
                            "type": 15, "text": "Source not found."}])
            recs = ["TIMESTAMP  0 %s" % os.environ.get("FAKE_EVF_TIME", "20261116120001"),
                    "PROCESSOR  0 000 1",
                    "FILEID     0      001 000000 052 %s 20261116120000 0" % path]
            for no, line in enumerate(src.splitlines(), 1):
                col = line.find("TOTL")
                if col >= 0:
                    recs.append("ERROR      0      001 1 %06d %06d %03d %06d %03d RNF7030 S 30 "
                                "049 The name or indicator TOTL is not defined." % (
                                    no, no, col + 1, no, col + 4))
                if "warn" in line:
                    recs.append("ERROR      0      001 1 %06d %06d 001 %06d 001 RNF7031 I 00 "
                                "040 The name or indicator is not referenced." % (no, no, no))
            recs.append("FILEEND    0      001 %06d" % len(src.splitlines()))
            EVF[(lib, obj)] = recs
            bad = any(" S 30 " in x for x in recs)
            return dict(res, ok=not bad, messages=[{"id": "RNS9310" if bad else "RNS9304",
                        "severity": 50 if bad else 0, "type": 15 if bad else 2,
                        "text": "Compilation stopped. Severity 30 errors found in program."
                        if bad else "Program %s placed in library %s." % (obj, lib)}])
        if cmd.startswith("QSH CMD("):              # run the PASE wrapper with a local sh
            import re as _re
            import subprocess as _sp
            d = _re.search(r"sh (/tmp/t5250-\w+)/run\.sh", cmd).group(1)
            script = open(real(d + "/run.sh")).read().replace(d, real(d)).replace(
                "/QOpenSys/usr/bin/sh", "/bin/sh").replace("PATH=", "PATH=/bin:", 1).replace(
                "cd '/", "cd '%s/" % ROOT)
            _sp.run(["/bin/sh", "-c", script], timeout=60)
            return dict(res, ok=True, messages=[{"id": "QSH0005", "severity": 0, "type": 2,
                        "text": "Command ended normally with exit status 0."}])
        if cmd.startswith(("ADDPFM", "CHGPFM")):
            m = __import__("re").search(r"FILE\((\w+)/(\w+)\) MBR\((\w+)\)", cmd)
            if m.group(2) == "NOFILE":
                return dict(res, ok=False, messages=[{"id": "CPF7301", "severity": 40,
                            "type": 15, "text": "File NOFILE in BOBLIB not found."}])
            SRC.setdefault(m.groups(), [])
            save_src()
            return dict(res, ok=True, messages=[])
        if cmd.startswith("CPYFRMSTMF"):
            m = __import__("re").search(r"FROMSTMF\('([^']+)'\) TOMBR\('/QSYS.LIB/(\w+).LIB/"
                                        r"(\w+).FILE/(\w+).MBR'\)", cmd)
            MEMBERS["%s/%s(%s)" % m.group(2, 3, 4)] = open(real(m.group(1))).read()
            return dict(res, ok=True, messages=[])
        if cmd.startswith("SBMJOB"):
            name = __import__("re").search(r"JOB\((\w+)\)", cmd).group(1)
            job = "%06d/BOB/%s" % (432000 + len(SUBMITTED), name)
            SUBMITTED[job] = 20 if job_spool(job) else 0
            return dict(res, ok=True, messages=[{"id": "CPC1221", "severity": 0, "type": 1,
                        "text": "Job %s submitted to job queue QBATCH in library QGPL." % job}])
        if cmd.startswith("DSPLIBL OUTPUT(*PRINT)"):
            SPOOL.insert(0, ["QPRTLIBL", str(len(SPOOL) + 7), "654322/BOB/QPRTJOB",
                             ["Library List", "  MYLIB   USR"], "2026-11-16 12:00:04"])
            return dict(res, ok=True, messages=[])
        if cmd.startswith("DLTSPLF"):
            SPOOL[:] = [s for s in SPOOL if "FILE(%s)" % s[0] not in cmd]
            return dict(res, ok=True, messages=[])
        if cmd.startswith(("CRTSRVPGM", "CRTPGM", "RUNSQLSTM", "CRTBNDDIR", "ADDBNDDIRE",
                           "DLTF", "CRTSRCPF")):
            if cmd.startswith("RUNSQLSTM"):
                path = __import__("re").search(r"SRCSTMF\('([^']+)'\)", cmd).group(1)
                if "syntax error" in open(real(path)).read():
                    stem = os.path.basename(path).split(".")[0].upper()
                    SPOOL.insert(0, [stem, str(len(SPOOL) + 5), "654322/BOB/QPRTJOB", [
                        "Record  *...+... 1", "MSG ID  SEV  RECORD  TEXT",
                        "SQL0104  30       2  Position 8 Token SYNTAX was not valid.",
                        "                    Message Summary"], "2026-11-16 12:00:02"])
                    return dict(res, ok=False, messages=[{"id": "SQL9010", "severity": 30,
                                "type": 15, "text": "RUNSQLSTM command failed."}])
            return dict(res, ok=True, messages=[])
        if cmd.startswith("DLYJOB") or cmd.startswith("DSP"):
            return dict(res, ok=True, messages=[])
        return dict(res, ok=False, messages=[{"id": "CPD0030", "severity": 30, "type": 15,
                                              "text": "Command %s not found." % cmd.split()[0]}])
    if op == "call_program":
        if r.get("libl"):
            LIBL[:] = r["libl"]
        if r["program"].endswith("CRASH"):
            return {"ok": False, "outputs": [None] * len(r["params"]), "job":
                    "654321/QUSER/QZRCSRVS", "messages": [{"id": "CEE9901", "severity": 30,
                    "type": 15, "text": "Application error.  RNX9998 unmonitored by CRASH at "
                    "statement *N, instruction X'0000'."}]}
        if r["program"].endswith("NOSUCH"):
            return {"ok": False, "outputs": [None] * len(r["params"]), "job":
                    "654321/QUSER/QZRCSRVS", "messages": [{"id": "CPF4101", "severity": 40,
                    "type": 15, "text": "Program %s not found." % r["program"]}]}
        nums = [float(p["value"]) for p in r["params"]
                if not p["out"] and p["type"].startswith(("packed", "zoned", "int"))]
        outs = [None if not p["out"] else
                ("HELLO %s" % r["params"][0]["value"]).strip() if p["type"].startswith("char")
                else sum(nums) for p in r["params"]]
        return {"ok": True, "outputs": outs, "job": "654321/QUSER/QZRCSRVS",
                "messages": [], "libl": list(LIBL) or None}
    if op == "ifs_delete":
        import shutil as _sh
        _sh.rmtree(real(r["path"]), ignore_errors=True)
        return True
    if op == "read_source":
        k = (r["lib"], r["file"], r["member"])
        if k not in SRC:
            raise Exception("SQL0204: %s in %s type *FILE not found." % (r["file"], r["lib"]))
        return {"lines": SRC[k], "width": 100, "truncated": False}
    if op == "write_source":
        k = (r["lib"], r["file"], r["member"])
        for i, (_, t) in enumerate(r["lines"], 1):
            if len(t) > 100:
                raise Exception("line %d is %d characters; this source file holds 100"
                                % (i, len(t)))
        SRC[k] = [["%d.00" % i, d, t] for i, (d, t) in enumerate(r["lines"], 1)]
        save_src()
        return {"lines": len(r["lines"]), "width": 100}
    if op == "query" and "SYSPARTITIONSTAT" in r["sql"]:
        lib, file = r["params"][:2]
        like = r["params"][2].replace("%", "") if len(r["params"]) > 2 else ""
        rows = [[m, "RPGLE", "", str(len(v)), "2026-11-16 12:00:00"]
                for (l, f, m), v in sorted(SRC.items()) if (l, f) == (lib, file)
                and m.startswith(like)]
        return {"columns": ["MEMBER", "TYPE", "TEXT", "LINES", "CHANGED"], "types": [],
                "rows": rows, "truncated": False}
    if op == "query" and "FILE_TYPE = 'S'" in r["sql"]:
        files = sorted({f for (l, f, _) in SRC if l == r["params"][0]})
        return {"columns": ["FILE", "TEXT", "WIDTH"], "types": [],
                "rows": [[f, None, "100"] for f in files], "truncated": False}
    if op == "ifs_mkdir":
        os.makedirs(real(r["path"]), exist_ok=True)
        return True
    if op == "read_member":
        k = (r["lib"], r["member"])
        if r["file"] != "EVFEVENT" or k not in EVF:
            raise Exception("SQL0204: %s in %s type *FILE not found." % (r["file"], r["lib"]))
        return {"records": EVF[k]}
    if op == "spooled_file":
        if r["job"] in SUBMITTED:
            hit = next((s for s in job_spool(r["job"]) if s[0] == r["file"]
                        and s[1] == r["number"]), None)
            return {"lines": hit[3] if hit else [], "truncated": False}
        hit = next((s for s in SPOOL if s[0] == r["file"]), None)
        if hit:
            return {"lines": hit[3], "truncated": False}
        if not r["job"].endswith("/QPRTJOB"):
            return {"lines": [], "truncated": False}
        return {"lines": ["PAGE 1", "REPORT FOR %s" % r["job"], "TOTAL 42"], "truncated": False}
    if op == "query" and "VARCHAR_FORMAT(CURRENT TIMESTAMP" in r["sql"]:
        return {"columns": ["TS"], "types": ["VARCHAR"], "rows": [["20261116120000"]],
                "truncated": False}
    if op == "outfile":
        import re as _re
        if not r["command"].startswith(("DSPFFD", "DSPFD")):
            raise Exception("not a display command this helper runs")
        f = _re.search(r"FILE\(\w+/(\w+)\)", r["command"]).group(1)
        if r["command"].startswith("DSPFFD"):
            return {"columns": FFD_COLS, "rows": FFD.get(f, [])}
        return {"columns": ["APKEYF", "APKSEQ", "APKEYN", "APUNIQ"],
                "rows": [["CUSTID", "A", "1", "Y"]] if f == "CUSTMAST" else []}
    if op == "query" and "OBJECT_STATISTICS" in r["sql"]:
        lib, typ, name = r["params"]
        rows = [[name, t, a, txt or None, "BOBLIB" if lib == "*LIBL" else lib]
                for t, a, txt in OBJECTS.get(name, []) if typ in ("*ALL", t)]
        return {"columns": ["OBJNAME", "OBJTYPE", "OBJATTRIBUTE", "OBJTEXT", "OBJLONGSCHEMA"],
                "types": [], "rows": rows, "truncated": False}
    if op == "query" and "BOUND_MODULE_INFO" in r["sql"]:
        pgm = r["params"][1]
        attr = {"CRASH": "RPGLE", "SQLCRASH": "SQLRPGLE", "CLCRASH": "CLLE", "UTILS": "RPGLE"}
        rows = [[pgm, attr.get(pgm, "RPGLE"), "BOBLIB", "QRPGLESRC", pgm, None]] if pgm in attr \
            else []
        if len(r["params"]) == 3:          # describe: module library first, and more
            rows = [["BOBLIB"] + x + ["2026-11-16 12:00:00", "*YES"] for x in rows]
        return {"columns": [], "types": [], "rows": rows, "truncated": False}
    if op == "query" and "BOUND_SRVPGM_INFO" in r["sql"]:
        rows = [["BOBLIB", "UTILS"], ["QSYS", "QRNXIE"]] if r["params"][1] == "CRASH" else []
        return {"columns": [], "types": [], "rows": rows, "truncated": False}
    if op == "query" and "PROGRAM_EXPORT_IMPORT_INFO" in r["sql"]:
        return {"columns": [], "types": [], "rows": [["GREET", "*PROCEXP"],
                                                     ["COUNTER", "*DATAEXP"]], "truncated": False}
    if op == "query" and "BINDING_DIRECTORY_INFO" in r["sql"]:
        return {"columns": [], "types": [], "rows": [["BOBLIB", "UTILS", "*SRVPGM", "*IMMED"]],
                "truncated": False}
    if op == "query" and "DATA_AREA_INFO" in r["sql"]:
        return {"columns": [], "types": [], "rows": [["*CHAR", "20", "0", "PROD"]],
                "truncated": False}
    if op == "query" and "MESSAGE_FILE_DATA" in r["sql"]:
        msgs = {"RNQ0102": ("99", "Attempt to divide by zero (C G D F).",
                            "Cause . . . . . :   RPG procedure &1 tried to divide by zero. "
                            "Recovery  . . . :   Contact the person responsible."),
                "RNF7030": ("30", "The name or indicator is not defined.",
                            "Cause . . . . . :   A field is not defined. Recovery  . . . :   "
                            "Define it. Compile again."),
                "APP0001": ("10", "Customer &1 not found.", "No help.")}
        if len(r["params"]) == 3 and "LIKE" not in r["sql"]:       # one message
            hit = msgs.get(r["params"][2])
            return {"columns": [], "types": [], "rows": [list(hit)] if hit else [],
                    "truncated": False}
        return {"columns": [], "types": [], "truncated": False,
                "rows": [["APP0001", "10", "Customer &1 not found."]]}
    if op == "query" and "FROM_INSTRUCTION" in r["sql"]:     # a crash's job log, newest first
        def m(i, t, sev, text, lib=None, pgm="QSYS", mod=None, proc=None, stmt="*N", to=None):
            return [i, t, sev, text, lib, pgm, mod, proc, stmt, to]
        rows = [
            m("CEE9901", "ESCAPE", "30", "Application error.  RNX9998 unmonitored by CRASH at "
              "statement *N, instruction X'0000'.", "QSYS", "QLEAWI", "QLEDEH", "eh", "175"),
            m("RNX9998", "ESCAPE", "50", "Internal failure in compiler or subroutine.", "QSYS",
              "QRNXIE", "QRNXMSG", "SignalException", "22"),
            m("CPF2469", "ESCAPE", "40", "Error occurred when sending message RNQ0102.", "QSYS",
              "QMHSNINQ"),
            m("CPF9999", "ESCAPE", "40", "Function check. MCH1211 unmonitored by CRASH at "
              "statement 0000000008, instruction X'0000'.", None, "QMHUNMSG"),
            m("MCH1211", "ESCAPE", "40", "Attempt made to divide by zero for fixed point "
              "operation.", "BOBLIB", "CRASH", "CRASH", "CRASH", "8", "CRASH"),
            m("CPF5C61", "INFORMATIONAL", "0", "Client request - run program BOBLIB/CRASH."),
            m("MCH0603", "ESCAPE", "40", "An older error from another call.", "BOBLIB", "OLD",
              "OLD", "OLD", "3", "OLD")]
        return {"columns": [], "types": [], "rows": rows, "truncated": False}
    if op == "query" and "JOBLOG_INFO" in r["sql"]:
        return {"columns": ["MESSAGE_TIMESTAMP", "MESSAGE_ID", "MESSAGE_TYPE", "SEVERITY",
                            "MESSAGE_TEXT"], "types": [], "truncated": False,
                "rows": [["2026-11-16 12:00:02", "RNS9310", "ESCAPE", "50", "Compilation "
                          "stopped."], ["2026-11-16 12:00:01", "CPC2206", "COMPLETION", "0",
                                        "Ownership changed."]], "params": r["params"]}
    if op == "query" and "SPOOLED_FILE_INFO" in r["sql"]:
        if r["params"] and r["params"][0] in SUBMITTED:
            spool = job_spool(r["params"][0])
        else:
            spool = SPOOL + [["QPRTLIBL", "1", "654322/BOB/QPRTJOB", [], "2026-11-15 10:00:00"]]
        return {"columns": ["SPOOLED_FILE_NAME", "SPOOLED_FILE_NUMBER", "QUALIFIED_JOB_NAME",
                            "STATUS", "TOTAL_PAGES", "USER_DATA", "CREATION_TIMESTAMP"],
                "types": [], "rows": [[x[0], x[1], x[2], "READY", "1", None, x[4]]
                                      for x in spool], "truncated": False,
                "params": r["params"]}
    if op == "query" and "VARCHAR(CURRENT TIMESTAMP)" in r["sql"]:
        return {"columns": ["TS"], "types": [], "rows": [["2026-11-16-12.00.00.000000"]],
                "truncated": False}
    if op == "query" and "HISTORY_LOG_INFO" in r["sql"]:
        job = r["params"][1]
        if job not in SUBMITTED or os.environ.get("FAKE_JOB_NEVER_ENDS"):
            return {"columns": ["TEXT"], "types": [], "rows": [], "truncated": False}
        return {"columns": ["TEXT"], "types": [], "truncated": False, "rows": [[
            "Job %s ended on 26-11-16 at 12:00:03; .014 seconds used; end code %d ."
            % (job, SUBMITTED[job])]]}
    if op == "query":
        if not r["sql"].lstrip().upper().startswith(("SELECT", "WITH", "VALUES")):
            raise Exception("SQL0817: Update operation not allowed. (SQLSTATE 42987)")
        if "JOB_INFO" in r["sql"]:             # a submitted job, advancing per poll
            states = os.environ.get("FAKE_JOB_STATES", "OUTQ").split(",")
            JOBPOLL[0] += 1
            st = states[min(JOBPOLL[0], len(states)) - 1]
            if st == "GONE":
                return {"columns": ["JOB_STATUS", "COMPLETION_STATUS"], "types": [],
                        "rows": [], "truncated": False}
            done = os.environ.get("FAKE_COMPLETION", "NORMAL") if st == "OUTQ" else None
            return {"columns": ["JOB_STATUS", "COMPLETION_STATUS"], "types": ["CHAR", "CHAR"],
                    "rows": [[st, done]], "truncated": False, "params": r["params"]}
        rows = [[str(i), "ITEM %d" % i, None if i == 2 else "%d.50" % i] for i in range(1, 6)]
        n = r["max_rows"]
        return {"columns": ["ID", "NAME", "PRICE"], "types": ["INTEGER", "CHAR", "DECIMAL"],
                "rows": rows[:n], "truncated": len(rows) > n, "params": r["params"]}
    if op == "execute":
        return {"update_count": 1}
    if op == "ifs_list":
        p = real(r["path"])
        if not os.path.exists(p):
            raise Exception("no such file or directory: " + r["path"])
        names = sorted(os.listdir(p)) if os.path.isdir(p) else [os.path.basename(p)]
        base = p if os.path.isdir(p) else os.path.dirname(p)
        return {"path": r["path"], "dir": os.path.isdir(p), "truncated": False, "entries": [
            {"name": n, "dir": os.path.isdir(os.path.join(base, n)),
             "size": os.path.getsize(os.path.join(base, n)), "modified": 0} for n in names]}
    if op == "ifs_read":
        data = open(real(r["path"]), "rb").read()
        part = data[:r["max_bytes"]]
        out = {"size": len(data), "ccsid": 1208, "truncated": len(data) > len(part)}
        if r["binary"]:
            out["base64"] = base64.b64encode(part).decode()
        else:
            out["text"] = part.decode("utf-8")
        return out
    if op == "ifs_write":
        p = real(r["path"])
        if os.path.exists(p) and not (r["overwrite"] or r["append"]):
            raise Exception(r["path"] + " exists; pass overwrite or append")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        data = base64.b64decode(r["base64"]) if "base64" in r else r["text"].encode()
        with open(p, "ab" if r["append"] else "wb") as fh:
            fh.write(data)
        return {"path": r["path"], "size": os.path.getsize(p), "ccsid": r.get("ccsid", 65535)}
    raise Exception("unknown op " + op)


print(json.dumps({"ready": True, "jt400": "fake 1.0"}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    try:
        resp = {"id": req["id"], "ok": True, "result": handle(req)}
    except Exception as e:  # noqa: BLE001
        resp = {"id": req["id"], "ok": False, "error": str(e)}
        if isinstance(e, SecurityError):
            resp["security"] = True
            if e.expired:
                resp["expired"] = True
    print(json.dumps(resp), flush=True)
