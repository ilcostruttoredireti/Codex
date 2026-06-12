#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
=============================
Monitora la casella Gmail in entrata e sincronizza i mittenti come contatti HubSpot.

Utilizzo:
    python main.py                  # loop continuo (default 60s)
    python main.py --once           # esegui un solo ciclo e termina
    python main.py --interval 120   # loop ogni 120 secondi
    python main.py --verbose        # output dettagliato
"""

import argparse
import logging
import os
import time

from dotenv import load_dotenv

load_dotenv()

from src import gmail_client, state_store
from src.sync_engine import run_cycle

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format=_LOG_FORMAT)
    # Silence noisy libraries
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _check_env() -> None:
    missing = [v for v in ("HUBSPOT_TOKEN",) if not os.getenv(v)]
    if missing:
        raise SystemExit(f"Variabili d'ambiente mancanti: {', '.join(missing)}\n"
                         "Copia .env.example in .env e compila i valori.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument("--once", action="store_true", help="Esegui un solo ciclo e termina")
    parser.add_argument("--interval", type=int, default=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
                        help="Secondi tra un ciclo e l'altro (default: 60)")
    parser.add_argument("--credentials", default=os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"),
                        help="Percorso del file credentials.json di Google OAuth2")
    parser.add_argument("--token", default=os.getenv("GMAIL_TOKEN_FILE", "token.json"),
                        help="Percorso del file token OAuth2 (creato automaticamente)")
    parser.add_argument("--state", default=".sync_state.json",
                        help="Percorso del file di stato per il tracking dei messaggi già processati")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    _setup_logging(args.verbose)
    _check_env()

    log = logging.getLogger(__name__)
    log.info("=== Gmail → HubSpot Sync avviato ===")

    from pathlib import Path

    gmail_svc = gmail_client.get_service(
        credentials_file=args.credentials,
        token_file=args.token,
    )
    state_path = Path(args.state)
    state = state_store.load(state_path)

    log.info("Stato caricato — messaggi già processati: %d", len(state.processed_ids))

    try:
        while True:
            results = run_cycle(gmail_svc, state)
            state_store.save(state, state_path)

            if results:
                creati   = sum(1 for r in results if r.status == "CREATO")
                aggiornati = sum(1 for r in results if r.status == "AGGIORNATO")
                ignorati = sum(1 for r in results if r.status == "IGNORATO")
                log.info("Ciclo completato — Creati: %d | Aggiornati: %d | Ignorati: %d",
                         creati, aggiornati, ignorati)

            if args.once:
                break

            log.info("Prossimo controllo tra %ds...", args.interval)
            time.sleep(args.interval)

    except KeyboardInterrupt:
        log.info("Sync interrotto dall'utente.")
    finally:
        state_store.save(state, state_path)
        log.info("Stato salvato. Bye.")


if __name__ == "__main__":
    main()
