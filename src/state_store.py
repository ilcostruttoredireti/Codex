"""
SQLite-backed state store.

Persists:
  - The Gmail historyId checkpoint (so we only poll new messages after a restart)
  - Processed Gmail message IDs (to prevent double-processing on overlapping polls)
"""

import sqlite3
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_INIT_SQL = """
CREATE TABLE IF NOT EXISTS checkpoint (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_messages (
    message_id  TEXT PRIMARY KEY,
    email       TEXT NOT NULL,
    contact_id  TEXT NOT NULL,
    status      TEXT NOT NULL,
    processed_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


class StateStore:
    def __init__(self, db_path: str = "sync_state.db"):
        self._path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.executescript(_INIT_SQL)
        self._conn.commit()
        logger.debug("State store opened at %s", db_path)

    # ------------------------------------------------------------------
    # Checkpoint (historyId)
    # ------------------------------------------------------------------

    def get_history_id(self) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM checkpoint WHERE key = 'history_id'"
        ).fetchone()
        return row[0] if row else None

    def save_history_id(self, history_id: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO checkpoint (key, value) VALUES ('history_id', ?)",
            (history_id,),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Processed messages
    # ------------------------------------------------------------------

    def is_processed(self, message_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None

    def mark_processed(
        self, message_id: str, email: str, contact_id: str, status: str
    ) -> None:
        self._conn.execute(
            """INSERT OR IGNORE INTO processed_messages
               (message_id, email, contact_id, status)
               VALUES (?, ?, ?, ?)""",
            (message_id, email, contact_id, status),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
