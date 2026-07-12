#!/usr/bin/env python3
"""
Gmail → HubSpot contact sync.

Monitors Gmail inbox, extracts senders, and upserts them into HubSpot
using the sender email as a unique key.

Usage:
    python sync.py                # single pass
    python sync.py --watch 60     # poll every 60 seconds
    python sync.py --days 7       # look back 7 days (default: 1)
"""

import argparse
import sys
import time
from dataclasses import dataclass
from typing import Literal

from contact_parser import deduplicate, parse_sender
from gmail_client import iter_inbox_senders
from hubspot_client import create_contact, create_note, find_contact_by_email, update_contact

SyncStatus = Literal["created", "updated", "ignored", "error"]


@dataclass
class SyncResult:
    email: str
    status: SyncStatus
    contact_id: str
    note: str = ""


def _run_once(days_back: int) -> list[SyncResult]:
    query = f"in:inbox -from:me newer_than:{days_back}d"
    raw_senders = list(iter_inbox_senders(max_results=500, query=query))

    contacts = [c for h in raw_senders if (c := parse_sender(h)) is not None]
    contacts = deduplicate(contacts)

    results: list[SyncResult] = []

    for contact in contacts:
        try:
            existing = find_contact_by_email(contact.email)

            if existing is None:
                new_id = create_contact(contact)
                create_note(new_id)
                results.append(SyncResult(
                    email=contact.email,
                    status="created",
                    contact_id=new_id,
                    note=f"{contact.first_name} {contact.last_name}".strip(),
                ))
            else:
                updated = update_contact(existing.id, contact, existing)
                if updated:
                    create_note(existing.id)
                status: SyncStatus = "updated" if updated else "ignored"
                results.append(SyncResult(
                    email=contact.email,
                    status=status,
                    contact_id=existing.id,
                    note="fields up to date" if not updated else "fields patched",
                ))
        except Exception as exc:  # noqa: BLE001
            results.append(SyncResult(
                email=contact.email,
                status="error",
                contact_id="",
                note=str(exc),
            ))

    return results


def _print_results(results: list[SyncResult]) -> None:
    col_w = (32, 10, 20, 30)
    header = ("Email", "Status", "HubSpot ID", "Note")
    sep = "  ".join("-" * w for w in col_w)
    row_fmt = "  ".join(f"{{:<{w}}}" for w in col_w)

    print(row_fmt.format(*header))
    print(sep)
    for r in results:
        print(row_fmt.format(r.email[:col_w[0]], r.status, r.contact_id[:col_w[2]], r.note[:col_w[3]]))

    counts = {"created": 0, "updated": 0, "ignored": 0, "error": 0}
    for r in results:
        counts[r.status] += 1
    print()
    print(f"  Created: {counts['created']}  Updated: {counts['updated']}  "
          f"Ignored: {counts['ignored']}  Errors: {counts['error']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Gmail senders to HubSpot contacts")
    parser.add_argument("--days", type=int, default=1, help="How many days back to scan (default: 1)")
    parser.add_argument("--watch", type=int, default=0, metavar="SECONDS",
                        help="Poll continuously every N seconds (0 = run once)")
    args = parser.parse_args()

    if args.watch <= 0:
        results = _run_once(args.days)
        _print_results(results)
        sys.exit(0 if not any(r.status == "error" for r in results) else 1)

    print(f"Watching Gmail inbox — polling every {args.watch}s. Press Ctrl+C to stop.\n")
    while True:
        try:
            print(f"--- Sync pass ({args.days}d window) ---")
            results = _run_once(args.days)
            _print_results(results)
            print()
            time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\nStopped.")
            break


if __name__ == "__main__":
    main()
