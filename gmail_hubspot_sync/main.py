"""Entry point.

Usage:
    python -m gmail_hubspot_sync.main           # run once and print report
    python -m gmail_hubspot_sync.main --watch   # continuous polling
"""
import argparse
import sys

from .sync import run_continuous, run_once


def _print_table(rows: list[dict]) -> None:
    if not rows:
        print("Nessuna nuova email da processare.")
        return

    col_w = {"stato": 10, "email": 42, "id_hubspot": 16, "nota": 30}
    header = (
        f"{'Stato':<{col_w['stato']}}  "
        f"{'Email':<{col_w['email']}}  "
        f"{'ID HubSpot':<{col_w['id_hubspot']}}  "
        f"{'Note'}"
    )
    sep = "-" * len(header)
    print(sep)
    print(header)
    print(sep)
    for r in rows:
        print(
            f"{r['stato'].value:<{col_w['stato']}}  "
            f"{r['email']:<{col_w['email']}}  "
            f"{str(r['id_hubspot'] or ''):<{col_w['id_hubspot']}}  "
            f"{r['nota']}"
        )
    print(sep)

    created = sum(1 for r in rows if r["stato"].value == "Creato")
    updated = sum(1 for r in rows if r["stato"].value == "Aggiornato")
    ignored = sum(1 for r in rows if r["stato"].value == "Ignorato")
    print(f"\nTotale: {len(rows)}  |  Creati: {created}  Aggiornati: {updated}  Ignorati: {ignored}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Gmail senders → HubSpot contacts")
    parser.add_argument("--watch", action="store_true", help="Polling continuo")
    args = parser.parse_args()

    if args.watch:
        run_continuous()
    else:
        rows = run_once()
        _print_table(rows)


if __name__ == "__main__":
    main()
