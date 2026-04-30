#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync Daemon
====================================
Monitors the Gmail INBOX continuously and upserts every sender as a
HubSpot contact, avoiding duplicates and filling in missing fields.

Usage:
    python gmail_hubspot_sync.py [--once] [--hours HOURS]

Flags:
    --once          Run a single sync cycle and exit (useful for cron jobs).
    --hours HOURS   On the first run (no saved state), look back this many
                    hours for existing inbox messages (default: 24).
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from config import (
    GMAIL_CREDENTIALS_FILE,
    GMAIL_TOKEN_FILE,
    HUBSPOT_ACCESS_TOKEN,
    POLL_INTERVAL_SECONDS,
    SYNC_STATE_FILE,
)
from src.gmail_client import GmailClient
from src.hubspot_client import HubSpotClient
from src.sync_engine import SyncEngine, SyncStatus

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# Keep at most this many processed IDs cached in the state file to bound growth
_MAX_CACHED_IDS = 5_000

# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state(path: str) -> dict:
    p = Path(path)
    if p.exists():
        with open(p) as f:
            state = json.load(f)
        state["processed_ids"] = set(state.get("processed_ids", []))
        return state
    return {"last_history_id": None, "processed_ids": set(), "last_run": None}


def save_state(path: str, state: dict) -> None:
    # Trim the ID set to avoid unbounded growth
    trimmed_ids = list(state["processed_ids"])[-_MAX_CACHED_IDS:]
    serializable = {
        **state,
        "processed_ids": trimmed_ids,
        "last_run": datetime.now(timezone.utc).isoformat(),
    }
    with open(path, "w") as f:
        json.dump(serializable, f, indent=2)

# ---------------------------------------------------------------------------
# Sync cycle
# ---------------------------------------------------------------------------

def run_cycle(
    engine: SyncEngine,
    gmail: GmailClient,
    state: dict,
    initial_hours: int = 24,
) -> dict:
    """
    Fetch new inbox messages, process each one, return a tally dict.
    Mutates *state* in place (last_history_id, processed_ids).
    """
    tally = {"created": 0, "updated": 0, "ignored": 0, "errors": 0}

    if state["last_history_id"]:
        messages = gmail.get_new_messages(state["last_history_id"])
    else:
        logger.info(f"Prima esecuzione — recupero messaggi delle ultime {initial_hours}h")
        messages = gmail.get_recent_inbox_messages(hours=initial_hours)

    # Always refresh history ID before processing so we don't re-process on
    # the next cycle even if the current cycle fails partway through.
    state["last_history_id"] = gmail.get_current_history_id()

    new_messages = [m for m in messages if m["id"] not in state["processed_ids"]]
    if not new_messages:
        logger.info("Nessun nuovo messaggio da elaborare.")
        return tally

    logger.info(f"Elaborazione di {len(new_messages)} nuovo/i messaggio/i...")

    for msg in new_messages:
        msg_id = msg["id"]
        state["processed_ids"].add(msg_id)
        try:
            result = engine.process_message(msg_id)
            if result is None:
                tally["ignored"] += 1
            else:
                _log_result(result)
                key = {
                    SyncStatus.CREATED: "created",
                    SyncStatus.UPDATED: "updated",
                    SyncStatus.IGNORED: "ignored",
                }.get(result.status, "ignored")
                tally[key] += 1
        except Exception:
            logger.exception(f"Errore elaborando il messaggio {msg_id}")
            tally["errors"] += 1

    return tally


def _log_result(result) -> None:
    icon = {"Creato": "✅", "Aggiornato": "🔄", "Ignorato": "⏭"}.get(result.status.value, "❓")
    logger.info(
        f"{icon} [{result.status.value:12s}]  "
        f"Email: {result.email:<40s}  "
        f"ID HubSpot: {result.contact_id}"
    )

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single sync cycle and exit",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=24,
        metavar="HOURS",
        help="Hours to look back on the first run (default: 24)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if not HUBSPOT_ACCESS_TOKEN:
        logger.error(
            "HUBSPOT_ACCESS_TOKEN non impostato. "
            "Crea un file .env con le credenziali (vedi .env.example)."
        )
        sys.exit(1)

    logger.info("Inizializzazione client Gmail...")
    gmail = GmailClient(
        credentials_file=GMAIL_CREDENTIALS_FILE,
        token_file=GMAIL_TOKEN_FILE,
    )

    logger.info("Inizializzazione client HubSpot...")
    hs = HubSpotClient(access_token=HUBSPOT_ACCESS_TOKEN)

    engine = SyncEngine(gmail=gmail, hubspot=hs, add_notes=True)
    state = load_state(SYNC_STATE_FILE)

    if args.once:
        logger.info("Modalità singola esecuzione.")
        tally = run_cycle(engine, gmail, state, initial_hours=args.hours)
        save_state(SYNC_STATE_FILE, state)
        _print_summary(tally)
        return

    logger.info(
        f"Sync avviato. Polling ogni {POLL_INTERVAL_SECONDS}s. "
        "Premi Ctrl+C per fermare."
    )
    try:
        while True:
            logger.info("─── Inizio ciclo di sync ───")
            tally = run_cycle(engine, gmail, state, initial_hours=args.hours)
            save_state(SYNC_STATE_FILE, state)
            _print_summary(tally)
            logger.info(f"Prossima verifica tra {POLL_INTERVAL_SECONDS}s...")
            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        logger.info("Sync interrotto dall'utente.")
        save_state(SYNC_STATE_FILE, state)


def _print_summary(tally: dict) -> None:
    logger.info(
        f"Riepilogo ciclo — "
        f"Creati: {tally['created']}  "
        f"Aggiornati: {tally['updated']}  "
        f"Ignorati: {tally['ignored']}  "
        f"Errori: {tally['errors']}"
    )


if __name__ == "__main__":
    main()
