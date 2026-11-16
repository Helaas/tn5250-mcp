"""End-to-end data-entry test through the MCP tools.

Creates LIB/T5250TEST, keys three records in through UPDDTA (DFU) screens,
checks them with interactive SQL, then deletes the table.

Needs a running server (`tn5250-mcp --transport http`) with a session that is
signed on and sitting at the IBM i Main Menu (sign on in the viewer).

    python examples/data_entry_test.py MYLIB [--keep] [--session main]

Leaves one spooled file per run (UPDDTA prints an audit list of added records).
STRSQL is left with its default "save session" option.
"""

import argparse
import asyncio
import os
import re
import sys

from mcp.client import Client

URL = os.environ.get("T5250_MCP_URL", "http://127.0.0.1:8250/mcp")
CMDLINE = "20,7"          # command line on the standard MAIN menu


class Failed(Exception):
    pass


class Driver:
    def __init__(self, client, session):
        self.c, self.session = client, session

    async def call(self, tool, **args):
        args.setdefault("session", self.session)
        res = await self.c.call_tool(tool, args)
        text = "\n".join(getattr(i, "text", "") for i in res.content)
        if res.is_error:
            raise Failed("%s failed: %s" % (tool, text))
        return text

    async def press(self, key, expect, **kw):
        return await self.call("press", key=key, expect=expect, **kw)

    async def command(self, cmd):
        return await self.press("Enter", "IBM i Main Menu", values={CMDLINE: cmd}, timeout=60)


def rows(screen_text):
    return {int(m.group(1)): m.group(2) for m in re.finditer(r"^(\d\d) \|(.*)$", screen_text, re.M)}


def keyboard(screen_text):
    return re.search(r"keyboard (\w+)", screen_text).group(1)


async def run(lib, keep, session):
    table = "%s/T5250TEST" % lib
    async with Client(URL) as client:
        d = Driver(client, session)
        scr = await d.call("get_screen")
        if "IBM i Main Menu" not in scr:
            raise Failed("start from the IBM i Main Menu; the screen shows:\n" + scr)

        print("== create %s" % table)
        scr = await d.command("DLTF FILE(%s)" % table)        # ok if it doesn't exist
        if keyboard(scr) == "ERROR":
            await d.call("press", key="Reset")
        await d.command("RUNSQL SQL('CREATE TABLE %s (ID INT NOT NULL PRIMARY KEY, "
                        "NAME CHAR(20) NOT NULL, QTY DECIMAL(5,0) NOT NULL)') COMMIT(*NONE)"
                        % table)

        print("== enter records through UPDDTA")
        scr = await d.command("UPDDTA FILE(%s)" % table)
        if "ENTRY" not in rows(scr).get(1, ""):
            raise Failed("UPDDTA entry screen not shown:\n" + scr)
        for rid, name, qty in [("1", "WIDGET", "25"), ("2", "GADGET", "100"),
                               ("3", "SPROCKET", "-3")]:
            scr = await d.press("Enter", "1:ENTRY", values={"ID": rid, "NAME": name, "QTY": qty})
            msg = rows(scr).get(24, "").strip()
            if msg or keyboard(scr) != "READY":
                raise Failed("record %s rejected: %r\n%s" % (rid, msg, scr))
            print("   added %s %s %s" % (rid, name, qty))
        scr = await d.press("F3", "1:ENTRY")
        if not re.search(r"Added .*:\s+3\b", scr):
            raise Failed("UPDDTA did not report 3 added:\n" + scr)
        await d.press("Enter", "End Data Entry")

        print("== check with SQL")
        scr = await d.command("STRSQL")
        m = re.search(r"cursor (\d+),(\d+)", scr)
        stmt_field = "%s,%s" % m.groups()                  # STRSQL puts the cursor on its input
        scr = await d.press("Enter", "Enter SQL Statements",
                            values={stmt_field: "SELECT ID, NAME, QTY FROM %s ORDER BY ID" % table})
        body = "\n".join(rows(scr).values())
        for rx in [r"\b1\s+WIDGET\s+25\b", r"\b2\s+GADGET\s+100\b", r"\b3\s+SPROCKET\s+3-"]:
            if not re.search(rx, body):
                raise Failed("missing row %s:\n%s" % (rx, scr))
        print("\n".join(l for l in body.splitlines() if re.search(r"WIDGET|GADGET|SPROCKET|NAME", l)))
        await d.press("F3", "Display Data")
        await d.press("F3", "Enter SQL Statements")
        await d.press("Enter", "Exit Interactive SQL")

        if not keep:
            print("== clean up")
            await d.command("DLTF FILE(%s)" % table)
        scr = await d.call("get_screen", fields=False)
        if "IBM i Main Menu" not in scr:
            raise Failed("did not end on the main menu:\n" + scr)
    print("data-entry test passed")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("library")
    p.add_argument("--keep", action="store_true")
    p.add_argument("--session", default="main")
    a = p.parse_args()
    try:
        asyncio.run(run(a.library.upper(), a.keep, a.session))
    except Failed as e:
        print("FAILED: %s" % e)
        sys.exit(1)


if __name__ == "__main__":
    main()
