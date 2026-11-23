"""Basit kalıcı önbellek (SQLite): belge özetleri gibi pahalı LLM çıktıları tekrar üretilmesin."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

_LOCK = threading.Lock()


class DocCache:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS doc_cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, created REAL NOT NULL)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def get(self, key: str, max_age_s: float | None = None) -> Any | None:
        with _LOCK, self._conn() as c:
            row = c.execute("SELECT value, created FROM doc_cache WHERE key = ?", (key,)).fetchone()
        if not row or (max_age_s is not None and time.time() - row[1] > max_age_s):
            return None
        return json.loads(row[0])

    def set(self, key: str, value: Any) -> None:
        with _LOCK, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO doc_cache (key, value, created) VALUES (?, ?, ?)",
                      (key, json.dumps(value, ensure_ascii=False, default=str), time.time()))
