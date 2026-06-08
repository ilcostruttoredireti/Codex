#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox and syncs sender contacts to HubSpot CRM.

Usage:
    python sync.py               # single run
    python sync.py --continuous  # polling loop (default interval: 60s)
    python sync.py --interval 120 --continuous
"""

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# ── Gmail ─────────────────────────────────────────────────────────────────────
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = Path(os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json"))
TOKEN_FILE = Path("token.json")

# ── State ─────────────────────────────────────────────────────────────────────
STATE_FILE = Path("state.json")

# ── Filtering ─────────────────────────────────────────────────────────────────
SKIP_DOMAINS = frozenset({
    # generic freemail (personal, not business contacts)
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com",
    # automated infrastructure
    "noreply.com", "no-reply.com", "mailer-daemon.org",
    # Italian PEC / certified-mail providers (delivery notifications only)
    "legalmail.it", "pec.it", "postacert.it", "pecmail.it",
    # Google infrastructure
    "accounts.google.com", "google.com",
})
SKIP_PREFIXES = (
    "noreply", "no-reply", "donotreply", "mailer-daemon",
    "postmaster", "bounce", "notification", "alert", "security",
    "posta-certificata", "delivery-status",
)


# ─────────────────────────────────────────────────────────────────────────────
# State helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_ids": []}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ─────────────────────────────────────────────────────────────────────────────
# Gmail helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_gmail_service():
    import google.auth.transport.requests
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(google.auth.transport.requests.Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_inbox_messages(gmail, state: dict, max_results: int = 50) -> list[dict]:
    """Return messages in inbox not yet processed."""
    result = gmail.users().messages().list(
        userId="me", q="in:inbox", maxResults=max_results
    ).execute()
    processed = set(state.get("processed_ids", []))
    return [m for m in result.get("messages", []) if m["id"] not in processed]


def parse_message(gmail, msg_id: str) -> Optional[dict]:
    """Fetch metadata headers and return parsed sender dict or None."""
    msg = gmail.users().messages().get(
        userId="me", id=msg_id, format="metadata",
        metadataHeaders=["From", "Subject"]
    ).execute()
    headers = {h["name"]: h["value"]
               for h in msg.get("payload", {}).get("headers", [])}
    from_header = headers.get("From", "").strip()
    if not from_header:
        return None
    name, email, domain = parse_sender(from_header)
    if not email or should_skip(email, domain):
        return None
    return {
        "id": msg_id,
        "email": email,
        "name": name,
        "domain": domain,
        "subject": headers.get("Subject", ""),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Text / parsing helpers
# ─────────────────────────────────────────────────────────────────────────────

def parse_sender(from_header: str) -> tuple[str, str, str]:
    match = re.match(r'"?([^"<]+?)"?\s*<([^>]+)>', from_header)
    if match:
        name = match.group(1).strip()
        email = match.group(2).strip().lower()
    else:
        name = ""
        email = from_header.strip().lower()
    domain = email.split("@")[-1] if "@" in email else ""
    return name, email, domain


def split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split(None, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0] if parts else "", "")


def should_skip(email: str, domain: str) -> bool:
    local = email.split("@")[0]
    return domain in SKIP_DOMAINS or any(local.startswith(p) for p in SKIP_PREFIXES)


def company_from_domain(domain: str) -> str:
    return domain.split(".")[0].capitalize() if domain else ""


# ─────────────────────────────────────────────────────────────────────────────
# HubSpot helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_hubspot_client():
    import hubspot
    token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise ValueError("HUBSPOT_ACCESS_TOKEN not set in environment")
    return hubspot.Client.create(access_token=token)


def find_contact(hs, email: str) -> Optional[object]:
    from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest
    req = PublicObjectSearchRequest(
        filter_groups=[FilterGroup(filters=[
            Filter(property_name="email", operator="EQ", value=email)
        ])],
        properties=["email", "firstname", "lastname", "company"],
        limit=1,
    )
    resp = hs.crm.contacts.search_api.do_search(public_object_search_request=req)
    return resp.results[0] if resp.total > 0 else None


def create_contact(hs, email: str, firstname: str, lastname: str, company: str) -> str:
    from hubspot.crm.contacts import SimplePublicObjectInputForCreate
    props = {"email": email, "hs_lead_source": "Gmail"}
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company
    result = hs.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=SimplePublicObjectInputForCreate(properties=props)
    )
    return result.id


def update_contact(hs, contact_id: str, firstname: str, lastname: str,
                   company: str, existing) -> bool:
    from hubspot.crm.contacts import SimplePublicObjectInput
    ep = existing.properties
    updates = {}
    if firstname and not ep.get("firstname"):
        updates["firstname"] = firstname
    if lastname and not ep.get("lastname"):
        updates["lastname"] = lastname
    if company and not ep.get("company"):
        updates["company"] = company
    if not updates:
        return False
    hs.crm.contacts.basic_api.update(
        contact_id=contact_id,
        simple_public_object_input=SimplePublicObjectInput(properties=updates),
    )
    return True


def log_email_activity(hs, contact_id: str, subject: str, from_email: str):
    """Optionally log an inbound email activity on the contact timeline."""
    try:
        from hubspot.crm.objects.models import (
            SimplePublicObjectInputForCreate as ObjInput,
        )
        ts = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        props = {
            "hs_activity_type": "EMAIL",
            "hs_email_direction": "INCOMING_EMAIL",
            "hs_email_subject": subject or "(no subject)",
            "hs_email_from_email": from_email,
            "hs_timestamp": ts,
        }
        obj = ObjInput(
            properties=props,
            associations=[{
                "to": {"id": contact_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED",
                           "associationTypeId": 198}],
            }],
        )
        hs.crm.objects.emails.basic_api.create(
            simple_public_object_input_for_create=obj
        )
    except Exception:
        pass  # activity logging is optional; never block the main flow


# ─────────────────────────────────────────────────────────────────────────────
# Core sync
# ─────────────────────────────────────────────────────────────────────────────

def sync_once(gmail, hs, state: dict) -> list[dict]:
    results = []
    new_msgs = fetch_inbox_messages(gmail, state)
    processed = set(state.get("processed_ids", []))

    if not new_msgs:
        print("[INFO] Nessuna nuova email da processare.")
        return results

    print(f"[INFO] Trovate {len(new_msgs)} nuove email...")

    for msg_ref in new_msgs:
        msg_id = msg_ref["id"]
        try:
            data = parse_message(gmail, msg_id)
        except Exception as exc:
            print(f"[WARN] Impossibile leggere {msg_id}: {exc}")
            processed.add(msg_id)
            continue

        if data is None:
            processed.add(msg_id)
            continue

        email = data["email"]
        firstname, lastname = split_name(data["name"])
        company = company_from_domain(data["domain"])
        subject = data["subject"]

        existing = find_contact(hs, email)
        if existing:
            updated = update_contact(hs, existing.id, firstname, lastname, company, existing)
            if updated:
                log_email_activity(hs, existing.id, subject, email)
                stato = "Aggiornato"
            else:
                stato = "Ignorato"
            contact_id = existing.id
        else:
            try:
                contact_id = create_contact(hs, email, firstname, lastname, company)
                log_email_activity(hs, contact_id, subject, email)
                stato = "Creato"
            except Exception as exc:
                print(f"[ERROR] HubSpot create fallito per {email}: {exc}")
                stato = "Errore"
                contact_id = None

        row = {"stato": stato, "email_contatto": email, "id_hubspot": contact_id}
        results.append(row)
        processed.add(msg_id)
        print(f"  [{stato:10s}] {email:45s} ID: {contact_id}")

    state["processed_ids"] = list(processed)
    save_state(state)
    return results


def print_summary(results: list[dict]):
    if not results:
        return
    print("\n" + "═" * 70)
    print(f"{'STATO':<12} {'EMAIL CONTATTO':<42} {'ID HUBSPOT'}")
    print("─" * 70)
    for r in results:
        print(f"{r['stato']:<12} {r['email_contatto']:<42} {r['id_hubspot'] or '—'}")
    print("═" * 70)

    counts = {}
    for r in results:
        counts[r["stato"]] = counts.get(r["stato"], 0) + 1
    print("  " + " | ".join(f"{k}: {v}" for k, v in counts.items()))


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument("--continuous", action="store_true",
                        help="Polling continuo invece di singola esecuzione")
    parser.add_argument("--interval", type=int, default=60,
                        help="Intervallo polling in secondi (default: 60)")
    args = parser.parse_args()

    gmail = get_gmail_service()
    hs = get_hubspot_client()
    state = load_state()

    print("╔══════════════════════════════╗")
    print("║  Gmail → HubSpot Sync  v1.0  ║")
    print("╚══════════════════════════════╝")

    while True:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{ts}] Avvio sincronizzazione...")
        results = sync_once(gmail, hs, state)
        print_summary(results)

        if not args.continuous:
            break
        print(f"\n[INFO] Prossimo controllo tra {args.interval}s — Ctrl+C per uscire")
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n[INFO] Sync interrotto dall'utente.")
            break


if __name__ == "__main__":
    main()
