#!/usr/bin/env python3
"""
Gmail → HubSpot contact sync daemon.

Polls Gmail inbox at a configurable interval, extracts unique senders,
and creates or updates matching HubSpot contacts. Uses the sender email
as the deduplication key.

Setup:
  1. Copy .env.example → .env and fill in credentials.
  2. Download Google OAuth credentials.json from Google Cloud Console.
  3. pip install -r requirements.txt
  4. python main.py
"""
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import List

from config import (
    GMAIL_CREDENTIALS_FILE,
    GMAIL_TOKEN_FILE,
    HUBSPOT_API_KEY,
    POLL_INTERVAL_SECONDS,
)
from contact_extractor import extract_sender_info, SenderInfo
from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from sync_engine import SyncEngine, SyncResult

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

STATE_FILE = ".sync_state.json"
MAX_STORED_IDS = 5_000


def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"processed_ids": [], "last_timestamp": None}


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def _run_cycle(
    gmail: GmailClient,
    engine: SyncEngine,
    state: dict,
) -> dict:
    after_ts = state.get("last_timestamp")
    messages = gmail.get_inbox_messages(after_unix_ts=after_ts)

    processed: set = set(state.get("processed_ids", []))
    new_max_ts: int = after_ts or 0
    results: List[SyncResult] = []

    for msg in messages:
        msg_id: str = msg["id"]
        if msg_id in processed:
            continue

        from_header = gmail.get_header(msg, "From")
        if not from_header:
            processed.add(msg_id)
            continue

        sender: SenderInfo | None = extract_sender_info(from_header)
        if not sender:
            logger.debug("Skipping automated sender: %s", from_header)
            processed.add(msg_id)
            continue

        result = engine.sync(sender)
        results.append(result)
        processed.add(msg_id)

        msg_ts = int(msg.get("internalDate", 0)) // 1000
        if msg_ts > new_max_ts:
            new_max_ts = msg_ts

    if results:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n── {ts} {'─' * 50}")
        for r in results:
            print(r)
        counts = {s: sum(1 for r in results if r.status == s)
                  for s in ("created", "updated", "skipped")}
        print(f"   ▶ Creati: {counts['created']}  Aggiornati: {counts['updated']}  Ignorati: {counts['skipped']}")
    else:
        logger.info("Nessuna nuova email da processare.")

    return {
        "processed_ids": list(processed)[-MAX_STORED_IDS:],
        "last_timestamp": new_max_ts or None,
        "last_run": datetime.now(timezone.utc).isoformat(),
    }


def main() -> None:
    if not HUBSPOT_API_KEY:
        raise SystemExit("❌  HUBSPOT_API_KEY non impostato — controlla il file .env")
    if not os.path.exists(GMAIL_CREDENTIALS_FILE):
        raise SystemExit(f"❌  File OAuth Gmail non trovato: {GMAIL_CREDENTIALS_FILE}")

    print("🔗  Connessione a Gmail e HubSpot …")
    gmail = GmailClient(GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE)
    hs = HubSpotClient(HUBSPOT_API_KEY)
    engine = SyncEngine(hs)

    print(f"✅  Avviato — polling ogni {POLL_INTERVAL_SECONDS}s  (Ctrl+C per fermare)\n")

    state = _load_state()
    try:
        while True:
            try:
                state = _run_cycle(gmail, engine, state)
                _save_state(state)
            except Exception as exc:
                logger.error("Errore nel ciclo di sync: %s", exc, exc_info=True)
            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\n👋  Sync fermato.")


if __name__ == "__main__":
    main()
