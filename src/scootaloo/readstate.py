"""Per-user state: what's been read, which link domains are trusted, layout settings.

What a user has read is tracked per (origin, board).

The first time a board is seen, everything already on it counts as read (its
"floor"), so joining a busy relay doesn't bury you in hundreds of unread
posts. After that an article is unread until you open the thread it's in.
Article numbers are per (origin, board) and never reused, so they're a safe key.
"""

from __future__ import annotations

import sqlite3


class ReadState:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS floors (origin TEXT, board TEXT, floor INTEGER NOT NULL,
                PRIMARY KEY (origin, board));
            CREATE TABLE IF NOT EXISTS read (origin TEXT, board TEXT, num INTEGER,
                PRIMARY KEY (origin, board, num));
            CREATE TABLE IF NOT EXISTS trusted_domains (domain TEXT PRIMARY KEY, added_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )

    def close(self) -> None:
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def floor(self, origin: str, board: str) -> int | None:
        row = self._db.execute("SELECT floor FROM floors WHERE origin=? AND board=?", (origin, board)).fetchone()
        return None if row is None else row[0]

    def ensure_floor(self, origin: str, board: str, nums) -> int:
        """Set the floor at the newest article the first time a board is seen."""
        f = self.floor(origin, board)
        if f is None:
            f = max(nums, default=0)
            self._db.execute("INSERT INTO floors VALUES (?, ?, ?)", (origin, board, f))
            self._db.commit()
        return f

    def unread(self, origin: str, board: str, nums) -> set[int]:
        nums = set(nums)
        f = self.ensure_floor(origin, board, nums)
        candidates = {n for n in nums if n > f}
        if not candidates:
            return set()
        seen = {
            r[0]
            for r in self._db.execute(
                "SELECT num FROM read WHERE origin=? AND board=? AND num > ?", (origin, board, f)
            )
        }
        return candidates - seen

    def mark(self, origin: str, board: str, nums) -> None:
        self._db.executemany(
            "INSERT OR IGNORE INTO read VALUES (?, ?, ?)", [(origin, board, n) for n in nums]
        )
        self._db.commit()

    def mark_all(self, origin: str, board: str, nums) -> None:
        """Everything up to the newest is read: raise the floor and drop the per-article rows below it."""
        top = max(nums, default=0)
        cur = self.floor(origin, board) or 0
        if top > cur:
            self._db.execute("INSERT OR REPLACE INTO floors VALUES (?, ?, ?)", (origin, board, top))
        self._db.execute("DELETE FROM read WHERE origin=? AND board=? AND num <= ?", (origin, board, max(top, cur)))
        self._db.commit()

    # -- trusted link domains --------------------------------------------------

    def trusts(self, domain: str) -> bool:
        return self._db.execute("SELECT 1 FROM trusted_domains WHERE domain=?", (domain.lower(),)).fetchone() is not None

    def trust(self, domain: str) -> None:
        import time

        self._db.execute("INSERT OR IGNORE INTO trusted_domains VALUES (?, ?)", (domain.lower(), int(time.time())))
        self._db.commit()

    def untrust(self, domain: str) -> bool:
        cur = self._db.execute("DELETE FROM trusted_domains WHERE domain=?", (domain.lower(),))
        self._db.commit()
        return cur.rowcount > 0

    def trusted_domains(self) -> list[str]:
        return [r[0] for r in self._db.execute("SELECT domain FROM trusted_domains ORDER BY domain")]

    # -- settings --------------------------------------------------------------

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self._db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return default if row is None else row[0]

    def set(self, key: str, value: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))
        self._db.commit()
