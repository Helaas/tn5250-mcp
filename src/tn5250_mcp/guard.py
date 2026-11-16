"""Safety rails applied to everything an agent sends to the host."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field

# CL commands an agent should not run without a human. Matched as whole words,
# case-insensitive, anywhere in a value typed into any field (command lines,
# prompts, SQL). Best effort: it is a seatbelt, not an authority boundary.
# Use a user profile with limited authority for the real protection.
DEFAULT_BLOCKED = [
    r"PWRDWNSYS", r"ENDSYS", r"ENDSBS", r"ENDTCP", r"ENDTCPSVR", r"ENDHOSTSVR",
    r"DLTLIB", r"CLRLIB", r"DLTUSRPRF", r"CHGUSRPRF", r"CRTUSRPRF", r"CHGSYSVAL",
    r"RCLSTG", r"INZSYS", r"STRSST", r"RMVDIR", r"GRTOBJAUT", r"RVKOBJAUT",
    r"CHGOBJOWN", r"DROP\s+(SCHEMA|COLLECTION)",
    r"QCMDEXC", r"QCAPCMD",   # run any CL command from SQL or a program call
]


@dataclass
class Guard:
    require_check: bool = True           # mutating calls need expect or screen_id
    allowed_hosts: list[str] = field(default_factory=list)   # empty = any host
    blocked: list[str] = field(default_factory=lambda: list(DEFAULT_BLOCKED))

    def __post_init__(self):
        self._blocked_re = [re.compile(r"(?<![A-Z0-9_$#@])(?:%s)(?![A-Z0-9_$#@])" % p,
                                       re.IGNORECASE) for p in self.blocked]

    def check_host(self, host: str) -> None:
        if self.allowed_hosts and not any(fnmatch.fnmatch(host.lower(), p.lower())
                                          for p in self.allowed_hosts):
            raise PermissionError("host %r is not in the allowed hosts (%s)"
                                  % (host, ", ".join(self.allowed_hosts)))

    def check_values(self, values: dict[str, str]) -> None:
        for ref, v in values.items():
            for rx in self._blocked_re:
                m = rx.search(v or "")
                if m:
                    raise PermissionError(
                        "value for field %s contains blocked command %r. Ask the user "
                        "to run it themselves, or have them change the server's block "
                        "list." % (ref, m.group(0)))

    @staticmethod
    def expect_matches(rows: list[str], expect: str) -> bool:
        """`expect` is a substring of the screen, or "N:text" to require it on
        row N."""
        m = re.fullmatch(r"(\d+):(.*)", expect, re.DOTALL)
        if m:
            r = int(m.group(1))
            return 1 <= r <= len(rows) and m.group(2) in rows[r - 1]
        return expect in "\n".join(rows)
