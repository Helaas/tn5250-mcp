"""5250 session engine: interprets the host data stream and builds replies.

Protocol behaviour follows the 5250 Functions Reference and the open-source
tn5250 emulator (lib5250/session.c), which was used as the reference.
"""

from __future__ import annotations

import itertools
import re
import threading
import time
from dataclasses import dataclass

from . import telnet as T
from .screen import (ADJUST_MANDATORY_FILL, ADJUST_RIGHT_BLANK, ADJUST_RIGHT_ZERO, FFW_MDT, Codepage, Field,
                     ScreenBuffer, describe_attr)

ESC = 0x04
# commands (after ESC)
CMD_CLEAR_UNIT, CMD_CLEAR_UNIT_ALT, CMD_CLEAR_FORMAT_TABLE = 0x40, 0x20, 0x50
CMD_WTD, CMD_WEC, CMD_WEC_WINDOW = 0x11, 0x21, 0x22
CMD_READ_INPUT, CMD_READ_MDT, CMD_READ_MDT_ALT = 0x42, 0x52, 0x82
CMD_READ_SCREEN_IMM, CMD_READ_IMM, CMD_READ_IMM_ALT = 0x62, 0x72, 0x83
CMD_SAVE_SCREEN, CMD_SAVE_PARTIAL, CMD_RESTORE_SCREEN, CMD_RESTORE_PARTIAL = 0x02, 0x03, 0x12, 0x13
CMD_ROLL, CMD_WSF = 0x23, 0xF3
READ_SCREEN_OTHER = {0x64, 0x66, 0x68, 0x6A, 0x6C}
# orders inside WTD
O_SOH, O_RA, O_EA, O_TD, O_SBA, O_WEA, O_IC, O_MC, O_WDSF, O_SF = (
    0x01, 0x02, 0x03, 0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x1D)
# CC2 bits
CC2_IC_ULOCK, CC2_UNLOCK, CC2_ALARM, CC2_MSG_OFF, CC2_MSG_ON = 0x40, 0x08, 0x04, 0x02, 0x01

NR_INVALID_COMMAND = 0x10030101
NR_INVALID_ROW_COL = 0x10050122
NR_INVALID_SF_CLASS = 0x10050111

AID = {"enter": 0xF1, "help": 0xF3, "pageup": 0xF4, "rolldown": 0xF4,
       "pagedown": 0xF5, "rollup": 0xF5, "print": 0xF6, "clear": 0xBD}
for _i in range(1, 13):
    AID["f%d" % _i] = 0x30 + _i
for _i in range(13, 25):
    AID["f%d" % _i] = 0xB0 + (_i - 12)
SPECIAL_KEYS = {"sysreq", "attn", "reset"}
ALL_KEYS = sorted(set(AID) | SPECIAL_KEYS)

# AIDs a real terminal sends without its own field checks: Clear, Help,
# Print and the roll keys (5494 functions reference; matches ACS).
NO_CHECK_AIDS = {0xBD, 0xF3, 0xF6, 0xF4, 0xF5}
FCW_SELF_CHECK_MOD10 = 0xB140
FCW_SELF_CHECK_MOD11 = 0xB1A0


def mod10_ok(digits: str) -> bool:
    """IBM modulus 10 self-check: weights 2,1,2,1... from the right, not
    counting the check digit; digits of the products are added."""
    body, check = digits[:-1], int(digits[-1])
    total = 0
    for k, ch in enumerate(reversed(body)):
        prod = int(ch) * (2 if k % 2 == 0 else 1)
        total += prod // 10 + prod % 10
    return (10 - total % 10) % 10 == check


def mod11_ok(digits: str) -> bool:
    """IBM modulus 11 self-check: weights 2..7 repeating from the right."""
    body, check = digits[:-1], int(digits[-1])
    total = sum(int(ch) * (2 + k % 6) for k, ch in enumerate(reversed(body)))
    r = total % 11
    want = 0 if r == 0 else 11 - r
    return want != 10 and want == check

SAVE_MAGIC = b"\x04\x11\x00\x00\x10\x00\x06T5"   # opaque to the host


class ProtocolError(Exception):
    pass


class FieldError(ValueError):
    pass


def norm_key(key: str) -> str:
    k = key.strip().lower().replace("_", "").replace("-", "").replace(" ", "")
    if k.startswith("pf") and k[2:].isdigit():
        k = "f" + k[2:]
    return k


@dataclass
class SavedScreen:
    buf: ScreenBuffer
    read_opcode: int
    keyboard: str
    inhibited: bool
    error_line: tuple[int, bytes] | None


