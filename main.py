#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
=============================
Monitora continuamente la casella Gmail e sincronizza i mittenti
come contatti in HubSpot, evitando duplicati.

Setup rapido
------------
1. Copia .env.example in .env e compila i valori
2. Scarica credentials.json da Google Cloud Console
   (Abilita Gmail API: https://console.cloud.google.com/apis/library/gmail.googleapis.com)
3. Crea un Private App HubSpot e copia il token in .env
4. pip install -r requirements.txt
5. python main.py
"""

import json
import os
import sys
import time
from datetime import datetime

from config import (
    GMAIL_CREDENTIALS_FILE,
    GMAIL_TOKEN_FILE,
    HUBSPOT_ACCESS_TOKEN,
    POLL_INTERVAL_SECONDS,
    STATE_FILE,
)
from gmail_client import GmailClient, GmailHistoryExpired
from hubspot_client import HubSpotClient
from sync import ContactSync, SyncResult


# ------------------------------------------------------------------ #
# State persistence                                                    #
# ------------------------------------------------------------------ #

def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as fh:
            return json.load(fh)
    return {"history_id": None}


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=2)


# ------------------------------------------------------------------ #
# Header helpers                                                       #
# ------------------------------------------------------------------ #

def _get_header(headers: list[dict], name: str) -> str:
    name_lower = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_lower:
            return h.get("value", "")
    return ""


# ------------------------------------------------------------------ #
# Core polling loop                                                    #
# ------------------------------------------------------------------ #

def _poll_once(gmail: GmailClient, sync: ContactSync, state: dict) -> dict:
    """
    Fetch new INBOX messages since state['history_id'], process each one,
    and return the updated state dict.
    """
    try:
        new_history_id, message_ids = gmail.collect_new_message_ids(state["history_id"])
    except GmailHistoryExpired:
        print(f"  ⚠  History ID scaduto — reset dello stato")
        profile = gmail.get_profile()
        state["history_id"] = profile["historyId"]
        _save_state(state)
        return state

    if new_history_id != state["history_id"]:
        state["history_id"] = new_history_id
        _save_state(state)

    if not message_ids:
        return state

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{ts}] {len(message_ids)} nuova/e email")

    for msg_id in message_ids:
        try:
            msg = gmail.get_message_metadata(msg_id)
            headers = msg.get("payload", {}).get("headers", [])
            from_hdr = _get_header(headers, "From")
            subject = _get_header(headers, "Subject")
            date = _get_header(headers, "Date")

            result = sync.process_email(from_hdr, subject, date)
            print(str(result))

        except Exception as exc:
            print(f"  ✗ Errore msg {msg_id}: {exc}")

    return state


def main() -> None:
    # ── Sanity checks ─────────────────────────────────────────────── #
    if not HUBSPOT_ACCESS_TOKEN:
        sys.exit("ERRORE: HUBSPOT_ACCESS_TOKEN non configurato nel file .env")

    if not os.path.exists(GMAIL_CREDENTIALS_FILE):
        sys.exit(
            f"ERRORE: File credenziali Gmail non trovato: {GMAIL_CREDENTIALS_FILE}\n"
            "Scarica credentials.json da Google Cloud Console e riprova."
        )

    # ── Init clients ──────────────────────────────────────────────── #
    print("Inizializzazione Gmail client (potrebbe aprire il browser per il login OAuth)...")
    gmail = GmailClient(GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE)

    print("Inizializzazione HubSpot client...")
    hubspot = HubSpotClient(HUBSPOT_ACCESS_TOKEN)
    sync = ContactSync(hubspot)

    # ── Bootstrap history ID ──────────────────────────────────────── #
    state = _load_state()
    if not state.get("history_id"):
        profile = gmail.get_profile()
        state["history_id"] = profile["historyId"]
        _save_state(state)
        print(f"History ID iniziale salvato: {state['history_id']}")

    # ── Main loop ─────────────────────────────────────────────────── #
    print(f"\nMonitoraggio Gmail avviato — polling ogni {POLL_INTERVAL_SECONDS}s")
    print("Output: Stato | Email mittente | ID HubSpot")
    print("-" * 60)

    while True:
        try:
            state = _poll_once(gmail, sync, state)
        except KeyboardInterrupt:
            print("\nInterrotto dall'utente.")
            break
        except Exception as exc:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{ts}] Errore inatteso: {exc}")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
