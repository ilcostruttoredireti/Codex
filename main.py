#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
-----------------------------
Continuously monitors the Gmail inbox and syncs sender contact data
to HubSpot, creating new contacts or updating existing ones.

Usage:
    python main.py

Environment variables (see .env.example):
    HUBSPOT_ACCESS_TOKEN   – HubSpot Private App token  (required)
    GMAIL_CREDENTIALS_FILE – path to Google OAuth2 JSON (default: credentials.json)
    GMAIL_TOKEN_FILE       – path to cached token       (default: token.json)
    POLL_INTERVAL_SECONDS  – polling cadence in seconds (default: 60)
    LOOKBACK_DAYS          – how far back on first run  (default: 1)
    STATE_FILE             – path to processed-IDs file (default: sync_state.json)
"""

import datetime
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import config
from gmail_hubspot_sync.gmail_client import (
    get_gmail_service,
    fetch_inbox_message_ids,
    get_message_meta,
)
from gmail_hubspot_sync.hubspot_client import (
    get_client as get_hubspot_client,
    find_contact_by_email,
    create_contact,
    update_contact,
    create_email_note,
)
from gmail_hubspot_sync.state import SyncState


# ---------------------------------------------------------------------------
# Local addresses and patterns we never want to sync
# ---------------------------------------------------------------------------
_SKIP_LOCALS = frozenset({
    "noreply", "no-reply", "no_reply",
    "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster",
    "bounce", "bounces",
    "notifications", "notification",
    "alerts", "alert",
    "newsletter", "newsletters",
    "unsubscribe",
})


def _should_skip(email_addr: str) -> bool:
    if not email_addr or "@" not in email_addr:
        return True
    local = email_addr.split("@")[0].lower()
    return local in _SKIP_LOCALS


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class SyncResult:
    status: str          # "created" | "updated" | "ignored" | "error"
    email: str
    contact_id: Optional[str] = None
    detail: str = ""


# ---------------------------------------------------------------------------
# Per-email processing
# ---------------------------------------------------------------------------

def process_sender(sender: dict, hubspot) -> SyncResult:
    email_addr = sender["email"]

    if _should_skip(email_addr):
        return SyncResult("ignored", email_addr, detail="automated/system sender")

    existing = find_contact_by_email(hubspot, email_addr)

    if existing:
        cid = existing["id"]
        props = existing.get("properties") or {}
        changed = update_contact(hubspot, cid, props, sender)
        create_email_note(hubspot, cid, sender)
        status = "updated" if changed else "ignored"
        detail = "fields patched" if changed else "no new data"
        return SyncResult(status, email_addr, cid, detail)

    cid = create_contact(hubspot, sender)
    if cid:
        create_email_note(hubspot, cid, sender)
        return SyncResult("created", email_addr, cid)

    return SyncResult("error", email_addr, detail="HubSpot create failed")


# ---------------------------------------------------------------------------
# One sync cycle
# ---------------------------------------------------------------------------

def run_cycle(gmail, hubspot, state: SyncState, since: int) -> list[SyncResult]:
    message_stubs = fetch_inbox_message_ids(gmail, since_epoch=since)
    results: list[SyncResult] = []

    for stub in message_stubs:
        mid = stub["id"]
        if state.seen(mid):
            continue

        sender = get_message_meta(gmail, mid)
        state.mark(mid)

        if not sender:
            continue

        result = process_sender(sender, hubspot)
        results.append(result)

        icon = {"created": "✓", "updated": "~", "ignored": "·", "error": "✗"}.get(
            result.status, "?"
        )
        cid_str = result.contact_id or "—"
        print(
            f"  {icon} [{result.status.upper():<8}]  "
            f"{result.email:<45}  ID: {cid_str}"
            + (f"  ({result.detail})" if result.detail else "")
        )

    state.save()
    return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if not config.HUBSPOT_ACCESS_TOKEN:
        sys.exit("ERROR: HUBSPOT_ACCESS_TOKEN is not set. Check your .env file.")

    print("=" * 65)
    print("  Gmail → HubSpot Contact Sync")
    print("=" * 65)

    gmail = get_gmail_service(
        config.GMAIL_CREDENTIALS_FILE,
        config.GMAIL_TOKEN_FILE,
        config.GMAIL_SCOPES,
    )
    hubspot = get_hubspot_client(config.HUBSPOT_ACCESS_TOKEN)
    state = SyncState(config.STATE_FILE)

    lookback_dt = datetime.datetime.now() - datetime.timedelta(days=config.LOOKBACK_DAYS)
    since = int(lookback_dt.timestamp())

    print(f"  Polling every {config.POLL_INTERVAL_SECONDS}s  |  "
          f"Lookback: {config.LOOKBACK_DAYS}d  |  "
          f"Press Ctrl+C to stop\n")

    cycle = 0
    while True:
        cycle += 1
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}]  Cycle #{cycle}")

        try:
            results = run_cycle(gmail, hubspot, state, since)

            if results:
                created = sum(1 for r in results if r.status == "created")
                updated = sum(1 for r in results if r.status == "updated")
                ignored = sum(1 for r in results if r.status == "ignored")
                errors  = sum(1 for r in results if r.status == "error")
                print(
                    f"  ─── {len(results)} email(s): "
                    f"{created} created · {updated} updated · "
                    f"{ignored} ignored · {errors} errors"
                )
            else:
                print("  No new emails.")

            # After the first cycle only look at truly new messages
            since = int(time.time())

        except KeyboardInterrupt:
            print("\n\nStopped by user.")
            break
        except Exception as exc:
            print(f"  [error] Cycle failed: {exc}")

        print()
        time.sleep(config.POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
