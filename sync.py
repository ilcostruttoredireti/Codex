"""Core sync loop: reads Gmail, syncs senders to HubSpot."""

import json
import os
import time
from dataclasses import dataclass

import config
import gmail_client as gmail
import hubspot_client as hubspot


@dataclass
class SyncResult:
    status: str       # Creato / Aggiornato / Ignorato / Errore
    email: str
    contact_id: str


def _load_state() -> dict:
    if os.path.exists(config.STATE_FILE):
        with open(config.STATE_FILE) as f:
            return json.load(f)
    return {"processed_ids": [], "history_id": None}


def _save_state(state: dict) -> None:
    with open(config.STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def process_batch(service, state: dict) -> list[SyncResult]:
    """Fetch new messages and sync each sender. Returns results for this batch."""
    history_id = state.get("history_id")
    processed_ids: list = state.get("processed_ids", [])
    # Keep only the last 1000 IDs to bound memory
    processed_set = set(processed_ids[-1000:])

    messages = gmail.fetch_new_messages(service, since_history_id=history_id)

    results: list[SyncResult] = []

    for msg in messages:
        msg_id = msg["id"]
        if msg_id in processed_set:
            continue

        try:
            sender = gmail.get_sender_info(service, msg_id)
            if sender is None:
                processed_set.add(msg_id)
                continue

            status, contact_id = hubspot.sync_contact(
                email=sender.email,
                first_name=sender.first_name,
                last_name=sender.last_name,
                domain=sender.domain,
                subject=sender.subject,
                date=sender.date,
            )
            results.append(SyncResult(status=status, email=sender.email, contact_id=contact_id))
        except Exception as exc:
            results.append(SyncResult(status=f"Errore: {exc}", email="?", contact_id=""))

        processed_set.add(msg_id)

    # Refresh historyId after processing
    try:
        state["history_id"] = gmail.get_current_history_id(service)
    except Exception:
        pass

    state["processed_ids"] = list(processed_set)
    _save_state(state)
    return results


def run_once(service) -> list[SyncResult]:
    """Run a single sync pass. Useful for testing."""
    state = _load_state()
    return process_batch(service, state)


def run_loop(service) -> None:
    """Poll Gmail indefinitely, syncing new senders to HubSpot."""
    state = _load_state()

    # On first run, record the current historyId (do not process existing mail)
    if state.get("history_id") is None:
        print("Prima esecuzione: registro lo stato attuale della casella email.")
        print("Le email future saranno sincronizzate a partire dal prossimo ciclo.")
        state["history_id"] = gmail.get_current_history_id(service)
        _save_state(state)
        print(f"historyId registrato: {state['history_id']}")
        print(f"In attesa di nuove email (polling ogni {config.POLL_INTERVAL_SECONDS}s)...\n")
        time.sleep(config.POLL_INTERVAL_SECONDS)

    while True:
        try:
            results = process_batch(service, state)
            for r in results:
                _print_result(r)
        except Exception as exc:
            print(f"[errore batch] {exc}")

        time.sleep(config.POLL_INTERVAL_SECONDS)


def _print_result(r: SyncResult) -> None:
    print(f"  Stato: {r.status:<12}  Email: {r.email:<40}  ID HubSpot: {r.contact_id}")
