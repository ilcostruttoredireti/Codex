#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox, extracts sender contacts and syncs them to HubSpot CRM.
Handles both direct emails and forwarded emails (e.g., from redazione@latestata.it).
"""

import os
import re
import json
import time
import base64
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from email.utils import parseaddr

import requests
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

load_dotenv()

# ─── Configuration ────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
GMAIL_POLL_INTERVAL = int(os.getenv("GMAIL_POLL_INTERVAL", "60"))  # seconds

HUBSPOT_TOKEN = os.getenv("HUBSPOT_TOKEN", "")
HUBSPOT_BASE_URL = "https://api.hubapi.com"

STATE_FILE = os.getenv("STATE_FILE", "state.json")

# Email addresses that forward on behalf of others (extract original sender from body)
FORWARDER_ADDRESSES = set(
    a.strip().lower()
    for a in os.getenv("FORWARDER_ADDRESSES", "redazione@latestata.it").split(",")
    if a.strip()
)

# Free email providers – don't use these as company names
FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "live.com", "protonmail.com", "libero.it", "alice.it", "tin.it",
    "virgilio.it", "tiscali.it", "fastwebnet.it", "email.it",
}

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ─── State management ─────────────────────────────────────────────────────────

def load_state() -> dict:
    p = Path(STATE_FILE)
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return {"processed_messages": [], "last_run": None,
            "stats": {"total": 0, "created": 0, "updated": 0, "ignored": 0}}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ─── Gmail authentication ─────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if Path(GMAIL_TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


# ─── Email parsing ────────────────────────────────────────────────────────────

def extract_forwarded_sender(body: str) -> tuple[str, str]:
    """
    Parse original sender from Italian forwarded email body.
    Looks for:  Da "Name" email@domain.com
    or English: From "Name" email@domain.com
    Returns (name, email) or ("", "").
    """
    patterns = [
        # Italian/French forwarded header
        r'Da\s+"([^"]+)"\s+([\w._%+\-]+@[\w.\-]+\.[a-zA-Z]{2,})',
        r'Da:\s*"?([^"<\n]+?)"?\s*<?([\w._%+\-]+@[\w.\-]+\.[a-zA-Z]{2,})>?',
        # English
        r'From\s+"([^"]+)"\s+([\w._%+\-]+@[\w.\-]+\.[a-zA-Z]{2,})',
        r'From:\s*"?([^"<\n]+?)"?\s*<?([\w._%+\-]+@[\w.\-]+\.[a-zA-Z]{2,})>?',
        # Fallback: bare email in forwarded block
        r'(?:Da|From)[:\s]+([\w._%+\-]+@[\w.\-]+\.[a-zA-Z]{2,})',
    ]
    for pat in patterns:
        m = re.search(pat, body, re.IGNORECASE | re.MULTILINE)
        if m:
            groups = m.groups()
            if len(groups) == 2:
                name, email = groups[0].strip(), groups[1].strip().lower()
            else:
                name, email = "", groups[0].strip().lower()
            if email:
                return name, email
    return "", ""


def split_name(full_name: str) -> tuple[str, str]:
    """
    Split a full name into (first, last).
    Handles 'First Last', 'First Middle Last', organization names (single word or with dashes).
    """
    full_name = full_name.strip()
    if not full_name:
        return "", ""
    # Strip trailing role indicators separated by dash or pipe
    for sep in (" - ", " | ", " – "):
        if sep in full_name:
            full_name = full_name.split(sep)[0].strip()
    parts = full_name.split()
    if len(parts) == 1:
        return parts[0], ""
    # Last word is last name
    return " ".join(parts[:-1]), parts[-1]


def domain_to_company(domain: str) -> str:
    """Convert a domain like 'mannuccionline.com' → 'Mannuccionline'."""
    if domain.lower() in FREE_EMAIL_DOMAINS:
        return ""
    name = domain.split(".")[0]
    return name.replace("-", " ").replace("_", " ").title()


def parse_sender(raw_sender: str, body: str = "") -> dict:
    """
    Return a dict with keys: email, name, first_name, last_name, company, domain.
    Handles direct senders and forwarded emails.
    """
    name, email = parseaddr(raw_sender)
    email = email.strip().lower()

    # If this is a known forwarder, dig original sender from body
    if email in FORWARDER_ADDRESSES and body:
        fwd_name, fwd_email = extract_forwarded_sender(body)
        if fwd_email:
            name, email = fwd_name, fwd_email

    if not email:
        return {}

    domain = email.split("@")[-1] if "@" in email else ""
    first, last = split_name(name)
    company = domain_to_company(domain)

    return {
        "email": email,
        "name": name,
        "first_name": first,
        "last_name": last,
        "company": company,
        "domain": domain,
    }


def get_message_body(payload: dict) -> str:
    """Extract plain-text body from a Gmail message payload."""
    def _decode(data: str) -> str:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data", "")

    if body_data:
        return _decode(body_data)

    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return _decode(data)
        if part.get("mimeType", "").startswith("multipart/"):
            sub = get_message_body(part)
            if sub:
                return sub
    return ""


# ─── HubSpot helpers ──────────────────────────────────────────────────────────

def _hs_headers() -> dict:
    return {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}


def hubspot_find_contact(email: str) -> Optional[dict]:
    """Search HubSpot for a contact by email. Returns the contact dict or None."""
    url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [{
            "filters": [{"propertyName": "email", "operator": "EQ", "value": email}]
        }],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_status",
                       "leadsource", "hs_tag_ids"],
        "limit": 1,
    }
    resp = requests.post(url, headers=_hs_headers(), json=payload, timeout=15)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def hubspot_create_contact(sender: dict) -> dict:
    """Create a new HubSpot contact. Returns the created object."""
    url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts"
    props = {
        "email": sender["email"],
        "leadsource": "Gmail",
    }
    if sender.get("first_name"):
        props["firstname"] = sender["first_name"]
    if sender.get("last_name"):
        props["lastname"] = sender["last_name"]
    if sender.get("company"):
        props["company"] = sender["company"]
    resp = requests.post(url, headers=_hs_headers(), json={"properties": props}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def hubspot_update_contact(contact_id: str, sender: dict, existing_props: dict) -> dict:
    """Fill in missing fields on an existing HubSpot contact."""
    url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts/{contact_id}"
    props = {}
    if not existing_props.get("firstname") and sender.get("first_name"):
        props["firstname"] = sender["first_name"]
    if not existing_props.get("lastname") and sender.get("last_name"):
        props["lastname"] = sender["last_name"]
    if not existing_props.get("company") and sender.get("company"):
        props["company"] = sender["company"]
    if not existing_props.get("leadsource"):
        props["leadsource"] = "Gmail"
    if not props:
        return {}  # nothing to update
    resp = requests.patch(url, headers=_hs_headers(), json={"properties": props}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def hubspot_add_note(contact_id: str, subject: str, email_date: str) -> None:
    """Create a NOTE activity on the contact for the received email."""
    url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/notes"
    props = {
        "hs_note_body": f"Email inbound ricevuta: {subject}",
        "hs_timestamp": email_date,
    }
    resp = requests.post(url, headers=_hs_headers(), json={"properties": props}, timeout=15)
    if not resp.ok:
        log.warning("Could not create note: %s", resp.text)
        return
    note_id = resp.json()["id"]
    # Associate note → contact
    assoc_url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/notes/{note_id}/associations/contacts/{contact_id}/note_to_contact"
    requests.put(assoc_url, headers=_hs_headers(), timeout=15)


# ─── Core sync logic ──────────────────────────────────────────────────────────

def process_message(msg: dict, state: dict) -> dict:
    """
    Process a single Gmail message dict.
    Returns a result dict: {status, email, hubspot_id, message_id}.
    """
    msg_id = msg["id"]

    headers = {h["name"].lower(): h["value"]
               for h in msg.get("payload", {}).get("headers", [])}
    raw_sender = headers.get("from", "")
    subject = headers.get("subject", "(no subject)")
    date_str = headers.get("date", "")

    body = get_message_body(msg.get("payload", {}))
    sender = parse_sender(raw_sender, body)

    if not sender or not sender.get("email"):
        return {"status": "IGNORATO", "email": raw_sender, "hubspot_id": None, "message_id": msg_id,
                "note": "email mittente non ricavabile"}

    email = sender["email"]
    log.info("Processing: %s (subject: %.60s)", email, subject)

    try:
        existing = hubspot_find_contact(email)
    except requests.HTTPError as exc:
        log.error("HubSpot search failed for %s: %s", email, exc)
        return {"status": "ERRORE", "email": email, "hubspot_id": None, "message_id": msg_id,
                "note": str(exc)}

    if existing:
        contact_id = existing["id"]
        existing_props = existing.get("properties", {})
        try:
            updated = hubspot_update_contact(contact_id, sender, existing_props)
        except requests.HTTPError as exc:
            log.error("HubSpot update failed for %s: %s", email, exc)
            return {"status": "ERRORE", "email": email, "hubspot_id": contact_id,
                    "message_id": msg_id, "note": str(exc)}
        try:
            hubspot_add_note(contact_id, subject, date_str)
        except Exception as exc:
            log.warning("Note creation failed: %s", exc)
        status = "AGGIORNATO" if updated else "INVARIATO"
        log.info("  → %s | ID: %s", status, contact_id)
        return {"status": status, "email": email, "hubspot_id": contact_id,
                "message_id": msg_id}
    else:
        try:
            created = hubspot_create_contact(sender)
        except requests.HTTPError as exc:
            log.error("HubSpot create failed for %s: %s", email, exc)
            return {"status": "ERRORE", "email": email, "hubspot_id": None,
                    "message_id": msg_id, "note": str(exc)}
        contact_id = created["id"]
        try:
            hubspot_add_note(contact_id, subject, date_str)
        except Exception as exc:
            log.warning("Note creation failed: %s", exc)
        log.info("  → CREATO | ID: %s", contact_id)
        return {"status": "CREATO", "email": email, "hubspot_id": contact_id,
                "message_id": msg_id}


def fetch_new_messages(service, state: dict) -> list:
    """Fetch INBOX messages not yet processed."""
    processed = set(state.get("processed_messages", []))
    messages = []
    page_token = None

    while True:
        kwargs = {"userId": "me", "labelIds": ["INBOX"], "maxResults": 50}
        if page_token:
            kwargs["pageToken"] = page_token
        result = service.users().messages().list(**kwargs).execute()
        for m in result.get("messages", []):
            if m["id"] not in processed:
                messages.append(m["id"])
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return messages


def run_sync(service, state: dict) -> list[dict]:
    """Run one sync cycle. Returns list of result dicts."""
    new_ids = fetch_new_messages(service, state)
    if not new_ids:
        log.info("No new messages to process.")
        return []

    log.info("Found %d new message(s) to process.", len(new_ids))
    results = []

    for msg_id in new_ids:
        try:
            msg = service.users().messages().get(
                userId="me", id=msg_id, format="full"
            ).execute()
        except Exception as exc:
            log.error("Could not fetch message %s: %s", msg_id, exc)
            continue

        result = process_message(msg, state)
        results.append(result)

        # Mark as processed regardless of outcome (avoid infinite retry on bad data)
        state["processed_messages"].append(msg_id)
        stats = state["stats"]
        stats["total"] += 1
        s = result["status"]
        if s == "CREATO":
            stats["created"] += 1
        elif s in ("AGGIORNATO", "INVARIATO"):
            stats["updated"] += 1
        else:
            stats["ignored"] += 1

        save_state(state)
        time.sleep(0.3)  # gentle rate limiting

    return results


def print_results(results: list[dict]) -> None:
    print("\n" + "─" * 72)
    print(f"{'STATO':<12} {'EMAIL':<38} {'ID HUBSPOT'}")
    print("─" * 72)
    for r in results:
        print(f"{r['status']:<12} {r['email']:<38} {r['hubspot_id'] or '—'}")
        if r.get("note"):
            print(f"{'':12} ↳ {r['note']}")
    print("─" * 72)
    created = sum(1 for r in results if r["status"] == "CREATO")
    updated = sum(1 for r in results if r["status"] == "AGGIORNATO")
    invariant = sum(1 for r in results if r["status"] == "INVARIATO")
    ignored = sum(1 for r in results if r["status"] in ("IGNORATO", "ERRORE"))
    print(f"Totale: {len(results)} | Creati: {created} | Aggiornati: {updated} | "
          f"Invariati: {invariant} | Ignorati/Errori: {ignored}")
    print("─" * 72 + "\n")


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument("--once", action="store_true",
                        help="Run one sync cycle and exit (default: continuous polling)")
    parser.add_argument("--interval", type=int, default=GMAIL_POLL_INTERVAL,
                        help=f"Poll interval in seconds (default: {GMAIL_POLL_INTERVAL})")
    parser.add_argument("--reset", action="store_true",
                        help="Reset processed-message state before running")
    args = parser.parse_args()

    if not HUBSPOT_TOKEN:
        log.error("HUBSPOT_TOKEN not set. Add it to .env or environment.")
        raise SystemExit(1)

    state = load_state()
    if args.reset:
        state["processed_messages"] = []
        save_state(state)
        log.info("State reset.")

    service = get_gmail_service()
    log.info("Gmail service ready. Starting sync (interval=%ds, once=%s).",
             args.interval, args.once)

    try:
        while True:
            state["last_run"] = datetime.now(timezone.utc).isoformat()
            results = run_sync(service, state)
            if results:
                print_results(results)
            if args.once:
                break
            log.info("Sleeping %d seconds until next check…", args.interval)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log.info("Interrupted. Goodbye.")


if __name__ == "__main__":
    main()
