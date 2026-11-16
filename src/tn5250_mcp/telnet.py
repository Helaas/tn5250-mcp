"""TN5250 transport: telnet negotiation (RFC 1205 / RFC 4777) and 5250 records.

A 5250 record on the wire is a GDS header followed by 5250 data, terminated
by IAC EOR. Header layout (10 bytes for the usual 4-byte variable header):

    0-1  record length (including these two bytes)
    2-3  record type, always 0x12A0
    4-5  flow type (0x0000 = display)
    6    variable header length (normally 4)
    7    flags (ERR 0x80, ATN 0x40, SRQ 0x04, TRQ 0x02, HLP 0x01)
    8    reserved
    9    opcode
"""

from __future__ import annotations

import socket
import ssl
from dataclasses import dataclass

IAC, DONT, DO, WONT, WILL, SB, SE, EOR = 255, 254, 253, 252, 251, 250, 240, 239
OPT_BINARY, OPT_TIMING_MARK, OPT_TERMINAL_TYPE, OPT_EOR, OPT_NEW_ENVIRON = 0, 6, 24, 25, 39
TT_IS, TT_SEND = 0, 1
ENV_VAR, ENV_VALUE, ENV_USERVAR = 0, 1, 3

ACCEPTED_OPTS = {OPT_BINARY, OPT_TERMINAL_TYPE, OPT_EOR, OPT_NEW_ENVIRON}

# record opcodes
OP_NO_OP, OP_INVITE, OP_OUTPUT_ONLY, OP_PUT_GET = 0, 1, 2, 3
OP_SAVE_SCR, OP_RESTORE_SCR, OP_READ_IMMED, OP_READ_SCR = 4, 5, 6, 8
OP_CANCEL_INVITE, OP_MESSAGE_ON, OP_MESSAGE_OFF = 10, 11, 12

# record header flags
H_NONE, H_ERR, H_ATN, H_SRQ, H_TRQ, H_HLP = 0x00, 0x80, 0x40, 0x04, 0x02, 0x01


@dataclass
class Record:
    flowtype: int
    flags: int
    opcode: int
    data: bytes

    @classmethod
    def parse(cls, raw: bytes) -> "Record":
        if len(raw) < 10 or raw[2:4] != b"\x12\xa0":
            raise ValueError("not a 5250 GDS record: %r" % raw[:12])
        varhdr = raw[6]
        return cls(
            flowtype=(raw[4] << 8) | raw[5],
            flags=raw[7],
            opcode=raw[9],
            data=bytes(raw[6 + varhdr:]),
        )


def build_record(data: bytes, opcode: int, flags: int = H_NONE, flowtype: int = 0) -> bytes:
    length = len(data) + 10
    hdr = bytes([length >> 8, length & 0xFF, 0x12, 0xA0, flowtype >> 8, flowtype & 0xFF,
                 4, flags, 0, opcode])
    body = (hdr + data).replace(b"\xff", b"\xff\xff")
    return body + bytes([IAC, EOR])


class TelnetConnection:
    """Blocking TN5250 connection. `read_record()` returns the next 5250
    record, handling telnet negotiation transparently."""

    def __init__(self, host: str, port: int, use_tls: bool, verify_tls: bool,
                 terminal_type: str, env: dict[str, str], timeout: float = 20.0):
        self.host, self.port = host, port
        self.terminal_type = terminal_type
        self.env = env
        raw = socket.create_connection((host, port), timeout=timeout)
        if use_tls:
            ctx = ssl.create_default_context()
            if not verify_tls:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            raw = ctx.wrap_socket(raw, server_hostname=host)
        raw.settimeout(None)
        self.sock = raw
        self._buf = bytearray()
        self._pos = 0
        self.closed = False

    # -- low level ---------------------------------------------------------
    def _recv_byte(self) -> int:
        if self._pos >= len(self._buf):
            chunk = self.sock.recv(65536)
            if not chunk:
                self.closed = True
                raise ConnectionError("host closed the connection")
            self._buf = bytearray(chunk)
            self._pos = 0
        b = self._buf[self._pos]
        self._pos += 1
        return b

    def _send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def send_record(self, data: bytes, opcode: int, flags: int = H_NONE) -> None:
        self._send(build_record(data, opcode, flags))

    def close(self) -> None:
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass

    # -- negotiation -------------------------------------------------------
    def _verb(self, verb: int, opt: int) -> None:
        if verb == DO:
            reply = WILL if opt in ACCEPTED_OPTS else WONT
        elif verb == WILL:
            reply = DO if opt in ACCEPTED_OPTS else DONT
        else:
            return
        self._send(bytes([IAC, reply, opt]))

    def _subneg(self, sb: bytes) -> None:
        if not sb:
            return
        if sb[0] == OPT_TERMINAL_TYPE and len(sb) > 1 and sb[1] == TT_SEND:
            self._send(bytes([IAC, SB, OPT_TERMINAL_TYPE, TT_IS])
                       + self.terminal_type.encode("ascii") + bytes([IAC, SE]))
        elif sb[0] == OPT_NEW_ENVIRON and len(sb) > 1 and sb[1] == TT_SEND:
            out = bytearray([IAC, SB, OPT_NEW_ENVIRON, TT_IS])
            for k, v in self.env.items():
                out += bytes([ENV_VAR]) + k.encode("ascii") + bytes([ENV_VALUE]) + v.encode("ascii")
            out += bytes([IAC, SE])
            self._send(bytes(out))

    def read_record(self) -> Record:
        rec = bytearray()
        while True:
            b = self._recv_byte()
            if b != IAC:
                rec.append(b)
                continue
            cmd = self._recv_byte()
            if cmd == IAC:
                rec.append(IAC)
            elif cmd == EOR:
                if rec:
                    return Record.parse(bytes(rec))
            elif cmd in (DO, DONT, WILL, WONT):
                self._verb(cmd, self._recv_byte())
            elif cmd == SB:
                sb = bytearray()
                while True:
                    c = self._recv_byte()
                    if c == IAC:
                        c2 = self._recv_byte()
                        if c2 == SE:
                            break
                        sb.append(c2)
                    else:
                        sb.append(c)
                self._subneg(bytes(sb))
            # other telnet commands (NOP, GA, ...) are ignored
