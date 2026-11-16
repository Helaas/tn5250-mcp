"""Errors out of IBM i compiler listings and spooled job logs.

When something else runs the compile (a change-management tool, a build
script, a batch job), there is no event file to read; what is left is the
listing in the job's spooled files. This reads the formats of:

- ILE RPG (CRTBNDRPG, CRTRPGMOD, and the RPG step of CRTSQLRPGI): messages
  with a source sequence number, inline or under "Additional Diagnostic
  Messages";
- ILE CL (CRTBNDCL, CRTCLMOD, CRTCLPGM): "* CPD0727 40 text" after the
  source line it is about;
- DDS (CRTPF, CRTLF, CRTDSPF, CRTPRTF): "CPD7410-*****" under the source
  line, with severity and text in the message section;
- the SQL precompiler and RUNSQLSTM: "SQL0204  30  12  Position 5 text";

and QPJOBLOG (a job log printed when the job ended).

Line numbers: a listing gives the source sequence number (SEU's 0003.00);
`line` assumes members numbered 1.00, 2.00, ... (as source member tools and
CPYFRMSTMF number them) and `seq` keeps the number as printed.
"""

from __future__ import annotations

import re

_ID = r"[A-Z]{2,3}[0-9A-F]{4,5}"
# RPG: *RNF7030 30      3 000300  The name ...   (also fixed-form: *RNF7030 30 a      000300)
_RPG = re.compile(r"^\*(?P<id>%s)\s+(?P<sev>\d\d)\s+(?:[a-z]\s+)?(?:(?P<num>\d+)\s+)?"
                  r"(?P<seq>\d{6})\s+(?P<text>\S.*)$" % _ID)
# CL: * CPD0727 40  Variable &Y is referred to but not declared.
_CL = re.compile(r"^\*\s(?P<id>%s)\s+(?P<sev>\d\d)\s+(?P<text>\S.*)$" % _ID)
_CL_SRC = re.compile(r"^\s*(?P<seq>\d+)-\s")
# DDS source line, marker line and message section
_DDS_SRC = re.compile(r"^\s{1,8}(?P<seq>\d{2,6})\s{2,}\S")
_DDS_MARK = re.compile(r"^\*\s{5,}(?P<ids>(?:%s-?)+)\*" % _ID)
_DDS_MSG = re.compile(r"^\*\s+(?P<id>%s)\s+(?P<sev>\d\d)\s+(?:\d+\s+)?Message \. \. \. \. :\s+"
                      r"(?P<text>.*)$" % _ID)
# SQL precompiler / RUNSQLSTM: SQL1103  10       3  Position 39 Column definitions ...
_SQL = re.compile(r"^(?P<id>SQ[L0-9A-Z]\d{4})\s+(?P<sev>\d+)\s+(?P<rec>\d+)\s+(?P<text>\S.*)$")
_CONT = re.compile(r"^\s{15,}(?P<text>\S.*)$")
_HEADER = re.compile(r"^\s*(?P<key>[A-Za-z][A-Za-z ']*?)\s*[ .]{2,}:?\s*(?P<val>\S+)")
_PAGE = re.compile(r"Page\s+\d+\s*$")
_END = re.compile(r"\*\s\*\s\*\s\*\s\*\s+E\s?N\s?D")


def severity_name(sev: int) -> str:
    return ("info" if sev < 10 else "warning" if sev < 20 else "error" if sev < 30
            else "severe" if sev < 40 else "terminal")


