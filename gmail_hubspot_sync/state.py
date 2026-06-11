"""Persistent state: tracks processed Gmail message IDs and run statistics."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class SyncState:
    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._data: dict = self._load()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if self._path.exists():
            try:
                with self._path.open() as fh:
                    return json.load(fh)
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Could not read state file (%s); starting fresh.", exc)
        return {
            "processed_ids": [],
            "last_run_utc": None,
            "stats": {"created": 0, "updated": 0, "skipped": 0, "errors": 0},
        }

    def save(self) -> None:
        try:
            with self._path.open("w") as fh:
                json.dump(self._data, fh, indent=2)
        except OSError as exc:
            log.error("Could not save state file: %s", exc)

    # ── Processed message IDs ─────────────────────────────────────────────────

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._data["processed_ids"]

    def mark_processed(self, message_id: str) -> None:
        ids: list = self._data["processed_ids"]
        if message_id not in ids:
            ids.append(message_id)
        # Cap list to last 50 000 IDs to prevent unbounded growth
        if len(ids) > 50_000:
            self._data["processed_ids"] = ids[-50_000:]

    # ── Last-run timestamp ────────────────────────────────────────────────────

    @property
    def last_run_utc(self) -> Optional[datetime]:
        raw = self._data.get("last_run_utc")
        if raw:
            return datetime.fromisoformat(raw)
        return None

    def touch(self) -> None:
        self._data["last_run_utc"] = datetime.now(timezone.utc).isoformat()

    # ── Statistics ────────────────────────────────────────────────────────────

    def increment(self, outcome: str) -> None:
        """outcome: 'created' | 'updated' | 'skipped' | 'errors'"""
        self._data["stats"][outcome] = self._data["stats"].get(outcome, 0) + 1

    @property
    def stats(self) -> dict:
        return dict(self._data["stats"])
