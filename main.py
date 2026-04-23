#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Polls Gmail for new inbound emails and upserts sender contacts in HubSpot.

Usage:
    python main.py              # continuous polling (Ctrl-C to stop)
    python main.py --once       # single pass then exit
    python main.py --reset      # clear saved state and start fresh
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from contact_syncer import ContactSyncer

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("sync.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
STATE_FILE = Path("sync_state.json")
MAX_PROCESSED_IDS = 10_000   # cap the in-memory/disk set to avoid unbounded growth
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

STATUS_ICONS = {
    "CREATO": "✅",
    "AGGIORNATO": "🔄",
    "IGNORATO": "⏭️",
}


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not read state file: %s — starting fresh.", exc)
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_result(result: dict) -> None:
    icon = STATUS_ICONS.get(result["status"], "❓")
    contact_id = result.get("hubspot_id") or "-"
    reason = f" ({result['reason']})" if result.get("reason") else ""
    print(
        f"  {icon} [{result['status']:10}]  {result.get('email', 'N/A')}"
        f"  ID: {contact_id}{reason}"
    )


def print_summary(results: list[dict]) -> None:
    counts = {"CREATO": 0, "AGGIORNATO": 0, "IGNORATO": 0}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    logger.info(
        "Ciclo completato → Creati: %d | Aggiornati: %d | Ignorati: %d",
        counts["CREATO"],
        counts["AGGIORNATO"],
        counts["IGNORATO"],
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_clients() -> tuple[GmailClient, HubSpotClient, ContactSyncer]:
    token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not token:
        sys.exit(
            "ERROR: HUBSPOT_ACCESS_TOKEN non trovato. "
            "Configura il file .env (vedi .env.example)."
        )

    gmail = GmailClient(
        credentials_file=os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"),
        token_file=os.getenv("GMAIL_TOKEN_FILE", "token.json"),
    )
    hubspot = HubSpotClient(access_token=token)
    syncer = ContactSyncer(
        gmail=gmail,
        hubspot=hubspot,
        create_timeline=os.getenv("CREATE_TIMELINE", "true").lower() == "true",
        add_inbound_tag=os.getenv("ADD_INBOUND_TAG", "true").lower() == "true",
    )
    return gmail, hubspot, syncer


def run_cycle(
    gmail: GmailClient,
    syncer: ContactSyncer,
    state: dict,
) -> dict:
    """Execute one polling cycle. Returns the updated state dict."""
    last_history_id: str | None = state.get("last_history_id")
    processed_ids: set[str] = set(state.get("processed_ids", []))

    logger.info("Controllo email in arrivo (history_id=%s)…", last_history_id or "iniziale")
    messages = gmail.get_new_messages(since_history_id=last_history_id)
    new_history_id = gmail.get_current_history_id()

    new_messages = [m for m in messages if m and m["id"] not in processed_ids]

    if not new_messages:
        logger.info("Nessuna nuova email da processare.")
    else:
        logger.info("Trovate %d nuove email — avvio sync…", len(new_messages))
        results = []
        for msg in new_messages:
            result = syncer.process_message(msg)
            print_result(result)
            results.append(result)
            processed_ids.add(msg["id"])
        print_summary(results)

    # Keep set bounded
    trimmed = list(processed_ids)[-MAX_PROCESSED_IDS:]
    return {
        "processed_ids": trimmed,
        "last_history_id": new_history_id,
        "last_run": datetime.now(timezone.utc).isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--once", action="store_true", help="Esegui un solo ciclo ed esci")
    parser.add_argument("--reset", action="store_true", help="Azzera lo stato salvato")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("Gmail → HubSpot Sync avviato")
    logger.info("Polling ogni %ds | Timeline: %s | Tag: %s",
                POLL_INTERVAL,
                os.getenv("CREATE_TIMELINE", "true"),
                os.getenv("ADD_INBOUND_TAG", "true"))
    logger.info("=" * 60)

    if args.reset and STATE_FILE.exists():
        STATE_FILE.unlink()
        logger.info("Stato precedente rimosso.")

    gmail, _, syncer = build_clients()
    state = load_state()

    try:
        while True:
            try:
                state = run_cycle(gmail, syncer, state)
                save_state(state)
            except Exception as exc:
                logger.error("Errore durante il ciclo di sync: %s", exc, exc_info=True)

            if args.once:
                break

            logger.info("Prossimo controllo tra %ds…", POLL_INTERVAL)
            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Interruzione manuale. Salvataggio stato…")
        save_state(state)
        logger.info("Sync terminato.")


if __name__ == "__main__":
    main()
