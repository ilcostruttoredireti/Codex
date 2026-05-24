"""
main.py — Entry point del sistema Gmail → HubSpot Sync.

Utilizzo:
    python main.py              # loop continuo
    python main.py --once       # singolo ciclo e uscita
    python main.py --dry-run    # mostra cosa farebbe senza toccare HubSpot
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Assicura che il package sia importabile da qualsiasi cwd
sys.path.insert(0, str(Path(__file__).parent))

from config import config
from logger import get_logger
from sync_engine import SyncEngine

log = get_logger("main", config.log_file)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sincronizza i mittenti Gmail con HubSpot CRM"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Esegui un singolo ciclo di sync ed esci",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Mostra le operazioni senza modificare HubSpot o applicare label Gmail",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    log.info("=" * 60)
    log.info("  Gmail → HubSpot Sync  |  avvio")
    log.info(f"  Modalità: {'once' if args.once else 'loop'}  dry-run={args.dry_run}")
    log.info("=" * 60)

    engine = SyncEngine()

    if args.dry_run:
        log.warning("⚠️  DRY-RUN: nessuna modifica verrà applicata.")
        # Override dei metodi che scrivono
        engine.hubspot.sync_sender = lambda info: _dry_run_result(info)  # type: ignore[method-assign]
        engine.gmail.mark_as_processed = lambda msg_id: None  # type: ignore[method-assign]

    try:
        engine.initialize()
    except ValueError as exc:
        log.error(f"❌  Configurazione non valida: {exc}")
        return 1

    if args.once:
        report = engine.run_once()
        report.print_summary()
    else:
        engine.run_forever()

    return 0


def _dry_run_result(info):  # type: ignore[no-untyped-def]
    """Simula la sincronizzazione senza chiamare HubSpot."""
    from hubspot_client import SyncResult, SyncStatus

    log.info(f"[DRY-RUN] Processerei: {info.email}  nome={info.full_name}  dominio={info.company_domain}")
    return SyncResult(
        status=SyncStatus.IGNORED,
        contact_email=info.email,
        contact_id=None,
        detail="dry-run",
    )


if __name__ == "__main__":
    sys.exit(main())
