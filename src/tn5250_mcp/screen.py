"""5250 display buffer, field format table and rendering.

The buffer holds raw bytes exactly as the host wrote them: EBCDIC characters
(0x40-0xFE), screen attributes (0x20-0x3F) and nulls (0x00). An attribute byte
occupies a screen position (shown as a blank) and applies to everything after
it until the next attribute byte.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field as dc_field

# --- field format word (FFW) bits ----------------------------------------
FFW_BYPASS = 0x2000
FFW_DUP = 0x1000
FFW_MDT = 0x0800
FFW_SHIFT_MASK = 0x0700
FFW_AUTO_ENTER = 0x0080
FFW_FER = 0x0040
FFW_MONOCASE = 0x0020
FFW_MANDATORY_ENTRY = 0x0008
FFW_ADJUST_MASK = 0x0007

SHIFT_NAMES = {
    0x000: "alpha", 0x100: "alpha_only", 0x200: "numeric_shift", 0x300: "numeric_only",
    0x400: "katakana", 0x500: "digits_only", 0x600: "mag_reader", 0x700: "signed_numeric",
}
ADJUST_RIGHT_ZERO, ADJUST_RIGHT_BLANK, ADJUST_MANDATORY_FILL = 5, 6, 7

NONDISPLAY_ATTRS = {0x27, 0x2F, 0x37, 0x3F}

# colour of each attribute byte 0x20-0x3F (the 5250 colour mapping)
_ATTR_COLOR = (["green"] * 7 + ["none"]       # 0x20-0x27
               + ["red"] * 7 + ["none"]       # 0x28-0x2F
               + ["turquoise", "turquoise", "yellow", "yellow", "turquoise",
                  "turquoise", "yellow", "none"]  # 0x30-0x37
               + ["pink", "pink", "blue", "blue", "pink", "pink", "blue", "none"])  # 0x38-0x3F
# 0x22/0x23/0x26 render white (high intensity green)
_WHITE = {0x22, 0x23, 0x26}
_RED_BLINK = {0x2A, 0x2B, 0x2E}


def describe_attr(a: int) -> dict:
    """Return display properties for an attribute byte (0x20-0x3F)."""
    if a in NONDISPLAY_ATTRS:
        return {"color": "none", "hidden": True, "underline": False, "reverse": False,
                "colsep": False, "blink": False}
    color = "white" if a in _WHITE else _ATTR_COLOR[a - 0x20]
    return {
        "color": color,
        "hidden": False,
        "reverse": bool(a & 0x01),
        "underline": bool(a & 0x04) and a not in (0x2E,),
        "colsep": 0x30 <= a <= 0x37 or (a & 0x10 and a < 0x38),
        "blink": a in _RED_BLINK,
    }


class Codepage:
    """Single-byte EBCDIC code page using Python's codecs (cp037, cp273,
    cp500, cp1140, ...)."""

    def __init__(self, name: str = "cp037"):
        self.name = name
        table = []
        for i in range(256):
            if i < 0x40:
                table.append(" ")
                continue
            ch = bytes([i]).decode(name, errors="replace")
            table.append(ch if ch.isprintable() else " ")
        self.to_char = table

    def encode(self, text: str) -> bytes:
        try:
            return text.encode(self.name)
        except UnicodeEncodeError as e:
            raise ValueError("character %r can't be sent in code page %s"
                             % (text[e.start:e.end], self.name)) from None

    def decode_byte(self, b: int) -> str:
        return self.to_char[b]


@dataclass
class Field:
    start: int            # linear buffer index of the first data position
    length: int
    ffw: int
    attr: int
    fcw: list[int] = dc_field(default_factory=list)
    modified: bool = False
    cont_first: bool = False
    cont_middle: bool = False
    cont_last: bool = False

    @property
    def bypass(self) -> bool:
        return bool(self.ffw & FFW_BYPASS)

    @property
    def shift(self) -> str:
        return SHIFT_NAMES[self.ffw & FFW_SHIFT_MASK]

    @property
    def signed(self) -> bool:
        return (self.ffw & FFW_SHIFT_MASK) == 0x700

    @property
    def hidden(self) -> bool:
        return self.attr in NONDISPLAY_ATTRS

    @property
    def mandatory_entry(self) -> bool:
        return bool(self.ffw & FFW_MANDATORY_ENTRY)

    @property
    def adjust(self) -> int:
        return self.ffw & FFW_ADJUST_MASK

    @property
    def monocase(self) -> bool:
        return bool(self.ffw & FFW_MONOCASE)

    @property
    def continued(self) -> bool:
        return self.cont_first or self.cont_middle or self.cont_last


class ScreenBuffer:
    def __init__(self, rows: int = 24, cols: int = 80):
        self.resize(rows, cols)

    def resize(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols
        self.data = bytearray(rows * cols)
        self.fields: list[Field] = []
        self.header = b""          # format table header from SOH
        self.cursor = 0            # linear address of the write position
        self.user_cursor = 0       # where the operator cursor is (sent with AID)

    def snapshot(self) -> "ScreenBuffer":
        return copy.deepcopy(self)

    # -- addressing --------------------------------------------------------
    def addr(self, row: int, col: int) -> int:
        """1-based row/col to linear address."""
        return (row - 1) * self.cols + (col - 1)

    def rowcol(self, a: int) -> tuple[int, int]:
        return a // self.cols + 1, a % self.cols + 1

    def put(self, b: int) -> None:
        self.data[self.cursor] = b
        self.cursor = (self.cursor + 1) % len(self.data)

    # -- format table ------------------------------------------------------
    def clear_format_table(self) -> None:
        self.fields = []
        self.header = b""

    def field_at(self, a: int) -> Field | None:
        for f in self.fields:
            if f.start <= a < f.start + f.length:
                return f
        return None

    def add_field(self, f: Field) -> None:
        self.fields.append(f)

    def msg_line(self) -> int:
        """0-based message line row (from the SOH header, default last row)."""
        if len(self.header) >= 4 and 0 < self.header[3] <= self.rows:
            return self.header[3] - 1
        return self.rows - 1

    def send_data_for_aid(self, aid: int) -> bool:
        """SOH header bytes 4-6 hold a mask of F-keys that must NOT send
        field data (bit set = don't send)."""
        if len(self.header) <= 6:
            return True
        table = {}
        for i in range(8):
            table[0x31 + i] = (6, i)            # F1-F8
        for i, aidc in enumerate([0x39, 0x3A, 0x3B, 0x3C, 0xB1, 0xB2, 0xB3, 0xB4]):
            table[aidc] = (5, i)                # F9-F16
        for i, aidc in enumerate([0xB5, 0xB6, 0xB7, 0xB8, 0xB9, 0xBA, 0xBB, 0xBC]):
            table[aidc] = (4, i)                # F17-F24
        if aid not in table:
            return True
        byte, bit = table[aid]
        return (self.header[byte] & (0x80 >> bit)) == 0

    # -- logical input fields ---------------------------------------------
    def input_fields(self) -> list[list[Field]]:
        """Non-bypass fields, continued segments grouped into one logical
        field. Returned in format-table order."""
        out: list[list[Field]] = []
        group: list[Field] | None = None
        for f in self.fields:
            if f.cont_first:
                group = [f]
                continue
            if group is not None and (f.cont_middle or f.cont_last):
                group.append(f)
                if f.cont_last:
                    if not group[0].bypass:
                        out.append(group)
                    group = None
                continue
            if group is not None:           # malformed chain: flush
                if not group[0].bypass:
                    out.append(group)
                group = None
            if not f.bypass:
                out.append([f])
        if group is not None and not group[0].bypass:
            out.append(group)
        return out

    # -- rendering ---------------------------------------------------------
    def cell_attrs(self) -> list[int]:
        """Attribute in effect at each position (0x20 if none seen yet)."""
        out = [0x20] * len(self.data)
        cur = 0x20
        for i, b in enumerate(self.data):
            if 0x20 <= b <= 0x3F:
                cur = b
            out[i] = cur
        return out

    def text_rows(self, cp: Codepage, reveal_hidden: bool = False) -> list[str]:
        attrs = self.cell_attrs()
        chars = []
        for i, b in enumerate(self.data):
            if attrs[i] in NONDISPLAY_ATTRS and not reveal_hidden and not (0x20 <= b <= 0x3F):
                chars.append(" ")
            else:
                chars.append(cp.decode_byte(b))
        s = "".join(chars)
        return [s[r * self.cols:(r + 1) * self.cols] for r in range(self.rows)]

    def field_text(self, group: list[Field], cp: Codepage) -> str:
        return "".join(cp.decode_byte(self.data[a]) if self.data[a] >= 0x40 else " "
                       for f in group for a in range(f.start, f.start + f.length))

    def label_for(self, f: Field, rows: list[str]) -> str:
        """Best-effort label: text to the left on the same row, else the
        column heading above."""
        row, col = self.rowcol(f.start)
        line = rows[row - 1]
        left = line[:max(col - 2, 0)]
        # stop at the previous input field on this row
        for g in self.fields:
            gr, gc = self.rowcol(g.start)
            if g is not f and gr == row and gc < col:
                left = line[gc - 1 + g.length:max(col - 2, 0)]
        text = left.rstrip()
        text = re.sub(r"(\s*\.)+\s*:?$", "", text)       # "Option . . . :"
        text = re.sub(r"\s*(===>|==>|:)$", "", text)
        chunk = re.split(r"\s{2,}", text.strip())[-1] if text.strip() else ""
        if chunk:
            return chunk.strip(" .:")
        # column heading: look upwards for text above the field
        lo, hi = col - 1, col - 1 + min(f.length, self.cols - col + 1)
        for r in range(row - 2, -1, -1):
            for m in re.finditer(r"\S+(?: \S+)*", rows[r]):
                if m.start() < hi and m.end() > lo:      # heading overlaps the field
                    return m.group(0).strip(" .:")
        return ""
