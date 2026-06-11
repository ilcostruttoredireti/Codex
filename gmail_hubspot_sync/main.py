"""Entry point — run once or loop continuously."""
import argparse
import logging
import time

import config
from sync import SyncRecord, run_sync

logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

STATUS_ICON = {"created": "[+]", "updated": "[~]", "ignored": "[-]"}


def _print_results(records: list[SyncRecord]) -> None:
    if not records:
        log.info("Nessuna nuova email trovata.")
        return

    print("\n" + "=" * 65)
    print(f"{'STATO':<10} {'EMAIL':<38} {'ID HUBSPOT'}")
    print("-" * 65)
    for r in records:
        icon = STATUS_ICON.get(r.status, "   ")
        cid = r.contact_id or "—"
        print(f"{icon} {r.status.upper():<7}  {r.email:<38} {cid}")
        if r.reason:
            print(f"           Nota: {r.reason}")
    print("=" * 65 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sincronizza mittenti Gmail → HubSpot"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Esegui un singolo ciclo invece del loop continuo",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=config.SYNC_INTERVAL_SECONDS,
        help=f"Secondi tra un ciclo e l'altro (default: {config.SYNC_INTERVAL_SECONDS})",
    )
    args = parser.parse_args()

    if args.once:
        log.info("Avvio sincronizzazione singola…")
        records = run_sync()
        _print_results(records)
        return

    log.info("Avvio monitoraggio continuo (intervallo: %ds)…", args.interval)
    while True:
        try:
            log.info("Controllo nuove email…")
            records = run_sync()
            _print_results(records)
        except Exception as exc:
            log.error("Errore durante la sincronizzazione: %s", exc)
        log.info("Prossimo controllo tra %d secondi.", args.interval)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
