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
        if cmd.startswith(("CRTBNDRPG", "CRTSQLRPGI", "CRTBNDCL")):
            m = __import__("re").search(r"(PGM|OBJ)\((\w+)/(\w+)\) SRCSTMF\('([^']+)'\)", cmd)
            lib, obj, path = m.group(2), m.group(3), m.group(4)
            src = open(real(path)).read() if os.path.exists(real(path)) else None
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
        if cmd.startswith("DLYJOB") or cmd.startswith("DSP"):
            return dict(res, ok=True, messages=[])
        return dict(res, ok=False, messages=[{"id": "CPD0030", "severity": 30, "type": 15,
                                              "text": "Command %s not found." % cmd.split()[0]}])
    if op == "read_member":
        k = (r["lib"], r["member"])
        if r["file"] != "EVFEVENT" or k not in EVF:
            raise Exception("SQL0204: %s in %s type *FILE not found." % (r["file"], r["lib"]))
        return {"records": EVF[k]}
    if op == "spooled_file":
        if not r["job"].endswith("/QPRTJOB"):
            return {"lines": [], "truncated": False}
        return {"lines": ["PAGE 1", "REPORT FOR %s" % r["job"], "TOTAL 42"], "truncated": False}
    if op == "query" and "VARCHAR_FORMAT(CURRENT TIMESTAMP" in r["sql"]:
        return {"columns": ["TS"], "types": ["VARCHAR"], "rows": [["20261116120000"]],
                "truncated": False}
    if op == "query" and "JOBLOG_INFO" in r["sql"]:
        return {"columns": ["MESSAGE_TIMESTAMP", "MESSAGE_ID", "MESSAGE_TYPE", "SEVERITY",
                            "MESSAGE_TEXT"], "types": [], "truncated": False,
                "rows": [["2026-11-16 12:00:02", "RNS9310", "ESCAPE", "50", "Compilation "
                          "stopped."], ["2026-11-16 12:00:01", "CPC2206", "COMPLETION", "0",
                                        "Ownership changed."]], "params": r["params"]}
    if op == "query" and "SPOOLED_FILE_INFO" in r["sql"]:
        return {"columns": ["SPOOLED_FILE_NAME", "SPOOLED_FILE_NUMBER", "QUALIFIED_JOB_NAME"],
                "types": [], "rows": [["QPRTLIBL", "1", "654322/BOB/QPRTJOB"]],
                "truncated": False, "params": r["params"]}
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
