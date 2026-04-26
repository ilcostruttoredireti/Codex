"""Lightweight SQLite store to track already-processed Gmail message IDs."""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "processed.db"


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_messages (
            message_id TEXT PRIMARY KEY,
            processed_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()
    return conn


def is_processed(conn: sqlite3.Connection, message_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,)
    ).fetchone()
    return row is not None


def mark_processed(conn: sqlite3.Connection, message_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO processed_messages (message_id) VALUES (?)",
        (message_id,),
    )
    conn.commit()
