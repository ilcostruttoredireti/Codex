"""
Gmail → HubSpot contact sync — main orchestrator.

Run directly:
    python sync.py

Or with options:
    python sync.py --once          # single pass, then exit
    python sync.py --interval 120  # override poll interval (seconds)
"""
import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

import config
import gmail_client as gmail
import hubspot_client as hs

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Result types ───────────────────────────────────────────────────────────────

@dataclass
class SyncResult:
    status: str          # "Creato" | "Aggiornato" | "Ignorato" | "Errore"
    email: str
    hubspot_id: Optional[str] = None
    name: Optional[str] = None


# ── State persistence ──────────────────────────────────────────────────────────

def _load_state() -> dict:
    path = Path(config.STATE_FILE)
    if path.exists():
        with path.open() as fh:
            return json.load(fh)
    return {"history_id": None, "processed_ids": []}


def _save_state(state: dict):
    # Cap processed_ids to last 2000 to prevent unbounded growth
    state["processed_ids"] = list(state.get("processed_ids", []))[-2000:]
    with open(config.STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=2)


# ── Per-email processing ───────────────────────────────────────────────────────

def _process_message(gmail_service, message_id: str) -> SyncResult:
    """
    Process a single Gmail message:
      1. Extract sender info
      2. Look up / create / update HubSpot contact
      3. Return a SyncResult
    """
    sender = gmail.extract_sender(gmail_service, message_id)

    if sender is None:
        return SyncResult(status="Ignorato", email="(no valid sender)")

    email = sender["email"]

    try:
        existing = hs.find_contact_by_email(email)

        if existing is None:
            contact = hs.create_contact(sender)
            return SyncResult(
                status="Creato",
                email=email,
                hubspot_id=contact["id"],
                name=sender.get("name"),
            )

        contact_id = existing["id"]
        _, changed = hs.update_contact(contact_id, sender, existing)
        return SyncResult(
            status="Aggiornato" if changed else "Ignorato",
            email=email,
            hubspot_id=contact_id,
            name=sender.get("name"),
        )

    except requests.HTTPError as exc:
        log.error("HubSpot API error for %s: %s", email, exc)
        return SyncResult(status="Errore", email=email)


# ── Output formatting ──────────────────────────────────────────────────────────

_STATUS_ICON = {
    "Creato":    "✅",
    "Aggiornato": "🔄",
    "Ignorato":  "⏭ ",
    "Errore":    "❌",
}


def _print_result(r: SyncResult):
    icon = _STATUS_ICON.get(r.status, "  ")
    name_part = f" ({r.name})" if r.name else ""
    id_part = f"  →  HubSpot ID: {r.hubspot_id}" if r.hubspot_id else ""
    log.info("%s  %-10s  %s%s%s", icon, r.status, r.email, name_part, id_part)


def _print_summary(results: list[SyncResult]):
    counts = {"Creato": 0, "Aggiornato": 0, "Ignorato": 0, "Errore": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    log.info(
        "── Riepilogo: %d creati  %d aggiornati  %d ignorati  %d errori",
        counts["Creato"], counts["Aggiornato"], counts["Ignorato"], counts["Errore"],
    )


# ── Sync pass ──────────────────────────────────────────────────────────────────

def run_sync_pass(gmail_service, state: dict) -> list[SyncResult]:
    """Run a single sync pass. Mutates *state* in place. Returns results."""
    new_ids = gmail.fetch_new_message_ids(gmail_service, state)

    if not new_ids:
        log.debug("Nessun nuovo messaggio.")
        return []

    log.info("Trovati %d nuovi messaggi da processare.", len(new_ids))
    processed: set[str] = set(state.get("processed_ids", []))
    results: list[SyncResult] = []

    for msg_id in new_ids:
        if msg_id in processed:
            continue

        result = _process_message(gmail_service, msg_id)
        _print_result(result)
        results.append(result)
        processed.add(msg_id)

    state["processed_ids"] = list(processed)
    return results


# ── Entry point ────────────────────────────────────────────────────────────────

def _validate_config():
    missing = []
    if not config.HUBSPOT_TOKEN:
        missing.append("HUBSPOT_TOKEN")
    if not Path(config.GMAIL_CREDENTIALS_FILE).exists():
        missing.append(f"GMAIL_CREDENTIALS_FILE ({config.GMAIL_CREDENTIALS_FILE})")
    if missing:
        log.error("Variabili d'ambiente mancanti: %s", ", ".join(missing))
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument(
        "--once", action="store_true",
        help="Esegui un solo ciclo e termina",
    )
    parser.add_argument(
        "--interval", type=int, default=config.POLL_INTERVAL,
        help=f"Intervallo di polling in secondi (default: {config.POLL_INTERVAL})",
    )
    args = parser.parse_args()

    _validate_config()

    log.info("═" * 60)
    log.info("  Gmail → HubSpot Contact Sync")
    log.info("  Modalità: %s | Intervallo: %ds",
             "singolo ciclo" if args.once else "continuo", args.interval)
    log.info("═" * 60)

    gmail_service = gmail.get_gmail_service()
    state = _load_state()

    try:
        while True:
            results = run_sync_pass(gmail_service, state)
            _save_state(state)

            if results:
                _print_summary(results)

            if args.once:
                break

            log.info("In attesa di %ds…", args.interval)
            time.sleep(args.interval)

    except KeyboardInterrupt:
        log.info("Sync interrotto dall'utente.")
        _save_state(state)


if __name__ == "__main__":
    main()
