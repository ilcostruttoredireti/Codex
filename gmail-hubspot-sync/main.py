#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
==============================
Monitors inbound Gmail, extracts sender contacts and syncs them to HubSpot.

Usage:
    python main.py                  # sync last 24h of unread inbox
    python main.py --days 7         # sync last 7 days
    python main.py --dry-run        # print what would happen, no writes
"""

from __future__ import annotations

import argparse
import sys
from tabulate import tabulate

from contact_parser import parse_sender
from gmail_client import build_service, fetch_new_senders
from hubspot_client import (
    SyncStatus,
    create_contact,
    find_contact_by_email,
    update_contact_if_needed,
)


def sync(days: int = 1, dry_run: bool = False) -> list[dict]:
    service = build_service()
    query = f"in:inbox is:unread newer_than:{days}d -from:me"

    results = []
    seen_emails: set[str] = set()

    for msg in fetch_new_senders(service, query=query):
        contact = parse_sender(msg.sender_raw)
        if contact is None:
            continue
        if contact.email in seen_emails:
            continue
        seen_emails.add(contact.email)

        existing = find_contact_by_email(contact.email)

        if dry_run:
            status = SyncStatus.CREATED if existing is None else SyncStatus.UPDATED
            results.append(
                {
                    "Stato": f"[DRY-RUN] {status.value}",
                    "Email": contact.email,
                    "HubSpot ID": existing["id"] if existing else "—",
                }
            )
            continue

        if existing is None:
            result = create_contact(contact)
        else:
            result = update_contact_if_needed(existing, contact)

        results.append(
            {
                "Stato": result.status.value,
                "Email": result.email,
                "HubSpot ID": result.hubspot_id or "—",
            }
        )
        print(f"  [{result.status.value}] {result.email}  →  {result.hubspot_id or '—'}")

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--days", type=int, default=1, help="Lookback window in days (default: 1)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without writing to HubSpot")
    args = parser.parse_args()

    print(f"\n🔍 Scanning Gmail inbox (last {args.days}d)…")
    if args.dry_run:
        print("   [DRY-RUN mode — no changes will be written]\n")

    results = sync(days=args.days, dry_run=args.dry_run)

    if not results:
        print("No new contacts found.\n")
        return

    print(f"\n{'─' * 70}")
    print(tabulate(results, headers="keys", tablefmt="rounded_outline"))
    print(f"\nTotale: {len(results)} contatti processati")

    created = sum(1 for r in results if SyncStatus.CREATED.value in r["Stato"])
    updated = sum(1 for r in results if SyncStatus.UPDATED.value in r["Stato"])
    ignored = sum(1 for r in results if SyncStatus.IGNORED.value in r["Stato"])
    print(f"  Creati: {created}  |  Aggiornati: {updated}  |  Ignorati: {ignored}\n")


if __name__ == "__main__":
    main()
