#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitora la casella Gmail in arrivo ed esegue l'upsert dei mittenti in HubSpot.

Utilizzo:
  python main.py            # ciclo continuo
  python main.py --once     # esecuzione singola (utile per test o cron)
"""

import argparse
import logging
import sys
import time

from src.config import load_config
from src.state import load_state, save_state
from src.sync import GmailHubSpotSync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("sync.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def print_summary(results: list) -> None:
    if not results:
        return
    created = sum(1 for r in results if r.status == "created")
    updated = sum(1 for r in results if r.status == "updated")
    ignored = sum(1 for r in results if r.status == "ignored")

    print()
    print("─" * 60)
    print(f"  Riepilogo ciclo: {len(results)} email elaborate")
    print(f"  ✅ Creati   : {created}")
    print(f"  🔄 Aggiornati: {updated}")
    print(f"  ⏭  Ignorati : {ignored}")
    print("─" * 60)
    print()

    header = f"{'Stato':<10} {'Email':<42} {'ID HubSpot'}"
    print(header)
    print("─" * len(header))
    for r in results:
        print(f"{r.status.upper():<10} {r.email:<42} {r.contact_id or 'N/A'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single sync cycle and exit (useful for cron / testing)",
    )
    args = parser.parse_args()

    config = load_config()
    state = load_state(config.state_file)
    syncer = GmailHubSpotSync(config)

    if args.once:
        logger.info("Esecuzione singola avviata")
        state, results = syncer.run_cycle(state)
        save_state(state, config.state_file)
        print_summary(results)
        return

    logger.info(
        "Sync Gmail→HubSpot avviato — polling ogni %ds  (Ctrl+C per uscire)",
        config.poll_interval_seconds,
    )

    while True:
        try:
            state, results = syncer.run_cycle(state)
            save_state(state, config.state_file)
            print_summary(results)
        except KeyboardInterrupt:
            logger.info("Interruzione richiesta dall'utente — uscita")
            break
        except Exception as exc:
            logger.error("Errore nel ciclo di sync: %s", exc, exc_info=True)

        try:
            time.sleep(config.poll_interval_seconds)
        except KeyboardInterrupt:
            logger.info("Interruzione richiesta dall'utente — uscita")
            break


if __name__ == "__main__":
    main()
