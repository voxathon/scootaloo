"""Where scootaloo keeps things, and who is using it.

scootaloo is an application, so it has local users. Each user has a password
(no exceptions) and a directory of their own:

    <home>/config.json                  app-wide settings (see settings.py)
    <home>/users.db                     names + bcrypt hashes
    <home>/current                      last user to log in (a name, never a secret)
    <home>/users/<name>/identities.db   Bonnet identities, keys wrapped with the user's password
    <home>/users/<name>/origins.db      relays this user has joined
    <home>/users/<name>/trust.db        origin keys this user has accepted
    <home>/users/<name>/state.db        what this user has read, trusted link domains, layout
    <home>/users/<name>/cache.db        article bodies, keyed (and checked) by their hash
    <home>/users/<name>/preferences.json  this user's preferences (layout, pane sizes)

Pins and joined origins are per user on purpose: accepting a key is a trust
decision, and one person's decision shouldn't commit another.

<home> is $SCOOTALOO_HOME, else the OS per-user data folder.

`--gateway-store` skips all of this and uses the Bonnet MCP gateway's own
default store instead, so identities made there are usable here. Nothing is
copied in either direction.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass

import bcrypt
import platformdirs

from bonnet.core.trust import TrustStore
from bonnet.gateway.identity import IdentityStore
from bonnet.gateway.origins import OriginStore

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class AccountError(Exception):
    pass


def home() -> str:
    override = os.environ.get("SCOOTALOO_HOME")
    path = os.path.expanduser(override) if override else platformdirs.user_data_dir("scootaloo", appauthor=False)
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Stores: one set of Bonnet databases
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Stores:
    identities_path: str
    origins_path: str
    trust_path: str
    label: str
    state_path: str = ""
    cache_path: str = ""
    preferences_path: str = ""

    @classmethod
    def for_user(cls, name: str) -> "Stores":
        d = os.path.join(home(), "users", name)
        os.makedirs(d, exist_ok=True)
        return cls(
            os.path.join(d, "identities.db"),
            os.path.join(d, "origins.db"),
            os.path.join(d, "trust.db"),
            label=name,
            state_path=os.path.join(d, "state.db"),
            cache_path=os.path.join(d, "cache.db"),
            preferences_path=os.path.join(d, "preferences.json"),
        )

    @classmethod
    def gateway(cls) -> "Stores":
        from bonnet.gateway.paths import DEFAULT_TENANT, identities_db_path, origins_db_path, trust_db_path

        paths = [identities_db_path(DEFAULT_TENANT), origins_db_path(DEFAULT_TENANT), trust_db_path(DEFAULT_TENANT)]
        for p in paths:
            os.makedirs(os.path.dirname(p), exist_ok=True)
        # Read state is scootaloo's own, so it stays in scootaloo's home even here.
        state = os.path.join(home(), "gateway-store-state.db")
        cache = os.path.join(home(), "gateway-store-cache.db")
        prefs = os.path.join(home(), "gateway-store-preferences.json")
        return cls(*paths, label="gateway store", state_path=state, cache_path=cache, preferences_path=prefs)

    def identities(self) -> IdentityStore:
        return IdentityStore(self.identities_path)

    def origins(self) -> OriginStore:
        return OriginStore(self.origins_path)

    def trust(self) -> TrustStore:
        return TrustStore(self.trust_path)


@dataclass
class Account:
    """A logged-in local user (or the gateway store), plus the secret that unlocks their keys."""

    stores: Stores
    name: str
    password: str | None
    gateway: bool = False

    @property
    def wraps_keys(self) -> bool:
        """User accounts always encrypt identity keys; the gateway store leaves that per identity."""
        return not self.gateway


# ---------------------------------------------------------------------------
# Local users
# ---------------------------------------------------------------------------


class Users:
    def __init__(self) -> None:
        self.path = os.path.join(home(), "users.db")
        self._db = sqlite3.connect(self.path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS users (name TEXT PRIMARY KEY, pw_hash BLOB NOT NULL, created_at INTEGER NOT NULL)"
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def names(self) -> list[str]:
        return [r[0] for r in self._db.execute("SELECT name FROM users ORDER BY name")]

    def exists(self, name: str) -> bool:
        return self._db.execute("SELECT 1 FROM users WHERE name = ?", (name,)).fetchone() is not None

    def add(self, name: str, password: str) -> None:
        if not NAME_RE.match(name):
            raise AccountError("user names are 1-32 characters: lowercase letters, digits, - and _, starting with a letter or digit")
        if not password:
            raise AccountError("every user needs a password")
        if self.exists(name):
            raise AccountError(f"there's already a user called '{name}'")
        if os.path.exists(os.path.join(home(), "users", name)):
            raise AccountError(f"a folder for '{name}' already exists in {home()}; remove it first")
        h = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
        self._db.execute("INSERT INTO users VALUES (?, ?, ?)", (name, h, int(time.time())))
        self._db.commit()
        Stores.for_user(name)

    def verify(self, name: str, password: str) -> bool:
        row = self._db.execute("SELECT pw_hash FROM users WHERE name = ?", (name,)).fetchone()
        if row is None:
            bcrypt.checkpw(b"x", bcrypt.hashpw(b"y", bcrypt.gensalt()))  # same cost either way
            return False
        return bcrypt.checkpw(password.encode("utf-8"), bytes(row[0]))

    def login(self, name: str, password: str) -> Account:
        if not self.verify(name, password):
            raise AccountError("wrong user name or password")
        set_current(name)
        return Account(Stores.for_user(name), name, password)

    def change_password(self, name: str, old: str, new: str) -> int:
        """Re-encrypt every identity key of `name` under `new`. Returns how many keys moved.

        Builds a fresh identities database beside the old one, then swaps it
        in, so a failure part-way leaves the old keys and old password intact.
        The old database is kept as identities.db.bak until the password hash
        is updated too.
        """
        if not self.verify(name, old):
            raise AccountError("wrong user name or password")
        if not new:
            raise AccountError("every user needs a password")
        if new == old:
            raise AccountError("that's the same password")

        stores = Stores.for_user(name)
        path = stores.identities_path
        fresh_path, backup = path + ".new", path + ".bak"
        for leftover in (fresh_path,):
            if os.path.exists(leftover):
                os.remove(leftover)

        old_store = stores.identities()
        try:
            rows = old_store.list_users()
            conn = old_store._get_conn()
            if conn.execute("SELECT COUNT(*) FROM venue_accounts").fetchone()[0]:
                raise AccountError(
                    "this user has venue accounts linked through the Bonnet gateway; "
                    "scootaloo can't re-encrypt those yet, so nothing was changed"
                )
            seeds = []
            for r in rows:
                pw = old if old_store.is_wrapped(r["origin"], r["username"]) else None
                try:
                    seeds.append((r, old_store.get_private_key(r["origin"], r["username"], pw)))
                except Exception as e:
                    raise AccountError(
                        f"couldn't unlock '{r['username']}' on {r['origin']} with the current password "
                        f"({e}); nothing was changed"
                    ) from e
        finally:
            old_store.close()

        new_store = IdentityStore(fresh_path)
        try:
            for r, seed in seeds:
                new_store.register(r["origin"], r["username"], new, private_key=seed)
                if r["registered"]:
                    new_store.mark_registered(r["origin"], r["username"])
            for r, seed in seeds:  # prove every key opens under the new password before swapping
                if new_store.get_private_key(r["origin"], r["username"], new) != seed:
                    raise AccountError("re-encryption check failed; nothing was changed")
        except Exception:
            new_store.close()
            os.remove(fresh_path)
            raise
        new_store.close()

        if os.path.exists(path):
            os.replace(path, backup)
        os.replace(fresh_path, path)
        h = bcrypt.hashpw(new.encode("utf-8"), bcrypt.gensalt())
        self._db.execute("UPDATE users SET pw_hash = ? WHERE name = ?", (h, name))
        self._db.commit()
        if os.path.exists(backup):
            os.remove(backup)
        return len(seeds)

    def remove(self, name: str, password: str) -> None:
        if not self.verify(name, password):
            raise AccountError("wrong user name or password")
        self._db.execute("DELETE FROM users WHERE name = ?", (name,))
        self._db.commit()
        shutil.rmtree(os.path.join(home(), "users", name), ignore_errors=True)
        if current() == name:
            set_current("")


def current() -> str:
    try:
        with open(os.path.join(home(), "current"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def set_current(name: str) -> None:
    with open(os.path.join(home(), "current"), "w", encoding="utf-8") as f:
        f.write(name)


def default_user() -> str | None:
    """Who to log in as when nobody said: $SCOOTALOO_USER, the last user, or the only user."""
    env = os.environ.get("SCOOTALOO_USER")
    if env:
        return env
    with Users() as u:
        names = u.names()
    last = current()
    if last in names:
        return last
    return names[0] if len(names) == 1 else None
