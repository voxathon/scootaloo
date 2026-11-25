"""Settings files: one app-wide `config.json`, and a `preferences.json` per user.

    <home>/config.json                      app-wide: how scootaloo behaves for everyone
    <home>/users/<name>/preferences.json    one user's choices: layout, pane sizes

Both are plain JSON you can edit by hand. Keys are dotted paths
(`login.list_users`, `layout.list_share`). Every key has a default here, and
`set` only accepts a value of the default's type, so a typo in a value can't
quietly turn into a broken setting. Unknown keys in the file are kept as they
are, so a newer scootaloo's settings survive an older one.

A file that isn't valid JSON is renamed to `<name>.broken` (never deleted) and
the defaults are used, with a warning available in `.warning`.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from typing import Any

CONFIG_DEFAULTS: dict[str, Any] = {
    "version": 1,
    "login": {
        # Show a menu of local users at login. Off: a blank name + password form.
        "list_users": True,
    },
}

PREFERENCES_DEFAULTS: dict[str, Any] = {
    "version": 1,
    "layout": {
        "name": "three",  # three | two | stacked
        "boards_width": 32,  # columns
        "list_share": 50,  # percent of the width (or height, stacked) the thread list gets
    },
}

# Allowed values where the type alone isn't enough.
CHOICES: dict[str, tuple] = {"layout.name": ("three", "two", "stacked")}
RANGES: dict[str, tuple[int, int]] = {"layout.boards_width": (16, 80), "layout.list_share": (15, 85)}


class SettingsError(ValueError):
    pass


def _merge(defaults: dict, found: dict) -> dict:
    out = copy.deepcopy(found)
    for k, v in defaults.items():
        if isinstance(v, dict):
            out[k] = _merge(v, found.get(k) if isinstance(found.get(k), dict) else {})
        elif k not in found or type(found[k]) is not type(v):
            out[k] = v
    return out


def _flatten(d: dict, prefix: str = "") -> dict[str, Any]:
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


class JsonSettings:
    def __init__(self, path: str, defaults: dict[str, Any]):
        self.path = path
        self.defaults = defaults
        self.warning = ""
        self.data = self._load()

    @property
    def exists(self) -> bool:
        return os.path.exists(self.path)

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                found = json.load(f)
            if not isinstance(found, dict):
                raise ValueError("not a JSON object")
        except FileNotFoundError:
            return copy.deepcopy(self.defaults)
        except (OSError, ValueError) as e:
            broken = self.path + ".broken"
            try:
                os.replace(self.path, broken)
                self.warning = f"{self.path} couldn't be read ({e}); kept it as {broken} and used defaults"
            except OSError:
                self.warning = f"{self.path} couldn't be read ({e}); using defaults"
            return copy.deepcopy(self.defaults)
        return _merge(self.defaults, found)

    def _default(self, key: str):
        node: Any = self.defaults
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                raise SettingsError(f"unknown setting '{key}'")
            node = node[part]
        if isinstance(node, dict):
            raise SettingsError(f"'{key}' is a group, not a setting")
        return node

    def get(self, key: str):
        self._default(key)
        node: Any = self.data
        for part in key.split("."):
            node = node[part]
        return node

    def set(self, key: str, value) -> None:
        default = self._default(key)
        if key == "version":
            raise SettingsError("'version' is managed by scootaloo")
        if isinstance(value, str) and not isinstance(default, str):
            value = self._parse(key, value, default)
        if type(value) is not type(default):
            raise SettingsError(f"'{key}' takes a {type(default).__name__}, not {value!r}")
        if key in CHOICES and value not in CHOICES[key]:
            raise SettingsError(f"'{key}' must be one of: {', '.join(CHOICES[key])}")
        if key in RANGES:
            lo, hi = RANGES[key]
            if not lo <= value <= hi:
                raise SettingsError(f"'{key}' must be between {lo} and {hi}")
        node = self.data
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    @staticmethod
    def _parse(key: str, text: str, default):
        t = text.strip().lower()
        if isinstance(default, bool):
            if t in ("true", "yes", "on", "1"):
                return True
            if t in ("false", "no", "off", "0"):
                return False
            raise SettingsError(f"'{key}' takes true or false, not {text!r}")
        if isinstance(default, int):
            try:
                return int(t)
            except ValueError:
                raise SettingsError(f"'{key}' takes a whole number, not {text!r}") from None
        return text

    def items(self) -> dict[str, Any]:
        """Every known setting and its current value (unknown keys in the file aren't listed)."""
        return {k: self.get(k) for k in _flatten(self.defaults) if k != "version"}

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(self.path) or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def config() -> JsonSettings:
    from scootaloo.accounts import home

    return JsonSettings(os.path.join(home(), "config.json"), CONFIG_DEFAULTS)


def preferences(stores) -> JsonSettings:
    return JsonSettings(stores.preferences_path, PREFERENCES_DEFAULTS)
