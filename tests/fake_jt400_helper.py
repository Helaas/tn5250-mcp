"""Stands in for java/T5250Helper.java in offline tests: same JSON-lines
protocol, a tiny table and a folder that plays the IFS."""

import base64
import json
import os
import sys

ROOT = os.environ["FAKE_IFS"]
conns = {}
PASSWORDS = {}
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
    if op == "query":
        if not r["sql"].lstrip().upper().startswith(("SELECT", "WITH", "VALUES")):
            raise Exception("SQL0817: Update operation not allowed. (SQLSTATE 42987)")
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
