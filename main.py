#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors the Gmail inbox and syncs every new sender to HubSpot CRM.

Usage
-----
    python main.py                     # continuous polling (default: every 60 s)
    python main.py --once              # single pass then exit
    python main.py --interval 120      # poll every 2 minutes
    python main.py --no-notes          # skip HubSpot note creation
    python main.py --credentials my_creds.json --token my_token.json

Environment variables (or .env file)
--------------------------------------
    HUBSPOT_ACCESS_TOKEN   required  – HubSpot Private App access token
"""

import os
import sys
import json
import time
import logging
import argparse
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv is optional; set env vars manually if not installed

from gmail_to_hubspot.gmail import GmailClient, HistoryExpiredError
from gmail_to_hubspot.hubspot_client import HubSpotClient
from gmail_to_hubspot.sync import process_sender, SyncStatus

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STATE_FILE = Path("state.json")

_STATUS_ICON = {
    SyncStatus.CREATED:  "✅",
    SyncStatus.UPDATED:  "🔄",
    SyncStatus.IGNORED:  "⏭️ ",
    SyncStatus.ERROR:    "❌",
}

# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Single sync pass
# ---------------------------------------------------------------------------


def run_once(
    gmail: GmailClient,
    hs: HubSpotClient,
    my_email: str,
    state: dict,
    create_notes: bool = True,
) -> dict:
    history_id: str | None = state.get("history_id")

    # First run: save baseline and return — nothing to process yet.
    if not history_id:
        logger.info("Primo avvio — salvo historyId corrente come baseline.")
        state["history_id"] = gmail.get_current_history_id()
        _save_state(state)
        logger.info(
            "Baseline historyId: %s  — le prossime email saranno processate.",
            state["history_id"],
        )
        return state

    try:
        messages = list(gmail.get_new_messages_since(history_id))
    except HistoryExpiredError:
        logger.warning("historyId scaduto — reset al valore corrente.")
        state["history_id"] = gmail.get_current_history_id()
        _save_state(state)
        return state

    if not messages:
        return state

    logger.info("Trovati %d nuovi messaggi in arrivo.", len(messages))

    seen: set[str] = set()
    totals = {s: 0 for s in SyncStatus}

    for msg in messages:
        from_header = gmail.get_from_header(msg["id"])
        if not from_header:
            continue

        # Deduplicate within this batch (same sender can appear multiple times)
        key = from_header.lower().strip()
        if key in seen:
            continue
        seen.add(key)

        result = process_sender(from_header, hs, my_email, create_notes=create_notes)
        totals[result.status] += 1

        icon = _STATUS_ICON[result.status]
        logger.info(
            "%s %-10s | %-45s | HubSpot ID: %s",
            icon,
            result.status.value,
            result.email,
            result.contact_id or "N/A",
        )

    logger.info(
        "Riepilogo — Creati: %d  Aggiornati: %d  Ignorati: %d  Errori: %d",
        totals[SyncStatus.CREATED],
        totals[SyncStatus.UPDATED],
        totals[SyncStatus.IGNORED],
        totals[SyncStatus.ERROR],
    )

    state["history_id"] = gmail.get_current_history_id()
    _save_state(state)
    return state


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Gmail → HubSpot Contact Sync",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--once", action="store_true", help="Esegui una sola volta ed esci")
    p.add_argument(
        "--interval",
        type=int,
        default=60,
        metavar="SECONDI",
        help="Intervallo di polling in secondi (default: 60)",
    )
    p.add_argument(
        "--credentials",
        default="gmail_credentials.json",
        help="File credenziali OAuth2 Gmail (default: gmail_credentials.json)",
    )
    p.add_argument(
        "--token",
        default="gmail_token.json",
        help="File token OAuth2 Gmail (default: gmail_token.json)",
    )
    p.add_argument(
        "--no-notes",
        action="store_true",
        help="Non creare note HubSpot per i nuovi contatti",
    )
    return p


def main() -> None:
    args = _build_parser().parse_args()

    # Validate required env var
    hubspot_token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not hubspot_token:
        logger.error("Variabile d'ambiente HUBSPOT_ACCESS_TOKEN non impostata.")
        logger.info(
            "Crea un Private App HubSpot su https://app.hubspot.com/private-apps "
            "e imposta il token come HUBSPOT_ACCESS_TOKEN."
        )
        sys.exit(1)

    if not Path(args.credentials).exists():
        logger.error("File credenziali Gmail non trovato: %s", args.credentials)
        logger.info(
            "Scarica le credenziali OAuth2 Desktop da "
            "https://console.cloud.google.com/ e salvale come '%s'.",
            args.credentials,
        )
        sys.exit(1)

    logger.info("Inizializzazione client Gmail…")
    gmail = GmailClient(args.credentials, args.token)
    my_email = gmail.get_my_email()
    logger.info("Autenticato su Gmail come: %s", my_email)

    logger.info("Inizializzazione client HubSpot…")
    hs = HubSpotClient(hubspot_token)

    state = _load_state()
    create_notes = not args.no_notes

    if args.once:
        run_once(gmail, hs, my_email, state, create_notes)
        return

    logger.info(
        "Avvio monitoraggio continuo (ogni %d s) — Ctrl+C per uscire", args.interval
    )
    while True:
        try:
            state = run_once(gmail, hs, my_email, state, create_notes)
        except KeyboardInterrupt:
            logger.info("Interrotto dall'utente.")
            break
        except Exception as exc:
            logger.error("Errore durante la sincronizzazione: %s", exc, exc_info=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