def _seq_line(seq: str) -> tuple[str, int]:
    """'000300' or '300' -> ('3.00', 3)."""
    n = int(seq)
    return "%d.%02d" % (n // 100, n % 100), n // 100 if n % 100 == 0 else round(n / 100)


def _source(lines: list[str]) -> str | None:
    """LIB/FILE(MEMBER) or /stream/file from the listing's first page."""
    lib = file = mbr = stmf = None
    for line in lines[:80]:
        m = _HEADER.match(line)
        if not m:
            continue
        key, val = m.group("key").strip().lower(), m.group("val").strip()
        if key in ("source stream file", "source stream file name"):
            stmf = val
        elif key in ("source file", "source file containing dds"):
            if "/" in val:
                lib, file = val.split("/", 1)
            else:
                file, lib = val, None
        elif key in ("library", "library name") and file and lib is None:
            lib = val
        elif key in ("source member", "member", "source member name",
                     "source member containing dds") and mbr is None:
            mbr = val
    if stmf and stmf.startswith("/"):
        return stmf
    if lib and file and mbr:
        return "%s/%s(%s)" % (lib, file, mbr)
    return None


def _diag(source, line, seq, column, sev, mid, text) -> dict:
    return {"file": source, "line": line, "seq": seq, "column": column, "severity": sev,
            "severity_name": severity_name(sev), "id": mid, "text": " ".join(text.split())}


def parse_listing(lines: list[str]) -> dict:
    """{"compiler": rpg|cl|dds|sql|None, "source": ..., "diagnostics": [...]}"""
    head = "\n".join(lines[:3])
    compiler = ("sql" if ("Create SQL" in head or "Run SQL Statements" in head)
                else "rpg" if "ILE RPG" in head else "cl" if "Control Language" in head
                else "dds" if "Data Description" in head else None)
    source = _source(lines)
    out: list[dict] = []
    last_seq: str | None = None
    in_source = False
    dds_marks: list[tuple[str, str]] = []          # (id, seq)
    dds_text: dict[str, tuple[int, str]] = {}
    prev: dict | None = None
    # a line just before a page header (or the end) is a page footer, not message text
    footer = {i for i in range(len(lines))
              if i + 1 >= len(lines) or _PAGE.search(lines[i + 1] or "")}
    for i, raw in enumerate(lines):
        line = raw.rstrip()
        s = line.strip()
        if compiler in ("cl", "dds") and ("Control Language Source" in line
                                          or "Data Description Source" in line):
            in_source, last_seq = True, None
            prev = None
            continue
        if in_source and (_END.search(line) or "E N D   O F   S O U R C E" in line):
            in_source = False
        m = _RPG.match(line) if compiler in ("rpg", None) else None
        if m:
            seq, ln = _seq_line(m.group("seq"))
            prev = _diag(source, ln, seq, 0, int(m.group("sev")), m.group("id"), m.group("text"))
            prev["_col"] = m.start("text")
            out.append(prev)
            continue
        m = _SQL.match(line) if compiler in ("sql", None) else None
        if m:
            text = m.group("text")
            col = 0
            pm = re.match(r"Position (\d+)\s+(.*)", text)
            if pm:
                col, text = int(pm.group(1)), pm.group(2)
            rec = int(m.group("rec"))
            prev = _diag(source, rec, None, col, int(m.group("sev")), m.group("id"), text)
            prev["_col"] = m.start("text")
            out.append(prev)
            continue
        if compiler == "dds":
            m = _DDS_MARK.match(line)
            if m:
                for mid in re.findall(_ID, m.group("ids")):
                    dds_marks.append((mid, last_seq or "0"))
                prev = None
                continue
            m = _DDS_MSG.match(line)
            if m:
                dds_text[m.group("id")] = (int(m.group("sev")), m.group("text"))
                prev = {"_dds": m.group("id"), "_col": m.start("text")}
                continue
            if in_source:
                m = _DDS_SRC.match(line)
                if m:
                    last_seq = m.group("seq")
                    prev = None
                    continue
        if compiler in ("cl", None):
            if in_source:
                m = _CL_SRC.match(line)
                if m:
                    last_seq = m.group("seq")
                    prev = None
                    continue
            m = _CL.match(line)
            if m:
                seq, ln = _seq_line(last_seq) if (in_source and last_seq) else (None, 0)
                prev = _diag(source, ln, seq, 0, int(m.group("sev")), m.group("id"),
                             m.group("text"))
                prev["_col"] = m.start("text")
                out.append(prev)
                continue
        m = _CONT.match(line)
        if (m and prev is not None and s and i not in footer and not _END.search(line)
                and m.start("text") == prev.get("_col")):
            if "_dds" in prev:
                sev, text = dds_text[prev["_dds"]]
                dds_text[prev["_dds"]] = (sev, text + " " + m.group("text"))
            else:
                prev["text"] = " ".join((prev["text"] + " " + m.group("text")).split())
            continue
        prev = None
    if compiler == "dds":
        marked = set()
        for mid, seq in dds_marks:
            sev, text = dds_text.get(mid, (0, ""))
            sq, ln = _seq_line(seq)
            out.append(_diag(source, ln, sq if ln else None, 0, sev, mid, text))
            marked.add(mid)
        for mid, (sev, text) in dds_text.items():
            if mid not in marked:
                out.append(_diag(source, 0, None, 0, sev, mid, text))
    for d in out:
        d.pop("_col", None)
    out.sort(key=lambda d: (d["line"] == 0, -d["severity"], d["line"]))
    return {"compiler": compiler, "source": source, "diagnostics": out}


# QPJOBLOG: "RNS9310    Escape                  50   26-11-16  22:01:36.185013  QRN4CPP ..."
_JL_HEAD = re.compile(r"^(?P<id>%s|\*NONE)\s+(?P<type>Information|Completion|Diagnostic|Escape|"
                      r"Request|Inquiry|Notify|Reply|Sender copy|Status)\s+(?P<sev>\d\d)?\s+\d\d"
                      % _ID)
_JL_MSG = re.compile(r"^\s+Message \. \. \. \. :\s+-?(?P<text>.*)$")
_JL_MORE = re.compile(r"^\s+(Cause|Recovery|Technical description|From |To |Statement)")


def parse_joblog(lines: list[str]) -> list[dict]:
    """Messages of a spooled job log: id, type, severity, text (first level)."""
    out: list[dict] = []
    cur: dict | None = None
    in_text = False
    for raw in lines:
        m = _JL_HEAD.match(raw)
        if m:
            cur = {"id": None if m.group("id") == "*NONE" else m.group("id"),
                   "type": m.group("type"), "severity": int(m.group("sev") or 0), "text": ""}
            out.append(cur)
            in_text = False
            continue
        if cur is None:
            continue
        m = _JL_MSG.match(raw)
        if m:
            cur["text"], in_text = m.group("text").strip(), True
            continue
        if in_text:
            if _JL_MORE.match(raw) or not raw.startswith(" " * 30):
                in_text = False
            else:
                cur["text"] += " " + raw.strip()
    return out
