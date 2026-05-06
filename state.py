import sqlite3
import contextlib
from typing import Optional

from config import STATE_DB_PATH


def init_db() -> None:
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_messages (
                message_id   TEXT PRIMARY KEY,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_state (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)


def is_processed(message_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        return row is not None


def mark_processed(message_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_messages (message_id) VALUES (?)",
            (message_id,),
        )


def get_state(key: str) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM sync_state WHERE key = ?",
            (key,),
        ).fetchone()
        return row[0] if row else None


def set_state(key: str, value: Optional[str]) -> None:
    if value is None:
        with _connect() as conn:
            conn.execute("DELETE FROM sync_state WHERE key = ?", (key,))
    else:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
                (key, value),
            )


@contextlib.contextmanager
def _connect():
    conn = sqlite3.connect(STATE_DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
