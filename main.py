#!/usr/bin/env python3
"""Entry point for the Gmail → HubSpot contact sync."""

import argparse
import sys

import gmail_client as gmail
import sync


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sincronizza i mittenti Gmail in HubSpot come contatti."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Esegui un singolo ciclo e poi esci (utile per test).",
    )
    args = parser.parse_args()

    try:
        service = gmail.build_service()
    except FileNotFoundError as exc:
        print(f"Errore: {exc}")
        print(
            "Assicurati di aver scaricato credentials.json da Google Cloud Console "
            "e di averlo posizionato nella directory corrente."
        )
        sys.exit(1)

    if args.once:
        print("=== Esecuzione singola ===")
        results = sync.run_once(service)
        if not results:
            print("Nessuna nuova email trovata.")
        else:
            print(f"\n{'Stato':<14} {'Email':<40} {'ID HubSpot'}")
            print("-" * 70)
            for r in results:
                sync._print_result(r)
        print(f"\nTotale elaborati: {len(results)}")
    else:
        print("=== Gmail → HubSpot Sync avviato ===")
        print("Premi Ctrl+C per terminare.\n")
        try:
            sync.run_loop(service)
        except KeyboardInterrupt:
            print("\nSync interrotto dall'utente.")


if __name__ == "__main__":
    main()
