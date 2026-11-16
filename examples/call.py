"""Call one tn5250-mcp tool from the shell (handy for scripts and debugging).

    python examples/call.py get_screen
    python examples/call.py press '{"key": "Enter", "expect": "Main Menu"}'

Talks to a server started with `tn5250-mcp --transport http` (default
http://127.0.0.1:8250/mcp; override with T5250_MCP_URL). Exits 1 if the tool
returns an error.
"""

import asyncio
import json
import os
import sys

from mcp.client import Client


async def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    tool = sys.argv[1]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    url = os.environ.get("T5250_MCP_URL", "http://127.0.0.1:8250/mcp")
    async with Client(url) as client:
        res = await client.call_tool(tool, args)
    for item in res.content:
        print(getattr(item, "text", item))
    return 1 if res.is_error else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