class Session:
    def __init__(self, name: str, host: str, port: int, tls: bool = True,
                 verify_tls: bool = True, device_name: str | None = None,
                 wide: bool = False, codepage: str = "cp037", log=None):
        self.name, self.host, self.port, self.tls = name, host, port, tls
        self.verify_tls, self.device_name, self.wide = verify_tls, device_name, wide
        self.cp = Codepage(codepage)
        self.log = log or (lambda *a, **k: None)
        self.buf = ScreenBuffer(24, 80)
        self.cond = threading.Condition()
        self.conn: T.TelnetConnection | None = None
        self.connected = False
        self.last_error: str | None = None
        self.read_opcode = 0
        self.keyboard = "locked"          # locked | unlocked
        self.inhibited = False            # operator error (X II): needs Reset
        self.error_line: tuple[int, bytes] | None = None  # saved msg line during error
        self.message_waiting = False
        self.pending_ic: int | None = None
        self.host_seq = 0                 # bumps on every host update
        self.viewer_seq = 0               # bumps when the human edits in the viewer
        self.last_host_time = 0.0
        self.saved: dict[int, SavedScreen] = {}
        self._save_ids = itertools.count(1)
        self._reader: threading.Thread | None = None
        self.closing = False              # close() was asked for: no auto-reconnect
        self.on_drop = None               # called (from the reader) when the host drops us
        self.reconnecting: str | None = None   # e.g. "attempt 2 of 10", while it runs
        self.reconnects = 0
        self.last_reconnect: float | None = None   # wall clock of the last good reconnect
        self.recording_steps: int | None = None    # steps so far while a recorder runs
        self.profile = None                        # the Profile it was opened with, if any
        self.reconnect_note: str | None = None     # what happened after the last reconnect

    # ------------------------------------------------------------------ io
    def connect(self, timeout: float = 20.0) -> None:
        term = "IBM-3477-FC" if self.wide else "IBM-3179-2"
        env = {"TERM": term}
        if self.device_name:
            env["DEVNAME"] = self.device_name
        self.conn = T.TelnetConnection(self.host, self.port, self.tls, self.verify_tls,
                                       term, env, timeout=timeout)
        self.connected = True
        self._reader = threading.Thread(target=self._read_loop, name="5250-" + self.name,
                                        daemon=True)
        self._reader.start()

    def reconnect(self, timeout: float = 20.0) -> None:
        """Open a new connection for this session after the old one dropped.
        The screen starts empty again; the host sends its sign-on screen."""
        with self.cond:
            if self.conn:
                try:
                    self.conn.close()
                except OSError:
                    pass
            rows, cols = self.buf.rows, self.buf.cols
            self.buf = ScreenBuffer(rows, cols)
            self.read_opcode, self.keyboard, self.inhibited = 0, "locked", False
            self.error_line, self.pending_ic, self.message_waiting = None, None, False
            self.saved.clear()
            self.host_seq += 1
            self.closing = False
        self.connect(timeout=timeout)
        with self.cond:
            self.last_error = None
            self.cond.notify_all()

    def close(self) -> None:
        with self.cond:
            self.closing = True
            self.connected = False
            if self.conn:
                self.conn.close()
            self.cond.notify_all()

    def _read_loop(self) -> None:
        try:
            while True:
                rec = self.conn.read_record()
                with self.cond:
                    self._handle_record(rec)
                    self.host_seq += 1
                    self.last_host_time = time.monotonic()
                    self.cond.notify_all()
        except Exception as e:  # noqa: BLE001 - reader must report anything
            with self.cond:
                dropped = self.connected and not self.closing
                if self.connected:
                    self.last_error = "%s: %s" % (type(e).__name__, e)
                self.connected = False
                self.cond.notify_all()
            if dropped and self.on_drop:
                self.on_drop(self)

    def _send(self, data: bytes, opcode: int, flags: int = T.H_NONE) -> None:
        if not self.connected or not self.conn:
            raise ProtocolError("not connected")
        self.conn.send_record(data, opcode, flags)

    def _send_error(self, code: int) -> None:
        self._send(code.to_bytes(4, "big"), T.OP_NO_OP, T.H_ERR)

    # ------------------------------------------------------- host records
    def _handle_record(self, rec: T.Record) -> None:
        op = rec.opcode
        data = rec.data
        if op == T.OP_MESSAGE_ON:
            self.message_waiting = True
        elif op == T.OP_MESSAGE_OFF:
            self.message_waiting = False
        if op == T.OP_RESTORE_SCR and data.startswith(SAVE_MAGIC):
            sid = int.from_bytes(data[len(SAVE_MAGIC):len(SAVE_MAGIC) + 4], "big")
            self._restore(sid)
            data = data[len(SAVE_MAGIC) + 4:]
        if data:
            self._process_stream(memoryview(data))

    def _process_stream(self, d: memoryview) -> None:
        i = 0
        n = len(d)
        while i < n:
            if d[i] != ESC:
                self.log("protocol", "expected ESC at %d, got %02X" % (i, d[i]))
                return
            if i + 1 >= n:
                return
            cmd = d[i + 1]
            i += 2
            if cmd == CMD_CLEAR_UNIT:
                self._clear_unit(24, 80)
            elif cmd == CMD_CLEAR_UNIT_ALT:
                i += 1     # parameter byte (0x00 or 0x80)
                self._clear_unit(27, 132)
            elif cmd == CMD_CLEAR_FORMAT_TABLE:
                self.buf.clear_format_table()
                self.read_opcode = 0
            elif cmd == CMD_WTD:
                i = self._wtd(d, i)
            elif cmd in (CMD_WEC, CMD_WEC_WINDOW):
                if cmd == CMD_WEC_WINDOW:
                    i += 2
                i = self._wec(d, i)
            elif cmd in (CMD_READ_INPUT, CMD_READ_MDT, CMD_READ_MDT_ALT):
                cc1, cc2 = d[i], d[i + 1]
                i += 2
                self._cc1(cc1)
                self._cc2(cc2)
                if self.keyboard == "locked":
                    self.keyboard = "unlocked"
                self.read_opcode = cmd
            elif cmd == CMD_READ_IMM:
                old = self.read_opcode
                self.read_opcode = CMD_READ_IMM
                self._send_fields(0)
                self.read_opcode = old
            elif cmd == CMD_READ_SCREEN_IMM:
                self._send(bytes(self.buf.data), T.OP_NO_OP)
            elif cmd in READ_SCREEN_OTHER or cmd == CMD_READ_IMM_ALT:
                pass
            elif cmd in (CMD_SAVE_SCREEN, CMD_SAVE_PARTIAL):
                if cmd == CMD_SAVE_PARTIAL:
                    i += 5
                self._save()
            elif cmd in (CMD_RESTORE_SCREEN, CMD_RESTORE_PARTIAL):
                pass       # the data that follows is processed normally
            elif cmd == CMD_ROLL:
                self._roll(d[i], d[i + 1], d[i + 2])
                i += 3
            elif cmd == CMD_WSF:
                i = self._wsf(d, i)
            elif cmd == 0x0A:
                pass
            else:
                self.log("protocol", "unknown command %02X" % cmd)
                self._send_error(NR_INVALID_COMMAND)
                return

    def _clear_unit(self, rows: int, cols: int) -> None:
        self.buf.resize(rows, cols)
        self.read_opcode = 0
        self.pending_ic = None
        self.inhibited = False
        self.error_line = None

    def _cc1(self, cc1: int) -> None:
        mode = cc1 & 0xE0
        lock = mode != 0x00
        reset_nb_mdt = mode in (0x40, 0xA0, 0xC0)
        reset_all_mdt = mode in (0x60, 0xE0)
        null_nb_mdt = mode in (0x80, 0xC0)
        null_nb = mode in (0xA0, 0xE0)
        if lock:
            self.keyboard = "locked"
        for f in self.buf.fields:
            if not f.bypass and ((null_nb_mdt and f.modified) or null_nb):
                self.buf.data[f.start:f.start + f.length] = bytes(f.length)
            if reset_all_mdt or (reset_nb_mdt and not f.bypass):
                f.modified = False

    def _cc2(self, cc2: int) -> None:
        if cc2 & CC2_MSG_ON:
            self.message_waiting = True
        elif cc2 & CC2_MSG_OFF:
            self.message_waiting = False
        if cc2 & CC2_UNLOCK and self.keyboard == "locked":
            self.keyboard = "unlocked"

    def _home(self) -> int:
        if self.pending_ic is not None:
            return self.pending_ic
        for g in self.buf.input_fields():
            return g[0].start
        return 0

    def _wtd(self, d: memoryview, i: int) -> int:
        b = self.buf
        cc1, cc2 = d[i], d[i + 1]
        i += 2
        self._cc1(cc1)
        n = len(d)
        while i < n:
            o = d[i]
            if o == ESC:
                break
            i += 1
            if o == O_SBA:
                r, c = d[i], d[i + 1]
                i += 2
                if not (1 <= r <= b.rows and 1 <= c <= b.cols):
                    self._send_error(NR_INVALID_ROW_COL)
                    return n
                b.cursor = b.addr(r, c)
            elif o == O_SF:
                i = self._sf(d, i)
            elif o == O_SOH:
                ln = d[i]
                b.header = bytes(d[i + 1:i + 1 + ln])
                i += 1 + ln
                b.fields = []
                self.pending_ic = None
                self.keyboard = "locked"
            elif o == O_IC:
                r, c = d[i], d[i + 1]
                i += 2
                if 1 <= r <= b.rows and 1 <= c <= b.cols:
                    self.pending_ic = b.addr(r, c)
            elif o == O_MC:
                r, c = d[i], d[i + 1]
                i += 2
                if 1 <= r <= b.rows and 1 <= c <= b.cols:
                    b.user_cursor = b.addr(r, c)
            elif o == O_RA:
                r, c, ch = d[i], d[i + 1], d[i + 2]
                i += 3
                end = b.addr(r, c)
                if end < b.cursor:
                    self._send_error(NR_INVALID_ROW_COL)
                    return n
                while True:
                    at = b.cursor
                    b.put(ch)
                    if at == end:
                        break
            elif o == O_EA:
                r, c, ln = d[i], d[i + 1], d[i + 2]
                types = bytes(d[i + 3:i + 2 + ln])
                i += 2 + ln
                end = b.addr(r, c)
                if 0xFF in types or not types:
                    for a in range(b.cursor, min(end + 1, len(b.data))):
                        b.data[a] = 0
                b.cursor = (end + 1) % len(b.data)
            elif o == O_TD:
                ln = (d[i] << 8) | d[i + 1]
                i += 2
                for ch in d[i:i + ln]:
                    b.put(ch)
                i += ln
            elif o == O_WEA:
                i += 2
            elif o == O_WDSF:
                ln = (d[i] << 8) | d[i + 1]
                i += ln          # length includes the two length bytes
            else:
                b.put(o)
        if cc2 & CC2_UNLOCK and not cc2 & CC2_IC_ULOCK:
            b.user_cursor = self._home()
        elif self.pending_ic is not None:
            b.user_cursor = self.pending_ic
        self._cc2(cc2)
        return i

    def _sf(self, d: memoryview, i: int) -> int:
        b = self.buf
        x = d[i]
        i += 1
        ffw = None
        fcws: list[int] = []
        if (x & 0xE0) != 0x20:
            ffw = (x << 8) | d[i]
            i += 1
            x = d[i]
            i += 1
            while (x & 0xE0) != 0x20:
                fcws.append((x << 8) | d[i])
                i += 1
                x = d[i]
                i += 1
            self.keyboard = "locked"
        attr = x
        length = (d[i] << 8) | d[i + 1]
        i += 2
        b.put(attr)
        start = b.cursor
        if start + length > len(b.data):
            self._send_error(NR_INVALID_ROW_COL)
            return len(d)
        if ffw is not None:
            existing = next((f for f in b.fields if f.start == start), None)
            if existing:
                existing.ffw, existing.attr = ffw, attr
                existing.modified = bool(ffw & FFW_MDT)
            else:
                b.add_field(Field(
                    start=start, length=length, ffw=ffw, attr=attr, fcw=fcws,
                    modified=bool(ffw & FFW_MDT),
                    cont_first=0x8601 in fcws, cont_middle=0x8603 in fcws,
                    cont_last=0x8602 in fcws))
            end = (start + length) % len(b.data)
            if b.data[end] < 0x20 or b.data[end] > 0x3F:
                b.data[end] = 0x20
        return i

    def _wec(self, d: memoryview, i: int) -> int:
        b = self.buf
        msg = bytearray()
        n = len(d)
        while i < n and d[i] != ESC:
            if d[i] == O_IC and i + 2 < n:
                r, c = d[i + 1], d[i + 2]
                if 1 <= r <= b.rows and 1 <= c <= b.cols:
                    b.user_cursor = b.addr(r, c)
                i += 3
                continue
            msg.append(d[i])
            i += 1
        row = b.msg_line()
        start = row * b.cols
        if self.error_line is None:
            self.error_line = (row, bytes(b.data[start:start + b.cols]))
        line = bytes(msg[:b.cols]).ljust(b.cols, b"\x00")
        b.data[start:start + b.cols] = line
        self.inhibited = True
        return i

    def _roll(self, direction: int, top: int, bot: int) -> None:
        b = self.buf
        lines = direction & 0x1F
        if not direction & 0x80:
            lines = -lines
        if lines == 0 or not (1 <= top <= bot <= b.rows):
            return
        rows = [bytes(b.data[(r - 1) * b.cols:r * b.cols]) for r in range(top, bot + 1)]
        blank = bytes(b.cols)
        if lines > 0:
            rows = [blank] * lines + rows[:-lines]
        else:
            rows = rows[-lines:] + [blank] * (-lines)
        for k, r in enumerate(range(top, bot + 1)):
            b.data[(r - 1) * b.cols:r * b.cols] = rows[k]

    def _wsf(self, d: memoryview, i: int) -> int:
        ln = (d[i] << 8) | d[i + 1]
        cls, typ = d[i + 2], d[i + 3]
        nxt = i + ln
        if cls != 0xD9:
            self._send_error(NR_INVALID_SF_CLASS)
            return len(d)
        if typ in (0x70, 0x72):      # 5250 Query / Query Station State
            self._query_reply()
        return nxt

    def _query_reply(self) -> None:
        q = bytearray(61)
        q[0:5] = b"\x00\x00\x88\x00\x3a"
        q[5:13] = b"\xd9\x70\x80\x06\x00\x01\x01\x00"
        q[29] = 0x01
        dev = "3477" if self.wide else "3179"
        model = "FC" if self.wide else "02"
        q[30:34] = self.cp.encode(dev)
        q[34] = 0x00
        q[35:37] = self.cp.encode(model)
        q[37] = 0x02
        q[41:43] = b"\x61\x50"
        q[44:46] = b"\xff\xff"
        q[49:51] = b"\x23\x31"
        self._send(bytes(q), T.OP_NO_OP)

    def _save(self) -> None:
        sid = next(self._save_ids)
        self.saved[sid] = SavedScreen(self.buf.snapshot(), self.read_opcode, self.keyboard,
                                      self.inhibited, self.error_line)
        if len(self.saved) > 64:
            del self.saved[min(self.saved)]
        self._send(SAVE_MAGIC + sid.to_bytes(4, "big"), T.OP_SAVE_SCR)

    def _restore(self, sid: int) -> None:
        s = self.saved.get(sid)
        if not s:
            self.log("protocol", "restore of unknown screen %d" % sid)
            return
        self.buf = s.buf.snapshot()
        self.read_opcode, self.keyboard = s.read_opcode, s.keyboard
        self.inhibited, self.error_line = s.inhibited, s.error_line

    # ------------------------------------------------------- client → host
    def _field_bytes_mdt(self, group: list[Field], keep_nulls: bool) -> bytes:
        data = bytearray()
        for f in group:
            data += self.buf.data[f.start:f.start + f.length]
        if len(group) == 1 and group[0].signed and data:
            sign = data[-1]
            data = data[:-1]
            if data and sign == 0x60 and 0xF0 <= data[-1] <= 0xF9:
                data[-1] = 0xD0 | (data[-1] & 0x0F)
        data = data.rstrip(b"\x00")
        if not keep_nulls:
            data = data.replace(b"\x00", b"\x40")
        return bytes(data)

    def _field_bytes_all(self, group: list[Field]) -> bytes:
        data = bytearray()
        for f in group:
            data += self.buf.data[f.start:f.start + f.length]
        data = bytearray(b if b else 0x40 for b in data)
        if len(group) == 1 and group[0].signed and len(data) >= 2:
            neg = data[-1] == 0x60
            c = data[-2]
            data[-1] = (0xD0 | (c & 0x0F)) if neg else c
        return bytes(data)

    def _send_fields(self, aid: int) -> None:
        b = self.buf
        row, col = b.rowcol(b.user_cursor)
        out = bytearray([row, col, aid])
        groups = b.input_fields()
        any_mdt = any(f.modified for g in groups for f in g)
        op = self.read_opcode
        if op in (CMD_READ_INPUT, CMD_READ_IMM):
            if any_mdt and (op == CMD_READ_IMM or b.send_data_for_aid(aid)):
                for g in groups:
                    out += self._field_bytes_all(g)
        elif op in (CMD_READ_MDT, CMD_READ_MDT_ALT):
            if b.send_data_for_aid(aid):
                for g in groups:
                    if any(f.modified for f in g):
                        r, c = b.rowcol(g[0].start)
                        out += bytes([O_SBA, r, c])
                        out += self._field_bytes_mdt(g, keep_nulls=op == CMD_READ_MDT_ALT)
        else:
            raise ProtocolError("host is not waiting for input")
        self.read_opcode = 0
        self.keyboard = "locked"
        self._send(bytes(out), T.OP_PUT_GET)

    # ------------------------------------------------------- public API
    def _check_open(self) -> None:
        if not self.connected:
            if self.reconnecting:
                raise ProtocolError("session %r lost its connection and is reconnecting (%s); "
                                    "call wait, then sign on again" % (self.name,
                                                                       self.reconnecting))
            raise ProtocolError("session %r is not connected%s" % (
                self.name, ": " + self.last_error if self.last_error else ""))

    def screen_id(self) -> str:
        return "%d.%d" % (self.host_seq, self.viewer_seq)

    def ready(self) -> bool:
        return self.read_opcode != 0 and (self.keyboard == "unlocked" or self.inhibited)

    def keyboard_state(self) -> str:
        if not self.connected:
            return "disconnected"
        if self.inhibited:
            return "error"         # X II - needs Reset
        if self.ready():
            return "ready"
        return "busy"              # X SYSTEM - host working

    def wait(self, timeout: float = 30.0, text: str | None = None,
             settle: float = 0.15) -> bool:
        """Block until the host waits for input (and `text` is on screen).
        Returns False on timeout."""
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                if not self.connected and not self.reconnecting:
                    return True
                now = time.monotonic()
                if self.connected and self.ready() and (
                        text is None or text in "\n".join(self.rows())):
                    quiet = now - self.last_host_time
                    if quiet >= settle:
                        return True
                    self.cond.wait(settle - quiet)
                    continue
                if now >= deadline:
                    return False
                self.cond.wait(min(deadline - now, 0.5))

    def rows(self, reveal_hidden: bool = False) -> list[str]:
        return self.buf.text_rows(self.cp, reveal_hidden)

    def resolve(self, ref: str) -> list[Field]:
        groups = self.buf.input_fields()
        ref = str(ref).strip()
        if ref == "@cursor":
            a = self.buf.user_cursor
            for g in groups:
                if any(f.start <= a < f.start + f.length for f in g):
                    return g
            raise FieldError("the cursor (%d,%d) is not in an input field"
                             % self.buf.rowcol(a))
        m = re.fullmatch(r"(\d+)\s*,\s*(\d+)", ref)
        if m:
            a = self.buf.addr(int(m.group(1)), int(m.group(2)))
            for g in groups:
                if any(f.start <= a < f.start + f.length for f in g):
                    return g
            raise FieldError("no input field at row %s col %s" % m.groups())
        m = re.fullmatch(r"#(\d+)", ref)
        if m:
            k = int(m.group(1))
            if 1 <= k <= len(groups):
                return groups[k - 1]
            raise FieldError("there is no input field #%d (screen has %d)" % (k, len(groups)))
        rows = self.rows()
        labels = [(g, self.buf.label_for(g[0], rows)) for g in groups]
        exact = [g for g, lab in labels if lab.lower() == ref.lower()]
        if len(exact) == 1:
            return exact[0]
        part = [g for g, lab in labels if ref.lower() in lab.lower()]
        if len(exact) > 1 or len(part) > 1:
            where = ", ".join("%d,%d" % self.buf.rowcol(g[0].start) for g in (exact or part))
            raise FieldError("label %r matches several fields (%s); use row,col" % (ref, where))
        if len(part) == 1:
            return part[0]
        raise FieldError("no input field labelled %r" % ref)

    def encode_value(self, group: list[Field], value: str) -> bytes:
        f0 = group[0]
        total = sum(f.length for f in group)
        if f0.monocase:
            value = value.upper()
        if value == "":
            return bytes(total)
        shift = f0.shift
        if f0.signed and len(group) == 1:
            m = re.fullmatch(r"\s*([+-]?)\s*([0-9.,]*)\s*([+-]?)\s*", value)
            if not m or not m.group(2):
                raise FieldError("field %d,%d is signed numeric; %r is not a number"
                                 % (*self.buf.rowcol(f0.start), value))
            neg = "-" in (m.group(1) + m.group(3))
            digits = self.cp.encode(m.group(2))
            if len(digits) > total - 1:
                raise FieldError("value %r is too long for field %d,%d (max %d digits)"
                                 % (value, *self.buf.rowcol(f0.start), total - 1))
            fill = 0xF0 if f0.adjust == ADJUST_RIGHT_ZERO else 0x40
            return digits.rjust(total - 1, bytes([fill])) + (b"\x60" if neg else b"\x00")
        if shift == "numeric_only" and not re.fullmatch(r"[0-9+\-,. ]*", value):
            raise FieldError("field %d,%d accepts numbers only (operator error 0009)"
                             % self.buf.rowcol(f0.start))
        if shift == "digits_only" and not value.isdigit():
            raise FieldError("field %d,%d accepts digits only (operator error 0010)"
                             % self.buf.rowcol(f0.start))
        if shift == "alpha_only" and not all(ch.isalpha() or ch in ",.- " for ch in value):
            raise FieldError("field %d,%d accepts letters, comma, period, dash and blank only "
                             "(operator error 0008)" % self.buf.rowcol(f0.start))
        enc = self.cp.encode(value)
        if len(enc) > total:
            raise FieldError("value is %d characters; field %d,%d holds %d"
                             % (len(enc), *self.buf.rowcol(f0.start), total))
        if f0.adjust in (ADJUST_RIGHT_ZERO, ADJUST_RIGHT_BLANK):
            fill = 0xF0 if f0.adjust == ADJUST_RIGHT_ZERO else 0x40
            return enc.rjust(total, bytes([fill]))
        return enc.ljust(total, b"\x00")

    def fill(self, values: dict[str, str], from_viewer: bool = False) -> list[list[Field]]:
        """Put values into input fields (no host traffic). All-or-nothing."""
        with self.cond:
            self._check_open()
            planned = []
            for ref, value in values.items():
                g = self.resolve(ref)
                planned.append((g, self.encode_value(g, value)))
            for g, data in planned:
                k = 0
                for f in g:
                    self.buf.data[f.start:f.start + f.length] = data[k:k + f.length]
                    k += f.length
                    f.modified = True
            if from_viewer:
                self.viewer_seq += 1
            self.cond.notify_all()
            return [g for g, _ in planned]

    def set_cursor(self, ref: str, from_viewer: bool = False) -> None:
        with self.cond:
            m = re.fullmatch(r"\s*(\d+)\s*,\s*(\d+)\s*", str(ref))
            if m:
                r, c = int(m.group(1)), int(m.group(2))
                if not (1 <= r <= self.buf.rows and 1 <= c <= self.buf.cols):
                    raise FieldError("cursor %d,%d is off screen" % (r, c))
                self.buf.user_cursor = self.buf.addr(r, c)
            else:
                self.buf.user_cursor = self.resolve(ref)[0].start
            if from_viewer:
                self.viewer_seq += 1

    def press(self, key: str) -> None:
        """Send an AID key (or handle Reset/SysReq/Attn). Does not wait."""
        k = norm_key(key)
        with self.cond:
            self._check_open()
            if k == "reset":
                if self.error_line is not None:
                    row, line = self.error_line
                    s = row * self.buf.cols
                    self.buf.data[s:s + self.buf.cols] = line
                self.error_line = None
                self.inhibited = False
                self.host_seq += 1
                self.cond.notify_all()
                return
            if k == "sysreq":
                # the host may or may not answer (e.g. not at sign-on), so the
                # keyboard is not locked, as in tn5250
                self._send(b"", T.OP_NO_OP, T.H_SRQ)
                return
            if k == "attn":
                self.keyboard = "locked"
                self._send(b"", T.OP_NO_OP, T.H_ATN)
                return
            if k not in AID:
                raise FieldError("unknown key %r; keys: %s" % (key, ", ".join(ALL_KEYS)))
            if self.inhibited:
                raise ProtocolError("keyboard is in error state (X II); press Reset first "
                                    "and read the message line")
            if not self.ready():
                raise ProtocolError("host is busy (keyboard locked); call wait first")
            self._check_fields(AID[k])
            self._send_fields(AID[k])
            self.cond.notify_all()

    def _check_fields(self, aid: int) -> None:
        """The checks a real 5250 terminal makes before it sends an AID with
        field data: mandatory entry, mandatory fill and self-check digits.
        On a violation nothing is sent, the cursor goes to the field and
        FieldError says what to fix (with the standard operator error code)."""
        b = self.buf
        if aid in NO_CHECK_AIDS or not b.send_data_for_aid(aid):
            return
        for g in b.input_fields():
            f0 = g[0]
            modified = any(f.modified for f in g)
            raw = b"".join(bytes(b.data[f.start:f.start + f.length]) for f in g)
            r, c = b.rowcol(f0.start)
            problem = None
            if f0.mandatory_entry and not modified:
                problem = ("field %d,%d must be filled in before %s (mandatory entry, "
                           "operator error 0007)")
            elif f0.adjust == ADJUST_MANDATORY_FILL and modified:
                used = raw.rstrip(b"\x00\x40")
                if used and len(used) < len(raw):
                    problem = ("field %d,%d must be filled to its full length of " + str(len(raw))
                               + " or left empty before %s (mandatory fill, operator error 0014)")
            if problem is None and modified and (FCW_SELF_CHECK_MOD10 in f0.fcw
                                                 or FCW_SELF_CHECK_MOD11 in f0.fcw):
                text = b.field_text(g, self.cp).strip()
                if text:
                    ok = text.isdigit() and len(text) > 1 and (
                        mod10_ok(text) if FCW_SELF_CHECK_MOD10 in f0.fcw else mod11_ok(text))
                    if not ok:
                        problem = ("field %d,%d: the check digit of " + repr(text)
                                   + " is wrong; nothing was sent with %s (self-check, "
                                   "operator error 0015)")
            if problem:
                b.user_cursor = f0.start
                name = next((n for n, v in AID.items() if v == aid), "the key")
                raise FieldError(problem % (r, c, name.capitalize() if len(name) > 3
                                            else name.upper()))

    def on_signon_screen(self) -> bool:
        """A sign-on screen: a hidden (password) field next to a visible one,
        and 'password' or 'sign on' on screen (custom screens included)."""
        with self.cond:
            fields = self.buf.input_fields()
            text = "\n".join(self.rows()).lower()
            return (any(g[0].hidden for g in fields) and any(not g[0].hidden for g in fields)
                    and ("password" in text or "sign on" in text))

    def command_line(self) -> str | None:
        """The CL command line of this screen ("row,col"): an input field right
        after "===>" whose label mentions "command" (menus: "Selection or
        command", list panels: "Parameters or command", Command Entry). The
        STRSQL statement line and other "===>" prompts don't count."""
        with self.cond:
            rows = self.rows()
            for g in self.buf.input_fields():
                f0 = g[0]
                if f0.hidden:
                    continue
                r, c = self.buf.rowcol(f0.start)
                if not rows[r - 1][:c - 1].rstrip().endswith("===>"):
                    continue
                label = self.buf.label_for(f0, rows).lower()
                above = rows[r - 2].lower() if r >= 2 else ""
                if "command" in label or "command" in above:
                    return "%d,%d" % (r, c)
            return None

    def _row_segments(self, group: list[Field]) -> list[dict]:
        """Split a logical field into on-screen pieces, one per row (a long
        field wraps onto the next row)."""
        segs = []
        cols = self.buf.cols
        for f in group:
            a, left = f.start, f.length
            while left > 0:
                r, c = self.buf.rowcol(a)
                n = min(left, cols - c + 1)
                segs.append({"row": r, "col": c, "length": n})
                a += n
                left -= n
        return segs

    def snapshot(self, reveal_hidden_text: bool = False) -> dict:
        """Everything a client needs to show the screen."""
        with self.cond:
            b = self.buf
            rows = self.rows()
            crow, ccol = b.rowcol(b.user_cursor)
            fields = []
            for idx, g in enumerate(b.input_fields(), start=1):
                f0 = g[0]
                r, c = b.rowcol(f0.start)
                val = b.field_text(g, self.cp)
                filled = val.strip() != ""
                fields.append({
                    "index": idx,
                    "id": "%d,%d" % (r, c),
                    "row": r, "col": c,
                    "length": sum(f.length for f in g),
                    "segments": self._row_segments(g),
                    "label": b.label_for(f0, rows),
                    "type": f0.shift,
                    "hidden": f0.hidden,
                    "value": ("" if f0.hidden and not reveal_hidden_text else val.rstrip()),
                    "has_value": filled,
                    "modified": any(f.modified for f in g),
                    "mandatory_entry": f0.mandatory_entry,
                    "mandatory_fill": f0.adjust == 7,
                    "right_adjust": {5: "zero", 6: "blank"}.get(f0.adjust),
                    "monocase": f0.monocase,
                    "auto_enter": bool(f0.ffw & 0x80),
                    "attr": describe_attr(f0.attr),
                })
            return {
                "session": self.name,
                "host": self.host,
                "screen_id": self.screen_id(),
                "rows": b.rows, "cols": b.cols,
                "text": rows,
                "cursor": {"row": crow, "col": ccol},
                "keyboard": self.keyboard_state(),
                "message_waiting": self.message_waiting,
                "message_line": rows[b.msg_line()].strip(),
                "fields": fields,
                "error": self.last_error,
                "reconnecting": self.reconnecting,
                "reconnects": self.reconnects,
                "last_reconnect": self.last_reconnect,
                "recording": self.recording_steps,
                "profile": self.profile.name if self.profile else None,
                "reconnect_note": self.reconnect_note,
            }

    def cell_attrs(self) -> list[int]:
        with self.cond:
            return self.buf.cell_attrs()
