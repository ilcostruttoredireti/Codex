#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
------------------------------
Monitors incoming Gmail and syncs sender contacts to HubSpot.
Avoids duplicates using email as the unique key.

Setup
-----
1. Enable Gmail API in Google Cloud Console and download credentials.json
2. Create a HubSpot Private App with contacts read/write scope
3. Set HUBSPOT_ACCESS_TOKEN environment variable
4. pip install -r requirements.txt
5. python sync.py

On first run Gmail will open a browser for OAuth consent.
Subsequent runs use the stored token (token.json).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
]
CREDENTIALS_FILE = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.environ.get("GMAIL_TOKEN_FILE", "token.json")
HUBSPOT_TOKEN = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")

STATE_FILE = Path(__file__).parent / ".sync_state.json"
LOG_FILE = Path(__file__).parent / "sync_log.jsonl"

# Free/personal email providers — domain is NOT used as company name
FREE_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it",
    "hotmail.com", "hotmail.it", "outlook.com", "outlook.it",
    "live.com", "live.it", "icloud.com", "me.com",
    "aol.com", "protonmail.com", "pm.me",
    "libero.it", "alice.it", "virgilio.it", "tiscali.it",
    "tin.it", "fastwebnet.it",
}

# Patterns that indicate automated/system senders — skip them
SKIP_PATTERNS = (
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "notifications@",
    "support@", "newsletter@", "info@", "hello@",
)


# ── State helpers ──────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())
        data["processed_ids"] = set(data.get("processed_ids", []))
        return data
    return {"processed_ids": set(), "last_run": None}


def save_state(state: dict) -> None:
    out = {**state, "processed_ids": sorted(state["processed_ids"])}
    STATE_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False))


def append_log(entry: dict) -> None:
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ── Sender parsing ─────────────────────────────────────────────────────────────

def parse_sender(from_header: str) -> dict | None:
    """Return structured sender info or None if the sender should be skipped."""
    display_name, email = parseaddr(from_header or "")
    if not email or "@" not in email:
        return None

    email = email.lower().strip()

    if any(p in email for p in SKIP_PATTERNS):
        return None

    domain = email.split("@")[1]
    parts = display_name.strip().split(" ", 1) if display_name.strip() else []

    company = ""
    if domain not in FREE_DOMAINS:
        company = domain.split(".")[0].replace("-", " ").title()

    return {
        "email": email,
        "firstname": parts[0] if parts else "",
        "lastname": parts[1] if len(parts) > 1 else "",
        "domain": domain,
        "company": company,
    }


# ── Gmail ─────────────────────────────────────────────────────────────────────

def build_gmail():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if Path(TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        Path(TOKEN_FILE).write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def list_inbox_messages(svc, max_results: int = 100) -> list[dict]:
    result = svc.users().messages().list(
        userId="me",
        q="in:inbox -from:me",
        maxResults=max_results,
    ).execute()
    return result.get("messages", [])


def get_from_header(svc, msg_id: str) -> str | None:
    msg = svc.users().messages().get(
        userId="me", id=msg_id, format="metadata",
        metadataHeaders=["From"],
    ).execute()
    headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
    return headers.get("From")


# ── HubSpot ───────────────────────────────────────────────────────────────────

def build_hubspot():
    from hubspot import HubSpot
    if not HUBSPOT_TOKEN:
        raise EnvironmentError("HUBSPOT_ACCESS_TOKEN is not set")
    return HubSpot(access_token=HUBSPOT_TOKEN)


def hs_find_contact(client, email: str) -> dict | None:
    resp = client.crm.contacts.search_api.do_search(
        public_object_search_request={
            "filterGroups": [{
                "filters": [{
                    "propertyName": "email",
                    "operator": "EQ",
                    "value": email,
                }]
            }],
            "properties": ["email", "firstname", "lastname", "company"],
            "limit": 1,
        }
    )
    return resp.results[0].to_dict() if resp.results else None


def hs_create_contact(client, sender: dict) -> str | None:
    from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
    try:
        obj = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties={
                    "email": sender["email"],
                    "firstname": sender.get("firstname", ""),
                    "lastname": sender.get("lastname", ""),
                    "company": sender.get("company", ""),
                }
            )
        )
        _hs_add_inbound_note(client, obj.id, sender["email"])
        return obj.id
    except ApiException as exc:
        log.error("HubSpot create failed for %s: %s", sender["email"], exc)
        return None


