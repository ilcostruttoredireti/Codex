import sqlite3
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class StateManager:
    def __init__(self, db_path="sync_state.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS processed_emails (
                    message_id       TEXT PRIMARY KEY,
                    sender_email     TEXT,
                    status           TEXT,
                    hubspot_contact_id TEXT,
                    processed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_config (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            conn.commit()

    def is_processed(self, message_id: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT message_id FROM processed_emails WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            return row is not None

    def mark_processed(
        self,
        message_id: str,
        sender_email: str,
        status: str,
        hubspot_contact_id: str = None,
    ):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO processed_emails
                    (message_id, sender_email, status, hubspot_contact_id, processed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    sender_email,
                    status,
                    hubspot_contact_id,
                    datetime.utcnow().isoformat(),
                ),
            )
            conn.commit()

    def get_config(self, key: str, default=None):
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM sync_config WHERE key = ?", (key,)
            ).fetchone()
            return row[0] if row else default

    def set_config(self, key: str, value):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sync_config (key, value) VALUES (?, ?)",
                (key, str(value)),
            )
            conn.commit()
