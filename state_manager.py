"""SQLite-backed state: track processed message IDs and last-check timestamp."""

import sqlite3
import time
from pathlib import Path
from typing import Optional

DB_PATH = Path("sync_state.db")


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS processed_emails (
                message_id  TEXT PRIMARY KEY,
                processed_at INTEGER NOT NULL,
                sender_email TEXT,
                hubspot_id   TEXT,
                status       TEXT
            );
            CREATE TABLE IF NOT EXISTS sync_state (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)


def is_processed(message_id: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_emails WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        return row is not None


def mark_processed(
    message_id: str,
    sender_email: str,
    hubspot_id: Optional[str],
    status: str,
) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO processed_emails
                (message_id, processed_at, sender_email, hubspot_id, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (message_id, int(time.time()), sender_email, hubspot_id or "", status),
        )


def get_last_check_timestamp() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT value FROM sync_state WHERE key = 'last_check_ts'",
        ).fetchone()
        return int(row[0]) if row else 0


def set_last_check_timestamp(ts: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sync_state (key, value) VALUES ('last_check_ts', ?)",
            (str(ts),),
        )