def hs_update_contact(client, contact_id: str, existing_props: dict, sender: dict) -> bool:
    """Fill in missing fields only — never overwrite existing data."""
    from hubspot.crm.contacts import SimplePublicObjectInput, ApiException
    updates: dict[str, str] = {}
    for field in ("firstname", "lastname", "company"):
        if sender.get(field) and not existing_props.get(field):
            updates[field] = sender[field]
    if not updates:
        return False
    try:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return True
    except ApiException as exc:
        log.error("HubSpot update failed for id=%s: %s", contact_id, exc)
        return False


def _hs_add_inbound_note(client, contact_id: str, email: str) -> None:
    """Add a timeline note tagging the contact as Inbound Gmail."""
    from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate, ApiException
    from hubspot.crm.associations.v4 import PublicAssociation, AssociationSpec
    import time
    try:
        note = client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties={
                    "hs_note_body": (
                        f"📧 Inbound Gmail — Contatto {email} identificato "
                        f"tramite email in arrivo (Tag: Inbound Gmail)."
                    ),
                    "hs_timestamp": str(int(time.time() * 1000)),
                }
            )
        )
        client.crm.associations.v4.basic_api.create(
            object_type="notes",
            object_id=note.id,
            to_object_type="contacts",
            to_object_id=contact_id,
            association_spec=[AssociationSpec(association_category="HUBSPOT_DEFINED", association_type_id=202)],
        )
    except Exception as exc:
        log.warning("Could not add inbound note for %s: %s", contact_id, exc)


# ── Orchestration ──────────────────────────────────────────────────────────────

def run_sync(max_messages: int = 100) -> list[dict]:
    state = load_state()
    processed: set = state["processed_ids"]
    results: list[dict] = []

    try:
        gmail = build_gmail()
        hubspot = build_hubspot()
    except Exception as exc:
        log.critical("Init failed: %s", exc)
        sys.exit(1)

    messages = list_inbox_messages(gmail, max_results=max_messages)
    log.info("Inbox messages fetched: %d", len(messages))

    new_count = sum(1 for m in messages if m["id"] not in processed)
    log.info("New (unprocessed): %d", new_count)

    for msg in messages:
        msg_id = msg["id"]
        if msg_id in processed:
            continue

        from_header = get_from_header(gmail, msg_id)
        processed.add(msg_id)

        if not from_header:
            continue

        sender = parse_sender(from_header)
        if not sender:
            log.debug("Skipping automated/invalid sender: %s", from_header)
            continue

        email = sender["email"]
        existing = hs_find_contact(hubspot, email)

        if existing:
            existing_props = existing.get("properties", {})
            updated = hs_update_contact(hubspot, existing["id"], existing_props, sender)
            status = "Aggiornato" if updated else "Ignorato"
            contact_id = existing["id"]
        else:
            contact_id = hs_create_contact(hubspot, sender)
            status = "Creato" if contact_id else "Errore"

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message_id": msg_id,
            "stato": status,
            "email_contatto": email,
            "id_contatto_hubspot": contact_id,
        }
        results.append(entry)
        append_log(entry)
        log.info("[%s] %s  →  HubSpot ID: %s", status, email, contact_id or "—")

    state["processed_ids"] = processed
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    return results


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    results = run_sync()

    print("\n" + "=" * 70)
    print(" RIEPILOGO SINCRONIZZAZIONE Gmail → HubSpot")
    print("=" * 70)
    print(f"{'Stato':<12} {'Email Contatto':<42} {'ID HubSpot'}")
    print("-" * 70)
    for r in results:
        print(f"{r['stato']:<12} {r['email_contatto']:<42} {r['id_contatto_hubspot'] or 'N/A'}")

    totals = {"Creato": 0, "Aggiornato": 0, "Ignorato": 0, "Errore": 0}
    for r in results:
        totals[r["stato"]] = totals.get(r["stato"], 0) + 1

    print("-" * 70)
    print(
        f"Totale: {len(results)} elaborati  |  "
        f"Creati: {totals['Creato']}  |  "
        f"Aggiornati: {totals['Aggiornato']}  |  "
        f"Ignorati: {totals['Ignorato']}  |  "
        f"Errori: {totals['Errore']}"
    )
    print("=" * 70)


if __name__ == "__main__":
    main()
