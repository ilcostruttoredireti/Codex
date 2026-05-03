"""
Gmail → HubSpot contact sync daemon.

Continuously polls Gmail inbox for new messages and syncs senders to HubSpot.

Usage:
    python main.py

Required env vars (see .env.example):
    GMAIL_CREDENTIALS_PATH   Path to Google OAuth2 client_secret JSON
    GMAIL_TOKEN_PATH         Path where the OAuth token will be stored
    HUBSPOT_ACCESS_TOKEN     HubSpot Private App access token

Optional:
    POLL_INTERVAL_SECONDS    How often to check for new emails (default: 60)
    IGNORED_DOMAINS_EXTRA    Comma-separated extra domains to skip
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from sync import EmailContactSync, SyncResult

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

STATE_FILE = Path(".sync_state.json")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

STATUS_ICON = {"Creato": "✅", "Aggiornato": "🔄", "Ignorato": "⏭️"}


def load_state() -> dict:
    if STATE_FILE.exists():
        with STATE_FILE.open() as f:
            return json.load(f)
    return {"processed_ids": [], "last_run_ts": 0}


def save_state(state: dict) -> None:
    with STATE_FILE.open("w") as f:
        json.dump(state, f, indent=2)


def print_result(result: SyncResult) -> None:
    icon = STATUS_ICON.get(result.status, "❓")
    id_str = result.hubspot_id or "N/A"
    detail = f"  ({result.reason})" if result.reason and result.status == "Ignorato" else ""
    print(f"  {icon} {result.status:<12} | {result.email:<45} | ID HubSpot: {id_str}{detail}")


def run_cycle(sync: EmailContactSync, state: dict) -> dict:
    last_ts = state.get("last_run_ts", 0)
    processed_ids: set[str] = set(state.get("processed_ids", []))

    # Gmail 'after:' filter expects Unix seconds.
    after_seconds = last_ts / 1000 if last_ts else None
    messages = sync.gmail.get_new_messages(after_timestamp_seconds=after_seconds)

    new_messages = [m for m in messages if m["id"] not in processed_ids]

    if not new_messages:
        return state

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n[{now_str}] {len(new_messages)} nuova/e email trovata/e")
    print(f"  {'STATO':<12}   {'EMAIL':<45}   ID HUBSPOT")
    print("  " + "-" * 75)

    counters = {"Creato": 0, "Aggiornato": 0, "Ignorato": 0}

    for msg in new_messages:
        result = sync.process_message(msg["id"])
        processed_ids.add(msg["id"])
        print_result(result)
        counters[result.status] = counters.get(result.status, 0) + 1

    print(f"\n  Riepilogo: ✅ {counters['Creato']} creati  🔄 {counters['Aggiornato']} aggiornati  ⏭️ {counters['Ignorato']} ignorati")

    # Keep only the last 2000 processed IDs to bound memory / file size.
    state["processed_ids"] = list(processed_ids)[-2000:]
    state["last_run_ts"] = int(datetime.now(timezone.utc).timestamp() * 1000)
    save_state(state)
    return state


def main() -> None:
    print("=" * 60)
    print("  Gmail → HubSpot Contact Sync")
    print(f"  Polling ogni {POLL_INTERVAL}s  |  avvio: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    try:
        sync = EmailContactSync()
    except EnvironmentError as exc:
        logger.error("Configurazione mancante: %s", exc)
        sys.exit(1)

    state = load_state()

    while True:
        try:
            state = run_cycle(sync, state)
        except KeyboardInterrupt:
            print("\n\nMonitoraggio interrotto dall'utente.")
            break
        except Exception as exc:
            logger.error("Errore nel ciclo di sync: %s", exc, exc_info=True)

        try:
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("\n\nMonitoraggio interrotto dall'utente.")
            break


if __name__ == "__main__":
    main()
