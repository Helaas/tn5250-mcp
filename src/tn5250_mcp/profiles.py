"""Saved connection profiles: named hosts with their connection settings and
an optional flow to run after connecting (usually sign-on).

    profiles:
      pub400:
        description: Public IBM i for testing
        host: pub400.com
        port: 992
        device_name: "AGENT*"          # * = A, B, C ... per connection
        after_connect: {flow: signon, with: {user: MYUSER}}
        on_reconnect: true             # sign on again after the host drops us

The agent connects with `connect {"profile": "pub400"}` and never needs the
host details; `--profiles-only` makes profiles the only way to connect.
Passwords never go in a profile: use a sign-on flow with a secret parameter.
"""

from __future__ import annotations

import codecs
import itertools
import os
import random
import re
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .flows import NAME_RE, FlowConfigError, FlowSet

PROFILE_KEYS = {"description", "host", "port", "tls", "verify_tls", "device_name", "wide",
                "codepage", "after_connect", "on_reconnect"}
_SECRET_KEYS = {"password", "passwd", "pwd", "secret", "token"}
_DEVNAME = re.compile(r"^[A-Z#$@][A-Z0-9#$@_]{0,9}$")
_LETTERS = string.ascii_uppercase


class ProfileError(ValueError):
    """A profile file is invalid, or a profile doesn't exist."""


@dataclass
class Profile:
    name: str
    host: str
    description: str = ""
    port: int = 992
    tls: bool = True
    verify_tls: bool = True
    device_name: str | None = None      # may contain * (A, B, C...) and = (random)
    wide: bool = False
    codepage: str | None = None
    after_connect: dict | None = None   # {"flow": name, "with": {...}}
    on_reconnect: bool = False          # run after_connect again after a reconnect
    source: str = ""
    _seq: Any = field(default=None, repr=False)

    def next_device_name(self) -> str | None:
        """Expand the device name pattern: each * becomes the next letter
        (A, B, ... Z, then A again) for this profile, each = a random
        letter or digit."""
        if not self.device_name:
            return None
        if self._seq is None:
            self._seq = itertools.cycle(_LETTERS)
        out = []
        letter = next(self._seq) if "*" in self.device_name else ""
        for ch in self.device_name:
            if ch == "*":
                out.append(letter)
            elif ch == "=":
                out.append(random.choice(_LETTERS + string.digits))
            else:
                out.append(ch)
        return "".join(out)

    def summary(self) -> str:
        parts = ["%s:%d" % (self.host, self.port), "TLS" if self.tls else "plain telnet"]
        if self.device_name:
            parts.append("device %s" % self.device_name)
        if self.wide:
            parts.append("27x132")
        if self.after_connect:
            parts.append("then runs %s" % self.after_connect["flow"]
                         + (" (again after a reconnect)" if self.on_reconnect else ""))
        return "%s: %s%s" % (self.name, ", ".join(parts),
                             " - " + self.description if self.description else "")


def _bool(spec: dict, key: str, default: bool, where: str) -> bool:
    v = spec.get(key, default)
    if not isinstance(v, bool):
        raise ProfileError("%s: %s must be true or false" % (where, key))
    return v


