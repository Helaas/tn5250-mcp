"""Passwords the person typed, so they aren't asked for again and again.

Two layers, per host and user profile:

- memory: kept while the server runs, after a sign-on that worked, until it
  has gone unused for `idle_hours`;
- keychain (opt-in, "Remember on this computer" in the viewer): the system
  keychain through the `keyring` package (pip install "tn5250-mcp[keyring]"),
  so later sessions need no prompt at all.

A password is only kept after it worked, is dropped the moment the host
rejects it (failed sign-ons count toward disabling the profile), and is never
handed to the agent: nothing in the MCP tools returns it.
"""

from __future__ import annotations

import json
import threading
import time

SERVICE = "tn5250-mcp"
_INDEX = "_index"          # keychain entry listing the user@host entries we saved


def _key(host: str, user: str) -> tuple[str, str]:
    return host.strip().lower(), user.strip().upper()


def keyring_available() -> bool:
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:
        return False
    try:
        return not isinstance(keyring.get_keyring(), fail.Keyring)
    except Exception:  # noqa: BLE001 - a broken backend counts as none
        return False


class CredentialStore:
    def __init__(self, idle_hours: float = 8.0, keychain: bool = False):
        self.idle = idle_hours * 3600
        self.keychain = keychain and keyring_available()
        self._mem: dict[tuple[str, str], tuple[str, float]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.idle > 0 or self.keychain

    # -- keychain ---------------------------------------------------------------
    def _kc_get(self, k) -> str | None:
        if not self.keychain:
            return None
        import keyring
        try:
            return keyring.get_password(SERVICE, "%s@%s" % (k[1], k[0]))
        except Exception:  # noqa: BLE001 - keychain locked or access denied
            return None

    def _kc_index(self) -> list[str]:
        import keyring
        try:
            return json.loads(keyring.get_password(SERVICE, _INDEX) or "[]")
        except Exception:  # noqa: BLE001
            return []

    def _kc_set(self, k, password: str) -> None:
        import keyring
        name = "%s@%s" % (k[1], k[0])
        keyring.set_password(SERVICE, name, password)
        idx = self._kc_index()
        if name not in idx:
            keyring.set_password(SERVICE, _INDEX, json.dumps(idx + [name]))

    def _kc_delete(self, names: list[str]) -> int:
        import keyring
        gone = 0
        for name in names:
            try:
                keyring.delete_password(SERVICE, name)
                gone += 1
            except Exception:  # noqa: BLE001 - wasn't there
                pass
        left = [n for n in self._kc_index() if n not in names]
        try:
            if left:
                keyring.set_password(SERVICE, _INDEX, json.dumps(left))
            else:
                keyring.delete_password(SERVICE, _INDEX)
        except Exception:  # noqa: BLE001
            pass
        return gone

    # -- use --------------------------------------------------------------------
    def get(self, host: str, user: str) -> str | None:
        """A saved password for this host and user, or None."""
        k = _key(host, user)
        now = time.monotonic()
        with self._lock:
            hit = self._mem.get(k)
            if hit and now - hit[1] < self.idle:
                self._mem[k] = (hit[0], now)
                return hit[0]
            self._mem.pop(k, None)
        pw = self._kc_get(k)
        if pw and self.idle > 0:
            with self._lock:
                self._mem[k] = (pw, now)
        return pw

    def put(self, host: str, user: str, password: str, remember: bool = False) -> None:
        """Keep a password that just worked (and save it in the keychain when
        the person asked for that)."""
        k = _key(host, user)
        if self.idle > 0:
            with self._lock:
                self._mem[k] = (password, time.monotonic())
        if remember and self.keychain:
            self._kc_set(k, password)

    def replace(self, host: str, user: str, password: str) -> None:
        """The password was changed: hold the new one where the old one was
        (memory, and the keychain if it was saved there)."""
        k = _key(host, user)
        in_keychain = self.keychain and self._kc_get(k) is not None
        self.put(host, user, password, remember=in_keychain)
        if self.idle <= 0:                 # not kept in memory; still never keep the old one
            with self._lock:
                self._mem.pop(k, None)

    def invalidate(self, host: str, user: str) -> None:
        """The host rejected the saved password: drop it everywhere."""
        k = _key(host, user)
        with self._lock:
            self._mem.pop(k, None)
        if self.keychain:
            self._kc_delete(["%s@%s" % (k[1], k[0])])

    def forget(self, host: str | None = None, user: str | None = None) -> int:
        """Forget saved passwords (all, or for a host / user), in memory and in
        the keychain. Returns how many were forgotten."""
        def match(k):
            return (host is None or k[0] == host.strip().lower()) and \
                   (user is None or k[1] == user.strip().upper())
        with self._lock:
            mem = [k for k in self._mem if match(k)]
            for k in mem:
                del self._mem[k]
        names = set()
        if self.keychain:
            for name in self._kc_index():
                u, _, h = name.partition("@")
                if match((h, u)):
                    names.add(name)
            self._kc_delete(sorted(names))
        return len(set("%s@%s" % (k[1], k[0]) for k in mem) | names)

    def saved(self) -> list[str]:
        """user@host of the passwords held now (never the passwords)."""
        with self._lock:
            now = time.monotonic()
            mem = {"%s@%s" % (k[1], k[0]) for k, v in self._mem.items()
                   if now - v[1] < self.idle}
        kc = set(self._kc_index()) if self.keychain else set()
        return sorted(mem | kc)
