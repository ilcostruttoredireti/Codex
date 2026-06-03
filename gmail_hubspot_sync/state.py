import os
import sqlite3
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class StateManager:
    """
    Gestisce lo stato di sincronizzazione su SQLite.
    Traccia gli ID dei messaggi già processati per evitare duplicati.
    """

    def __init__(self, db_path: str):
        dir_path = os.path.dirname(db_path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        self.db_path = db_path
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS processed_messages (
                    message_id   TEXT PRIMARY KEY,
                    email        TEXT NOT NULL,
                    status       TEXT NOT NULL,
                    hubspot_id   TEXT,
                    processed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sync_state (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)
        logger.debug(f"State DB inizializzato: {self.db_path}")

    def is_processed(self, message_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
            return row is not None

    def mark_processed(
        self,
        message_id: str,
        email: str,
        status: str,
        hubspot_id: Optional[str],
    ):
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO processed_messages
                   (message_id, email, status, hubspot_id, processed_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    message_id,
                    email,
                    status,
                    hubspot_id,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def get_last_sync_time(self) -> Optional[datetime]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM sync_state WHERE key = 'last_sync_time'"
            ).fetchone()
            if row:
                return datetime.fromisoformat(row["value"])
            return None

    def update_last_sync_time(self, dt: Optional[datetime] = None):
        value = (dt or datetime.now(timezone.utc)).isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sync_state (key, value) VALUES ('last_sync_time', ?)",
                (value,),
            )

    def get_stats(self) -> dict:
        with self._conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM processed_messages"
            ).fetchone()["c"]
            rows = conn.execute(
                "SELECT status, COUNT(*) AS c FROM processed_messages GROUP BY status"
            ).fetchall()
            return {"totale": total, "per_stato": {r["status"]: r["c"] for r in rows}}
