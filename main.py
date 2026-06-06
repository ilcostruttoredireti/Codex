#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitora Gmail in arrivo e sincronizza i mittenti come contatti HubSpot.

Utilizzo:
    python main.py              # Loop continuo
    python main.py --once       # Esegui un solo ciclo e termina
    python main.py --reset      # Azzera lo stato (rielabora email recenti)
"""

import argparse
import logging
import signal
import sys
import time

from config import get_config, setup_logging
from sync_engine import GmailHubSpotSyncer, SyncStatus

logger = logging.getLogger(__name__)

_running = True


def _handle_signal(signum, frame):
    global _running
    logger.info("Segnale %s ricevuto, arresto in corso...", signum)
    _running = False


def print_summary(results: list):
    if not results:
        return
    created = sum(1 for r in results if r.status == SyncStatus.CREATED)
    updated = sum(1 for r in results if r.status == SyncStatus.UPDATED)
    ignored = sum(1 for r in results if r.status == SyncStatus.IGNORED)
    errors = sum(1 for r in results if r.status == SyncStatus.ERROR)

    print(f"\n{'─'*55}")
    print(f"  Ciclo completato: {len(results)} email processate")
    print(f"  ✓ Creati: {created}  ↺ Aggiornati: {updated}  "
          f"– Ignorati: {ignored}  ✗ Errori: {errors}")
    print(f"{'─'*55}")

    for r in results:
        icon = {"Creato": "✓", "Aggiornato": "↺", "Ignorato": "–", "Errore": "✗"}.get(r.status.value, "?")
        line = f"  {icon} [{r.status.value}] {r.email}"
        if r.contact_id:
            line += f"  (ID: {r.contact_id})"
        if r.reason:
            line += f"  — {r.reason}"
        print(line)
    print()


def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument("--once", action="store_true", help="Esegui un solo ciclo e termina")
    parser.add_argument("--reset", action="store_true", help="Azzera lo stato di sincronizzazione")
    args = parser.parse_args()

    try:
        config = get_config()
    except ValueError as e:
        print(f"ERRORE CONFIGURAZIONE:\n{e}", file=sys.stderr)
        sys.exit(1)

    setup_logging(config["log_level"])

    if args.reset:
        import os
        state_file = config["state_file"]
        if os.path.exists(state_file):
            os.remove(state_file)
            print(f"Stato azzerato: {state_file}")
        else:
            print("Nessuno stato da azzerare.")
        if not args.once:
            sys.exit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        syncer = GmailHubSpotSyncer(config)
    except FileNotFoundError as e:
        print(f"ERRORE:\n{e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Errore di inizializzazione: {e}", file=sys.stderr)
        sys.exit(1)

    interval = config["poll_interval"]

    if args.once:
        logger.info("Esecuzione singola.")
        results = syncer.run_once()
        print_summary(results)
        return

    logger.info("Avvio monitoraggio continuo (intervallo: %ds). Ctrl+C per fermare.", interval)

    while _running:
        try:
            results = syncer.run_once()
            print_summary(results)
        except Exception as e:
            logger.error("Errore nel ciclo di sync: %s", e, exc_info=True)

        if _running:
            logger.debug("Prossimo ciclo tra %ds...", interval)
            # Attendi in blocchi per reagire rapidamente a SIGINT
            for _ in range(interval):
                if not _running:
                    break
                time.sleep(1)

    logger.info("Sync terminato.")


if __name__ == "__main__":
    main()