def parse_profile(name: str, spec: Any, source: str) -> Profile:
    where = "%s: profile %s" % (source, name)
    if not NAME_RE.match(str(name)):
        raise ProfileError("%s: profile names must be lower_snake_case" % where)
    if not isinstance(spec, dict):
        raise ProfileError("%s: must be a mapping" % where)
    secret = _SECRET_KEYS & {str(k).lower() for k in spec}
    if secret:
        raise ProfileError("%s: profiles never hold passwords (%s). Use a sign-on flow with a "
                           "secret parameter (typed in the viewer, or from the environment) "
                           "as after_connect" % (where, ", ".join(sorted(secret))))
    bad = set(spec) - PROFILE_KEYS
    if bad:
        raise ProfileError("%s: unknown keys %s (allowed: %s)"
                           % (where, sorted(bad), ", ".join(sorted(PROFILE_KEYS))))
    host = spec.get("host")
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9.:\[\]_-]+", host):
        raise ProfileError("%s: needs a host name or address" % where)
    tls = _bool(spec, "tls", True, where)
    port = spec.get("port", 992 if tls else 23)
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise ProfileError("%s: port must be a number from 1 to 65535" % where)
    p = Profile(name=str(name), host=host, description=str(spec.get("description", "")),
                port=port, tls=tls, verify_tls=_bool(spec, "verify_tls", True, where),
                wide=_bool(spec, "wide", False, where),
                on_reconnect=_bool(spec, "on_reconnect", False, where), source=source)
    if spec.get("device_name") is not None:
        p.device_name = str(spec["device_name"]).upper()
        sample = p.device_name.replace("*", "A").replace("=", "A")
        if not _DEVNAME.match(sample):
            raise ProfileError("%s: device_name %r: up to 10 letters, digits, #, $, @ or _, "
                               "starting with a letter (* = A, B, C...; = = random)"
                               % (where, spec["device_name"]))
    if spec.get("codepage") is not None:
        try:
            codecs.lookup(str(spec["codepage"]))
        except LookupError:
            raise ProfileError("%s: unknown code page %r" % (where, spec["codepage"])) from None
        p.codepage = str(spec["codepage"])
    ac = spec.get("after_connect")
    if ac is not None:
        if isinstance(ac, str):
            ac = {"flow": ac}
        if not isinstance(ac, dict) or set(ac) - {"flow", "with"} or not ac.get("flow"):
            raise ProfileError("%s: after_connect is a flow name or {flow: name, with: {...}}"
                               % where)
        args = ac.get("with") or {}
        if not isinstance(args, dict):
            raise ProfileError("%s: after_connect.with must map parameter -> value" % where)
        p.after_connect = {"flow": str(ac["flow"]), "with": dict(args)}
    elif p.on_reconnect:
        raise ProfileError("%s: on_reconnect needs an after_connect flow" % where)
    return p


class ProfileSet:
    def __init__(self) -> None:
        self.profiles: dict[str, Profile] = {}

    @classmethod
    def load(cls, paths: list[str | os.PathLike]) -> "ProfileSet":
        ps = cls()
        for p in paths:
            p = Path(p).expanduser()
            files = sorted([*p.glob("*.yaml"), *p.glob("*.yml")]) if p.is_dir() else [p]
            for f in files:
                try:
                    doc = yaml.safe_load(f.read_text("utf-8"))
                except OSError as e:
                    raise ProfileError("%s: %s" % (f, e.strerror or e)) from None
                except yaml.YAMLError as e:
                    raise ProfileError("%s: not valid YAML: %s" % (f, e)) from None
                ps.add_doc(doc, str(f))
        return ps

    def add_doc(self, doc: Any, source: str = "<profiles>") -> None:
        if not isinstance(doc, dict) or set(doc) != {"profiles"} or \
                not isinstance(doc["profiles"], dict):
            raise ProfileError("%s: expected one top-level 'profiles:' mapping" % source)
        for name, spec in doc["profiles"].items():
            if name in self.profiles:
                raise ProfileError("%s: profile %s is defined twice (also in %s)"
                                   % (source, name, self.profiles[name].source))
            self.profiles[name] = parse_profile(name, spec, source)

    def get(self, name: str) -> Profile:
        p = self.profiles.get(name)
        if not p:
            raise ProfileError("no profile %r; profiles: %s" % (
                name, ", ".join(self.profiles) or "none (start the server with --profiles)"))
        return p

    def validate_flows(self, flows: FlowSet | None, runner=None) -> None:
        """after_connect flows must exist, and their parameters must be valid."""
        for p in self.profiles.values():
            if not p.after_connect:
                continue
            f = (flows.flows if flows else {}).get(p.after_connect["flow"])
            if not f:
                raise ProfileError("%s: profile %s runs flow %r after connecting, but no such "
                                   "flow is loaded (--flows)" % (p.source, p.name,
                                                                 p.after_connect["flow"]))
            if runner is not None:
                try:
                    runner.precheck(f, p.after_connect["with"])
                except Exception as e:  # FlowError: bad parameter values
                    raise ProfileError("%s: profile %s: after_connect: %s"
                                       % (p.source, p.name, e)) from None


__all__ = ["Profile", "ProfileSet", "ProfileError", "FlowConfigError"]
