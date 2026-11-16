"""Offline tests: feed hand-built 5250 data streams into a Session and check
the screen, the field table and the bytes sent back to the host."""

import pytest

from tn5250_mcp import telnet as T
from tn5250_mcp.guard import Guard
from tn5250_mcp.manager import GuardError, Manager
from tn5250_mcp.session import FieldError, ProtocolError, Session

E = lambda s: s.encode("cp037")  # noqa: E731


class FakeConn:
    def __init__(self):
        self.sent = []

    def send_record(self, data, opcode, flags=0):
        self.sent.append((data, opcode, flags))

    def close(self):
        pass


def make():
    s = Session("t", "example", 992)
    s.conn = FakeConn()
    s.connected = True
    return s


def feed(s, data: bytes, opcode=T.OP_PUT_GET):
    with s.cond:
        s._handle_record(T.Record(0, 0, opcode, data))
        s.host_seq += 1


def sba(r, c):
    return bytes([0x11, r, c])


def sf(ffw: int, attr: int, length: int, fcw: bytes = b""):
    return bytes([0x1D, ffw >> 8, ffw & 0xFF]) + fcw + bytes([attr, length >> 8, length & 0xFF])


def entry_screen(header=b"\x00\x00\x00\x18"):
    """Clear unit, a label, three input fields (alpha, right-adjust numeric,
    signed numeric), then Read MDT Fields."""
    soh = bytes([0x01, len(header)]) + header
    return (b"\x04\x40" + b"\x04\x11\x00\x08" + soh
            + sba(1, 2) + b"\x20" + E("ORDER ENTRY")
            + sba(4, 1) + E("Name:") + sba(4, 9) + sf(0x4000, 0x24, 10)
            + sba(5, 1) + E("Qty:") + sba(5, 9) + sf(0x4206, 0x24, 5)
            + sba(6, 1) + E("Amount:") + sba(6, 9) + sf(0x4700, 0x24, 6)
            + bytes([0x13, 4, 10])                 # IC: cursor to row 4 col 10
            + b"\x04\x52\x00\x00")


def test_screen_and_fields():
    s = make()
    feed(s, entry_screen())
    snap = s.snapshot()
    assert "ORDER ENTRY" in snap["text"][0]
    assert snap["keyboard"] == "ready"
    assert [f["id"] for f in snap["fields"]] == ["4,10", "5,10", "6,10"]
    assert [f["label"] for f in snap["fields"]] == ["Name", "Qty", "Amount"]
    assert snap["fields"][1]["right_adjust"] == "blank"
    assert snap["fields"][2]["type"] == "signed_numeric"
    assert snap["cursor"] == {"row": 4, "col": 10}


def test_read_mdt_reply_encodes_fields():
    s = make()
    feed(s, entry_screen())
    s.fill({"Name": "Widget", "Qty": "25", "#3": "-3"})
    s.press("Enter")
    data, opcode, flags = s.conn.sent[-1]
    assert opcode == T.OP_PUT_GET
    assert data[:3] == bytes([4, 10, 0xF1])            # cursor + Enter AID
    body = data[3:]
    assert body.startswith(sba(4, 10) + E("Widget"))
    assert sba(5, 10) + E("   25") in body           # right-adjusted, blank fill
    # signed numeric: sign position dropped, last digit zone D = negative
    assert body.endswith(sba(6, 10) + E("    ") + b"\xd3")
    assert s.keyboard_state() == "busy"


def test_unmodified_fields_not_sent_and_aid_mask():
    # header byte 6 bit for F3 set -> F3 must not send field data
    s = make()
    feed(s, entry_screen(header=b"\x00\x00\x00\x18\x00\x00\x20"))
    s.fill({"Name": "X"})
    s.press("F3")
    data = s.conn.sent[-1][0]
    assert data == bytes([4, 10, 0x33])


def test_validation():
    s = make()
    feed(s, entry_screen())
    with pytest.raises(FieldError):
        s.fill({"Name": "this is far too long"})
    with pytest.raises(FieldError):
        s.fill({"Amount": "abc"})
    with pytest.raises(FieldError):
        s.fill({"nope": "x"})


def test_error_code_inhibits_until_reset():
    s = make()
    feed(s, entry_screen())
    feed(s, b"\x04\x21" + b"\x22" + E("Function key not allowed.") + b"\x04\x52\x00\x00")
    assert s.keyboard_state() == "error"
    assert "Function key not allowed" in s.snapshot()["message_line"]
    with pytest.raises(ProtocolError):
        s.press("Enter")
    s.press("Reset")
    assert s.keyboard_state() == "ready"
    assert "Function key" not in s.snapshot()["message_line"]


