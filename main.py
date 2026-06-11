#!/usr/bin/env python3
"""Gmail → HubSpot contact sync.

Prima esecuzione:
  1. Crea un file .env con HUBSPOT_ACCESS_TOKEN=<token>
  2. Scarica credentials.json dalla Google Cloud Console (OAuth2 Desktop)
  3. Esegui:  python main.py
     Al primo avvio si aprirà il browser per autorizzare l'accesso Gmail.

Modalità d'uso:
  python main.py                  # loop continuo ogni 5 minuti
  python main.py --once           # singolo ciclo ed esci
  python main.py --interval 60    # loop ogni 60 secondi
  python main.py --no-notes       # non creare note su HubSpot
"""

import argparse
import logging
import os
import sys
import time

from dotenv import load_dotenv

from gmail_reader import GmailReader
from hubspot_writer import HubSpotWriter
from state_manager import StateManager
from sync_engine import Status, SyncEngine

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

_ICONS = {
    Status.CREATED: "✅",
    Status.UPDATED: "🔄",
    Status.IGNORED: "⏭️",
    Status.ERROR:   "❌",
}


def _run_cycle(
    engine: SyncEngine,
    state: StateManager,
    gmail: GmailReader,
    max_msgs: int,
) -> None:
    logger.info("Avvio ciclo di sincronizzazione…")
    messages = gmail.get_messages(
        since_epoch=state.last_epoch or None,
        max_results=max_msgs,
    )

    processed = 0
    stats: dict[Status, int] = {s: 0 for s in Status}

    for msg in messages:
        msg_id = msg.get("id", "")
        if state.is_processed(msg_id):
            continue

        result = engine.process_message(msg)
        state.mark_processed(msg_id)
        stats[result.status] += 1
        processed += 1

        extra = f"  ↳ {result.reason}" if result.reason else ""
        logger.info(
            "%s %-10s | %-42s | ID: %s%s",
            _ICONS[result.status],
            result.status.value,
            result.email,
            result.contact_id or "—",
            extra,
        )

    state.update_epoch(int(time.time()))
    state.save()

    logger.info(
        "Ciclo completato — %d elaborate  "
        "(✅ %d creati | 🔄 %d aggiornati | ⏭️ %d ignorati | ❌ %d errori)",
        processed,
        stats[Status.CREATED],
        stats[Status.UPDATED],
        stats[Status.IGNORED],
        stats[Status.ERROR],
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Sincronizza i mittenti Gmail come contatti HubSpot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--interval", type=int, default=300, metavar="SEC",
                   help="Secondi tra un ciclo e il successivo (default: 300)")
    p.add_argument("--once", action="store_true",
                   help="Esegui un solo ciclo ed esci")
    p.add_argument("--max-msgs", type=int, default=50, metavar="N",
                   help="Max email per ciclo (default: 50)")
    p.add_argument("--no-notes", action="store_true",
                   help="Non creare note su HubSpot per ogni nuovo contatto")
    p.add_argument("--credentials", default="credentials.json",
                   help="File OAuth2 credentials Google (default: credentials.json)")
    p.add_argument("--token", default="token.pickle",
                   help="File token OAuth2 — auto-generato al primo avvio")
    p.add_argument("--state", default="sync_state.json",
                   help="File JSON per lo stato (default: sync_state.json)")
    return p


def main() -> int:
    args = _build_arg_parser().parse_args()

    hubspot_token = os.environ.get("HUBSPOT_ACCESS_TOKEN", "").strip()
    if not hubspot_token:
        logger.error(
            "HUBSPOT_ACCESS_TOKEN non trovato.\n"
            "Aggiungilo nel file .env oppure impostalo come variabile d'ambiente."
        )
        return 1

    if not os.path.exists(args.credentials):
        logger.error(
            "File credentials Gmail non trovato: %s\n"
            "Scarica il file OAuth2 (Desktop App) dalla Google Cloud Console.",
            args.credentials,
        )
        return 1

    gmail = GmailReader(credentials_path=args.credentials, token_path=args.token)
    hubspot = HubSpotWriter(access_token=hubspot_token)
    engine = SyncEngine(gmail=gmail, hubspot=hubspot, create_notes=not args.no_notes)
    state = StateManager(state_file=args.state)

    logger.info("=== Gmail → HubSpot Sync avviato ===")
    logger.info(
        "Intervallo: %ds  |  Note HubSpot: %s  |  Max msg/ciclo: %d",
        args.interval,
        "sì" if not args.no_notes else "no",
        args.max_msgs,
    )

    if args.once:
        _run_cycle(engine, state, gmail, args.max_msgs)
        return 0

    try:
        while True:
            try:
                _run_cycle(engine, state, gmail, args.max_msgs)
            except Exception as exc:
                logger.error("Errore nel ciclo: %s", exc, exc_info=True)
            logger.info("Prossimo ciclo tra %d secondi — Ctrl+C per uscire", args.interval)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("Interruzione manuale. Uscita.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
