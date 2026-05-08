"""
Gmail → HubSpot contact sync
============================
Continuously polls Gmail INBOX for new messages and syncs the sender
as a HubSpot contact (create or update), avoiding duplicates.

Usage
-----
    cp .env.example .env          # fill in your tokens
    pip install -r requirements.txt
    python main.py

On the first run you will be redirected to a browser to authorise Gmail
access via OAuth 2.0. The token is cached in token.json for subsequent runs.
"""

import os
import time
import json
import signal
import sys
from datetime import datetime

from dotenv import load_dotenv

from gmail_client import build_gmail_service, fetch_new_messages
from hubspot_client import build_hubspot_client, sync_contact

load_dotenv()

CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE = ".sync_state.json"

_running = True


def _handle_signal(sig, frame):
    global _running
    print("\n[sync] Interruzione richiesta – chiusura in corso…")
    _running = False


def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as fh:
            return json.load(fh)
    return {"history_id": None, "processed_ids": []}


def _save_state(state: dict):
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=2)


def _print_result(result: dict):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    contact_id = result.get("contact_id") or "–"
    print(
        f"  [{ts}] Stato: {result['status']:<30} "
        f"Email: {result['email']:<40} "
        f"ID HubSpot: {contact_id}"
    )


def run():
    if not HUBSPOT_TOKEN:
        sys.exit("Errore: HUBSPOT_ACCESS_TOKEN non impostato nel file .env")

    if not os.path.exists(CREDENTIALS_FILE):
        sys.exit(
            f"Errore: file credenziali Gmail '{CREDENTIALS_FILE}' non trovato.\n"
            "Scarica il file OAuth 2.0 dalla Google Cloud Console e salvalo come credentials.json"
        )

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    print("[sync] Avvio Gmail → HubSpot contact sync")
    print(f"[sync] Polling ogni {POLL_INTERVAL}s  |  Ctrl+C per interrompere\n")

    gmail = build_gmail_service(CREDENTIALS_FILE, TOKEN_FILE)
    hubspot = build_hubspot_client(HUBSPOT_TOKEN)

    state = _load_state()
    processed_ids: set = set(state.get("processed_ids", []))

    while _running:
        try:
            messages, new_history_id = fetch_new_messages(gmail, state.get("history_id"))

            new_count = 0
            for msg in messages:
                msg_id = msg["id"]
                if msg_id in processed_ids:
                    continue

                sender = msg["sender"]
                print(f"\n[sync] Nuova email — Da: {msg['from_header']} | Oggetto: {msg['subject']}")
                result = sync_contact(hubspot, sender)
                _print_result(result)

                processed_ids.add(msg_id)
                new_count += 1

                # Keep processed_ids bounded (last 5 000 message IDs)
                if len(processed_ids) > 5000:
                    processed_ids = set(list(processed_ids)[-5000:])

            state["history_id"] = new_history_id
            state["processed_ids"] = list(processed_ids)
            _save_state(state)

            if new_count == 0:
                print(f"[sync] Nessuna nuova email — prossimo controllo tra {POLL_INTERVAL}s")

        except Exception as exc:
            print(f"[sync] Errore durante il ciclo: {exc}")

        if _running:
            time.sleep(POLL_INTERVAL)

    print("[sync] Terminato.")


if __name__ == "__main__":
    run()
