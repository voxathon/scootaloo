"""Article bodies on disk, keyed by the hash the author signed.

A Bonnet body is content-addressed: its record carries
sha256(DOMAIN_BODY + bytes), so a cached body can never go stale, and every
read re-checks the bytes against that hash. A row that fails the check (disk
corruption, tampering) is dropped and treated as a miss.
"""

from __future__ import annotations

import sqlite3
import time

from bonnet.core.record import compute_body_hash


class BodyCache:
    def __init__(self, path: str, max_bytes: int = 200 * 1024 * 1024):
        self.path = path
        self.max_bytes = max_bytes
        self._db = sqlite3.connect(path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS bodies (hash TEXT PRIMARY KEY, body BLOB NOT NULL, "
            "size INTEGER NOT NULL, used_at INTEGER NOT NULL)"
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def get(self, body_hash: str) -> bytes | None:
        if not body_hash:
            return None
        row = self._db.execute("SELECT body FROM bodies WHERE hash=?", (body_hash,)).fetchone()
        if row is None:
            return None
        raw = bytes(row[0])
        if compute_body_hash(raw).hex() != body_hash:
            self._db.execute("DELETE FROM bodies WHERE hash=?", (body_hash,))
            self._db.commit()
            return None
        self._db.execute("UPDATE bodies SET used_at=? WHERE hash=?", (int(time.time()), body_hash))
        self._db.commit()
        return raw

    def put(self, body_hash: str, raw: bytes) -> bool:
        """Store `raw` only if it really hashes to `body_hash`. Returns whether it was stored."""
        if not body_hash or compute_body_hash(raw).hex() != body_hash:
            return False
        self._db.execute(
            "INSERT OR REPLACE INTO bodies VALUES (?, ?, ?, ?)", (body_hash, raw, len(raw), int(time.time()))
        )
        self._db.commit()
        self._trim()
        return True

    def _trim(self) -> None:
        total = self._db.execute("SELECT COALESCE(SUM(size), 0) FROM bodies").fetchone()[0]
        if total <= self.max_bytes:
            return
        for h, size in self._db.execute("SELECT hash, size FROM bodies ORDER BY used_at").fetchall():
            self._db.execute("DELETE FROM bodies WHERE hash=?", (h,))
            total -= size
            if total <= self.max_bytes * 0.9:
                break
        self._db.commit()