def test_save_and_restore_screen():
    s = make()
    feed(s, entry_screen())
    feed(s, b"\x04\x02")                                   # save screen
    saved, opcode, _ = s.conn.sent[-1]
    assert opcode == T.OP_SAVE_SCR
    feed(s, b"\x04\x40\x04\x11\x00\x08" + sba(1, 1) + E("POPUP") + b"\x04\x52\x00\x00")
    assert "POPUP" in s.snapshot()["text"][0]
    feed(s, saved, opcode=T.OP_RESTORE_SCR)                # host hands it back
    assert "ORDER ENTRY" in s.snapshot()["text"][0]
    assert len(s.snapshot()["fields"]) == 3


def test_query_reply():
    s = make()
    feed(s, b"\x04\xf3\x00\x05\xd9\x70\x00")
    data, opcode, _ = s.conn.sent[-1]
    assert opcode == T.OP_NO_OP
    assert data[2] == 0x88 and data[5:7] == b"\xd9\x70"
    assert len(data) == 61


def test_iac_doubled_on_send():
    rec = T.build_record(b"\xff\x01", T.OP_PUT_GET)
    assert rec.endswith(b"\xff\xff\x01\xff\xef")


def test_hidden_field_never_shown():
    s = make()
    feed(s, b"\x04\x40\x04\x11\x00\x08" + sba(5, 1) + E("Password:")
         + sba(5, 12) + sf(0x4000, 0x27, 10) + b"\x04\x52\x00\x00")
    s.fill({"Password": "secret"})
    snap = s.snapshot()
    assert snap["fields"][0]["hidden"] and snap["fields"][0]["value"] == ""
    assert "secret".upper() not in "".join(snap["text"]).upper()


def test_guard_checks():
    mgr = Manager(Guard())
    s = make()
    mgr.sessions["t"] = s
    feed(s, entry_screen())
    with pytest.raises(GuardError, match="pass `expect`"):
        mgr.fill(s, {"Name": "x"})
    with pytest.raises(GuardError, match="expected"):
        mgr.fill(s, {"Name": "x"}, expect="MAIN MENU")
    with pytest.raises(GuardError, match="screen changed"):
        mgr.fill(s, {"Name": "x"}, screen_id="0.0")
    with pytest.raises(GuardError, match="blocked command"):
        mgr.fill(s, {"Name": "dltlib x"}, expect="ORDER")
    mgr.fill(s, {"Name": "ok"}, expect="1:ORDER ENTRY")
    mgr.fill(s, {"Name": "ok2"}, screen_id=s.screen_id())
    # the human typing in the viewer invalidates the agent's screen_id
    sid = s.screen_id()
    mgr.fill(s, {"Name": "human"}, from_viewer=True)
    with pytest.raises(GuardError, match="screen changed"):
        mgr.fill(s, {"Name": "agent"}, screen_id=sid)


def test_blocklist_word_boundaries():
    g = Guard()
    g.check_values({"a": "DLTF FILE(MYLIB/T5250TEST)"})       # allowed
    g.check_values({"a": "WRKLIBPDM"})                       # not DLTLIB
    with pytest.raises(PermissionError):
        g.check_values({"a": "  pwrdwnsys option(*immed)"})
    with pytest.raises(PermissionError):
        g.check_values({"a": "drop  schema foo"})


def test_label_from_heading_above():
    # MAIN menu: "Selection or command" on row 19, command line field at 20,7
    s = make()
    feed(s, b"\x04\x40\x04\x11\x00\x08" + sba(19, 2) + E("Selection or command")
         + sba(20, 2) + E("===>") + sba(20, 6) + sf(0x4000, 0x24, 153) + b"\x04\x52\x00\x00")
    f = s.snapshot()["fields"][0]
    assert f["label"] == "Selection or command"
    assert [(g["row"], g["col"], g["length"]) for g in f["segments"]] == [(20, 7, 74), (21, 1, 79)]


def test_viewer_falls_back_when_port_taken():
    import socket
    import urllib.request
    from tn5250_mcp.viewer import start_viewer
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    taken = busy.getsockname()[1]
    url = start_viewer(Manager(Guard()), taken)
    port = int(url.split(":")[2].split("/")[0])
    assert port != taken
    with urllib.request.urlopen(url) as r:          # Host header matches the new port
        assert r.status == 200
    busy.close()
