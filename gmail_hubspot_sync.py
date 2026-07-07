#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox and syncs sender contacts to HubSpot.

Usage:
    python gmail_hubspot_sync.py              # normal run
    python gmail_hubspot_sync.py --dry-run    # preview without changes
    python gmail_hubspot_sync.py --full-scan  # ignore last-sync timestamp
"""

import argparse
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Patterns that identify automated/no-reply senders to skip
SKIP_PREFIXES = (
    "no-reply@", "noreply@", "dont-reply@", "donotreply@",
    "do-not-reply@", "system@", "notify-noreply@", "updates-noreply@",
    "notifications@", "mailer-daemon@", "postmaster@",
    "bounce@", "bounces@", "auto-reply@", "autoreply@",
    "newsletter@", "news@",
)


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def _gmail_service(credentials_file: str, token_file: str):
    creds = None
    if Path(token_file).exists():
        creds = Credentials.from_authorized_user_file(token_file, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        Path(token_file).write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_inbox_messages(
    service,
    since_timestamp: Optional[int] = None,
    max_results: int = 50,
) -> list[dict]:
    """Return a list of message metadata dicts from the inbox."""
    query = "in:inbox -from:me -in:draft"
    if since_timestamp:
        query += f" after:{since_timestamp}"

    resp = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()
    msgs = resp.get("messages", [])

    result = []
    for m in msgs:
        detail = service.users().messages().get(
            userId="me",
            id=m["id"],
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        result.append(detail)
    return result


def parse_from_header(from_header: str) -> tuple[str, str]:
    """Parse 'Name <email>' → (name, email). Falls back to ('', raw)."""
    m = re.match(r'^(.+?)\s*<([^>]+)>', from_header.strip())
    if m:
        return m.group(1).strip().strip('"'), m.group(2).strip().lower()
    return "", from_header.strip().lower()


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

class HubSpot:
    BASE = "https://api.hubapi.com"

    def __init__(self, token: str):
        self._h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _get(self, path: str, **kwargs):
        r = requests.get(f"{self.BASE}{path}", headers=self._h, **kwargs)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, payload: dict):
        r = requests.post(f"{self.BASE}{path}", headers=self._h, json=payload)
        r.raise_for_status()
        return r.json()

    def _patch(self, path: str, payload: dict):
        r = requests.patch(f"{self.BASE}{path}", headers=self._h, json=payload)
        r.raise_for_status()
        return r.json()

    def _put(self, path: str):
        r = requests.put(f"{self.BASE}{path}", headers=self._h)
        r.raise_for_status()

    def find_contact(self, email: str) -> Optional[dict]:
        data = self._post("/crm/v3/objects/contacts/search", {
            "filterGroups": [{"filters": [
                {"propertyName": "email", "operator": "EQ", "value": email}
            ]}],
            "properties": ["email", "firstname", "lastname", "company"],
        })
        results = data.get("results", [])
        return results[0] if results else None

    def create_contact(self, props: dict) -> dict:
        return self._post("/crm/v3/objects/contacts", {"properties": props})

    def update_contact(self, contact_id: str, props: dict) -> dict:
        return self._patch(f"/crm/v3/objects/contacts/{contact_id}", {"properties": props})

    def create_note(self, contact_id: str, body: str):
        """Create a timeline note and associate it with a contact."""
        ts_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        note = self._post("/crm/v3/objects/notes", {
            "properties": {"hs_note_body": body, "hs_timestamp": ts_ms}
        })
        note_id = note["id"]
        self._put(
            f"/crm/v3/objects/notes/{note_id}"
            f"/associations/contacts/{contact_id}/note_to_contact"
        )


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def is_automated(email: str) -> bool:
    return email.lower().startswith(SKIP_PREFIXES)


def company_from_domain(email: str) -> str:
    domain = email.split("@")[-1]
    name = domain.split(".")[0]
    return name.replace("-", " ").replace("_", " ").title()


def split_name(full: str) -> tuple[str, str]:
    parts = full.strip().split(None, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0] if parts else "", "")


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state(path: str) -> dict:
    if Path(path).exists():
        return json.loads(Path(path).read_text())
    return {"last_sync_ts": None, "seen_message_ids": []}


def save_state(state: dict, path: str):
    # Keep only the most recent 1000 message IDs
    state["seen_message_ids"] = state.get("seen_message_ids", [])[-1000:]
    Path(path).write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------

def sync(
    gmail_service,
    hs: HubSpot,
    state: dict,
    dry_run: bool = False,
    max_results: int = 50,
) -> list[dict]:
    since = state.get("last_sync_ts")
    seen_ids = set(state.get("seen_message_ids", []))

    log.info("Fetching Gmail inbox messages (since=%s) …", since)
    messages = fetch_inbox_messages(gmail_service, since, max_results)
    log.info("  → %d messages retrieved", len(messages))

    state["last_sync_ts"] = int(datetime.now(timezone.utc).timestamp())

    results = []
    deduped_senders: set[str] = set()

    for msg in messages:
        msg_id = msg["id"]
        if msg_id in seen_ids:
            continue
        seen_ids.add(msg_id)

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        from_val = headers.get("From", "")
        if not from_val:
            continue

        sender_name, sender_email = parse_from_header(from_val)

        # De-duplicate senders within this batch
        if sender_email in deduped_senders:
            continue
        deduped_senders.add(sender_email)

        # Skip automated addresses
        if is_automated(sender_email):
            log.info("  SKIP  %s (automated)", sender_email)
            results.append({"status": "Ignorato", "email": sender_email,
                            "hubspot_id": None, "note": "automated sender"})
            continue

        firstname, lastname = split_name(sender_name) if sender_name else ("", "")
        company = company_from_domain(sender_email)
        subject = headers.get("Subject", "N/A")
        date_str = headers.get("Date", "N/A")

        existing = hs.find_contact(sender_email)

        if existing:
            cid = existing["id"]
            ep = existing.get("properties", {})
            updates: dict[str, str] = {}
            if not ep.get("firstname") and firstname:
                updates["firstname"] = firstname
            if not ep.get("lastname") and lastname:
                updates["lastname"] = lastname
            if not ep.get("company"):
                updates["company"] = company
            if not ep.get("hs_analytics_source_data_1"):
                updates["hs_analytics_source_data_1"] = "Gmail"

            if updates:
                if not dry_run:
                    hs.update_contact(cid, updates)
                    note_body = (
                        f"Email inbound ricevuta via Gmail.\n"
                        f"Mittente: {from_val}\n"
                        f"Oggetto: {subject}\n"
                        f"Data: {date_str}\n"
                        f"Tag: Inbound Gmail"
                    )
                    hs.create_note(cid, note_body)
                log.info("  UPDATE %s (id=%s) fields=%s", sender_email, cid, list(updates))
                results.append({"status": "Aggiornato", "email": sender_email,
                                "hubspot_id": cid, "fields": list(updates)})
            else:
                log.info("  OK     %s (id=%s) already complete", sender_email, cid)
                results.append({"status": "Ignorato", "email": sender_email,
                                "hubspot_id": cid, "note": "already up to date"})
        else:
            props: dict[str, str] = {"email": sender_email}
            if firstname:
                props["firstname"] = firstname
            if lastname:
                props["lastname"] = lastname
            props["company"] = company

            if not dry_run:
                created = hs.create_contact(props)
                cid = created["id"]
                note_body = (
                    f"Email inbound ricevuta via Gmail.\n"
                    f"Mittente: {from_val}\n"
                    f"Oggetto: {subject}\n"
                    f"Data: {date_str}\n"
                    f"Tag: Inbound Gmail"
                )
                hs.create_note(cid, note_body)
                log.info("  CREATE %s (id=%s)", sender_email, cid)
            else:
                cid = "DRY_RUN"
                log.info("  CREATE (dry-run) %s", sender_email)
            results.append({"status": "Creato", "email": sender_email, "hubspot_id": cid})

    state["seen_message_ids"] = list(seen_ids)
    return results


def print_report(results: list[dict]):
    created = [r for r in results if r["status"] == "Creato"]
    updated = [r for r in results if r["status"] == "Aggiornato"]
    ignored = [r for r in results if r["status"] == "Ignorato"]

    print(f"\n{'='*65}")
    print("  RIEPILOGO SYNC  Gmail → HubSpot")
    print(f"{'='*65}")
    print(f"  Creati       : {len(created)}")
    print(f"  Aggiornati   : {len(updated)}")
    print(f"  Ignorati     : {len(ignored)}")
    print(f"{'='*65}")
    for r in results:
        hid = r.get("hubspot_id") or "—"
        extra = ""
        if "fields" in r:
            extra = f"  [{', '.join(r['fields'])}]"
        elif "note" in r:
            extra = f"  ({r['note']})"
        print(f"  [{r['status']:10}]  {r['email']:<45}  HubSpot: {hid}{extra}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Sync Gmail senders → HubSpot contacts")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without making any changes")
    parser.add_argument("--full-scan", action="store_true",
                        help="Ignore last-sync timestamp, re-process all inbox messages")
    parser.add_argument("--max-results", type=int, default=50,
                        help="Max inbox messages to process per run (default: 50)")
    parser.add_argument("--state-file", default="sync_state.json",
                        help="Path to state file (default: sync_state.json)")
    parser.add_argument("--credentials", default="credentials.json",
                        help="Path to Gmail OAuth credentials (default: credentials.json)")
    parser.add_argument("--token", default="token.json",
                        help="Path to Gmail OAuth token (default: token.json)")
    args = parser.parse_args()

    hubspot_token = os.environ.get("HUBSPOT_API_TOKEN")
    if not hubspot_token:
        raise SystemExit("ERROR: HUBSPOT_API_TOKEN environment variable not set")

    gmail_svc = _gmail_service(
        os.environ.get("GMAIL_CREDENTIALS_FILE", args.credentials),
        os.environ.get("GMAIL_TOKEN_FILE", args.token),
    )
    hs = HubSpot(hubspot_token)
    state = load_state(args.state_file)
    if args.full_scan:
        state["last_sync_ts"] = None

    try:
        results = sync(gmail_svc, hs, state,
                       dry_run=args.dry_run,
                       max_results=args.max_results)
    finally:
        if not args.dry_run:
            save_state(state, args.state_file)

    print_report(results)


if __name__ == "__main__":
    main()
