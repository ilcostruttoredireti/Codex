"""
Gmail → HubSpot contact sync.

For every unread inbox email:
  1. Extract sender info
  2. Skip automated / no-reply senders
  3. Deduplicate by email address
  4. In HubSpot: CREATE if new, UPDATE missing fields if existing

Output per processed email:
  status  : CREATO | AGGIORNATO | IGNORATO
  email   : sender address
  id      : HubSpot contact ID (or None)
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from config import Config
from contact_parser import ContactParser
from gmail_reader import GmailReader
from hubspot_client import HubSpotClient


def run() -> list[dict]:
    config = Config()
    gmail = GmailReader(config)
    hubspot = HubSpotClient(config)
    parser = ContactParser()

    now = datetime.now(timezone.utc).isoformat()
    print(f"[{now}] Avvio sincronizzazione Gmail → HubSpot")
    print(f"  Query Gmail: {config.gmail_query}")

    messages = gmail.get_unread_messages(
        query=config.gmail_query,
        max_results=config.max_emails,
    )
    print(f"  Email trovate: {len(messages)}")

    results: list[dict] = []
    seen: set[str] = set()

    for msg in messages:
        contact = parser.parse(msg)
        if not contact:
            continue

        email = contact["email"]
        if email in seen:
            continue
        seen.add(email)

        # Skip automated senders
        if parser.is_automated(contact):
            results.append({"status": "IGNORATO", "email": email, "id": None})
            continue

        existing = hubspot.find_by_email(email)

        if existing:
            updated = hubspot.update_missing_fields(
                existing["id"], contact, existing["properties"]
            )
            results.append({
                "status": "AGGIORNATO" if updated else "IGNORATO",
                "email": email,
                "id": existing["id"],
            })
        else:
            created = hubspot.create(contact)
            results.append({
                "status": "CREATO",
                "email": email,
                "id": created["id"] if created else None,
            })

    _print_report(results)
    return results


def _print_report(results: list[dict]) -> None:
    print("\n" + "=" * 65)
    print("  REPORT SINCRONIZZAZIONE GMAIL → HUBSPOT")
    print("=" * 65)
    print(f"  {'Stato':<12} {'Email':<38} {'HubSpot ID'}")
    print("  " + "-" * 62)

    for r in results:
        if r["status"] == "IGNORATO":
            continue
        print(f"  {r['status']:<12} {r['email']:<38} {r['id'] or 'N/A'}")

    creati = sum(1 for r in results if r["status"] == "CREATO")
    aggiornati = sum(1 for r in results if r["status"] == "AGGIORNATO")
    ignorati = sum(1 for r in results if r["status"] == "IGNORATO")
    print("=" * 65)
    print(f"  Totale unici: {len(results)}  |  Creati: {creati}  |  Aggiornati: {aggiornati}  |  Ignorati: {ignorati}")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\nInterrotto dall'utente.")
        sys.exit(0)
