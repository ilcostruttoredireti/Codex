#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
=============================
Monitors the Gmail inbox and syncs new senders to HubSpot contacts.

Usage:
  python main.py          # continuous polling loop
  python main.py --once   # single pass, then exit (good for cron / CI)
"""
import logging
import sys

import config
from sync_engine import SyncEngine


def _setup_logging():
    level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)
    fmt = "%(asctime)s  %(levelname)-8s  %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler("gmail_hubspot_sync.log"))
    except OSError:
        pass
    logging.basicConfig(level=level, format=fmt, datefmt="%Y-%m-%d %H:%M:%S", handlers=handlers)


def _print_summary(results) -> None:
    if not results:
        print("Nessuna email da processare.")
        return

    width = 80
    print("\n" + "=" * width)
    print(f"{'STATO':<12}  {'EMAIL':<38}  {'ID HUBSPOT'}")
    print("=" * width)
    for r in results:
        print(f"{r.status.upper():<12}  {r.email:<38}  {r.contact_id or 'N/A'}")
    print("=" * width)

    created = sum(1 for r in results if r.status == "created")
    updated = sum(1 for r in results if r.status == "updated")
    ignored = sum(1 for r in results if r.status == "ignored")
    errors  = sum(1 for r in results if r.status == "error")
    print(
        f"Totale: {len(results)} email  |  "
        f"Creati: {created}  Aggiornati: {updated}  "
        f"Ignorati: {ignored}  Errori: {errors}\n"
    )


def main():
    _setup_logging()
    log = logging.getLogger(__name__)

    if not config.HUBSPOT_ACCESS_TOKEN:
        log.error("HUBSPOT_ACCESS_TOKEN mancante. Configura il file .env.")
        sys.exit(1)

    engine = SyncEngine()

    if "--once" in sys.argv:
        log.info("Modalità: singolo passaggio")
        results = engine.run_once()
        _print_summary(results)
    else:
        engine.run_loop()


if __name__ == "__main__":
    main()
