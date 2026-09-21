"""A tiny sqlite-backed store for facts and reminders.

This is intentionally small: it gives Hyqs durable memory across restarts and
serves as the example backend for the in-process MCP tools. Extend the schema
as you add capabilities.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reminders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    text       TEXT NOT NULL,
    due_at     TEXT NOT NULL,
    fired      INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class MemoryStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + a lock: safe to share across the asyncio loop.
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # --- facts ---------------------------------------------------------
    def add_fact(self, text: str) -> int:
        with self._lock:
            cur = self._conn.execute("INSERT INTO facts(text) VALUES (?)", (text,))
            self._conn.commit()
            return int(cur.lastrowid)

    def list_facts(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute("SELECT * FROM facts ORDER BY id"))

    # --- reminders -----------------------------------------------------
    def add_reminder(self, chat_id: int, text: str, due_at: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO reminders(chat_id, text, due_at) VALUES (?, ?, ?)",
                (chat_id, text, due_at),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def due_reminders(self, now_iso: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM reminders WHERE fired = 0 AND due_at <= ? ORDER BY due_at",
                    (now_iso,),
                )
            )

    def mark_fired(self, reminder_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE reminders SET fired = 1 WHERE id = ?", (reminder_id,)
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
