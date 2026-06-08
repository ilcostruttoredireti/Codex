#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail emails and syncs senders as HubSpot contacts.

Usage:
    python gmail_hubspot_sync.py            # continuous daemon
    python gmail_hubspot_sync.py --once     # single pass and exit
    python gmail_hubspot_sync.py --reset    # clear state and re-process all inbox

Required env vars:
    HUBSPOT_TOKEN   HubSpot Private App access token
    GMAIL_CREDENTIALS   Path to OAuth2 credentials.json (default: credentials.json)

Optional env vars:
    GMAIL_TOKEN     Path for stored OAuth2 token (default: token.json)
    STATE_FILE      Path for processed-message state (default: sync_state.json)
    POLL_INTERVAL   Seconds between inbox polls (default: 60)
    GMAIL_LABEL     Gmail label applied to processed emails (default: Inbound Gmail)
    LOG_LEVEL       Logging verbosity (default: INFO)
"""

import os
import re
import sys
import json
import time
import logging
from pathlib import Path
from datetime import datetime, timezone

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ─── Configuration ────────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

CREDENTIALS_FILE = Path(os.environ.get("GMAIL_CREDENTIALS", "credentials.json"))
TOKEN_FILE = Path(os.environ.get("GMAIL_TOKEN", "token.json"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "sync_state.json"))
HUBSPOT_TOKEN = os.environ.get("HUBSPOT_TOKEN", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
GMAIL_LABEL = os.environ.get("GMAIL_LABEL", "Inbound Gmail")

HUBSPOT_API = "https://api.hubapi.com"

# Automated sender patterns to skip (noreply, mailers, etc.)
_SKIP_LOCALS = {"noreply", "no-reply", "donotreply", "mailer-daemon",
                "postmaster", "bounce", "auto-reply", "notifications",
                "support", "newsletter"}

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ─── Gmail helpers ────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise SystemExit(
                    f"Missing {CREDENTIALS_FILE}. Download OAuth2 credentials from "
                    "https://console.cloud.google.com/apis/credentials and save as credentials.json"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def get_or_create_label(service, name):
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in labels:
        if lbl["name"] == name:
            return lbl["id"]
    body = {
        "name": name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    }
    return service.users().labels().create(userId="me", body=body).execute()["id"]


def list_unprocessed_messages(service, state):
    processed = set(state.get("processed", []))
    messages = []
    page_token = None
    # Personal / primary inbox only — skip promotions and social
    query = "in:inbox -in:draft -category:promotions -category:social -category:updates"
    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 100}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute()
        for m in resp.get("messages", []):
            if m["id"] not in processed:
                messages.append(m["id"])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return messages


def get_message_metadata(service, msg_id):
    msg = service.users().messages().get(
        userId="me",
        messageId=msg_id,
        format="metadata",
        metadataHeaders=["From", "Subject", "Date"],
    ).execute()
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return (
        headers.get("From", ""),
        headers.get("Subject", ""),
        headers.get("Date", ""),
    )


def label_message(service, msg_id, label_id):
    try:
        service.users().messages().modify(
            userId="me",
            id=msg_id,
            body={"addLabelIds": [label_id]},
        ).execute()
    except HttpError as e:
        log.debug(f"Could not label message {msg_id}: {e}")


# ─── Parsing ──────────────────────────────────────────────────────────────────

def parse_from_header(from_header):
    """Return (name, email) from 'Display Name <addr@example.com>' or bare address."""
    m = re.match(r'^"?([^"<]*?)"?\s*<([^>]+)>\s*$', from_header.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip().lower()
    bare = from_header.strip().lower()
    return "", bare


def should_skip(email, name):
    if not email or "@" not in email:
        return True, "invalid email"
    local = email.split("@")[0].lower()
    if any(kw in local for kw in _SKIP_LOCALS):
        return True, "automated sender"
    if any(kw in name.lower() for kw in _SKIP_LOCALS):
        return True, "automated sender name"
    return False, ""


def domain_to_company(domain):
    """Best-effort company name from email domain (strips TLD and subdomains)."""
    if not domain:
        return ""
    parts = domain.split(".")
    # 'mail.acme.co.uk' → 'acme', 'stripe.com' → 'stripe'
    if len(parts) >= 2:
        return parts[-2].capitalize()
    return domain.capitalize()


def split_name(full_name):
    parts = full_name.strip().split(" ", 1) if full_name else []
    first = parts[0] if parts else ""
    last = parts[1] if len(parts) > 1 else ""
    return first, last


# ─── HubSpot helpers ──────────────────────────────────────────────────────────

def _hs_headers():
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }


def hs_find_contact(email):
    resp = requests.post(
        f"{HUBSPOT_API}/crm/v3/objects/contacts/search",
        headers=_hs_headers(),
        json={
            "filterGroups": [
                {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
            ],
            "properties": ["email", "firstname", "lastname", "company", "lead_source"],
            "limit": 1,
        },
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(props):
    resp = requests.post(
        f"{HUBSPOT_API}/crm/v3/objects/contacts",
        headers=_hs_headers(),
        json={"properties": props},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def hs_update_contact(contact_id, props):
    resp = requests.patch(
        f"{HUBSPOT_API}/crm/v3/objects/contacts/{contact_id}",
        headers=_hs_headers(),
        json={"properties": props},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def hs_create_note(contact_id, body_text):
    """Attach a timeline note (engagement) to the contact."""
    ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    resp = requests.post(
        f"{HUBSPOT_API}/crm/v3/objects/notes",
        headers=_hs_headers(),
        json={
            "properties": {
                "hs_timestamp": str(ts_ms),
                "hs_note_body": body_text,
            },
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 202,  # note → contact
                        }
                    ],
                }
            ],
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# ─── Core sync logic ──────────────────────────────────────────────────────────

def sync_sender(email, name, subject, date_str):
    """
    Creates or updates a HubSpot contact for a given email sender.
    Returns (action, contact_id) where action is 'Creato' | 'Aggiornato' | 'Ignorato'.
    """
    domain = email.split("@")[1] if "@" in email else ""
    firstname, lastname = split_name(name)
    company = domain_to_company(domain)

    existing = hs_find_contact(email)

    if existing:
        contact_id = existing["id"]
        ep = existing.get("properties", {})
        updates = {}
        if firstname and not ep.get("firstname"):
            updates["firstname"] = firstname
        if lastname and not ep.get("lastname"):
            updates["lastname"] = lastname
        if company and not ep.get("company"):
            updates["company"] = company
        if updates:
            hs_update_contact(contact_id, updates)
            action = "Aggiornato"
        else:
            action = "Ignorato"  # already complete, no changes needed
    else:
        props = {k: v for k, v in {
            "email": email,
            "firstname": firstname,
            "lastname": lastname,
            "company": company,
            "lead_source": "Gmail",
        }.items() if v}
        created = hs_create_contact(props)
        contact_id = created["id"]
        action = "Creato"

    # Timeline note on every inbound email
    try:
        note = (
            f"📧 Email inbound ricevuta via Gmail\n"
            f"Oggetto: {subject or '(nessun oggetto)'}\n"
            f"Data: {date_str}\n"
            f"Tag: Inbound Gmail"
        )
        hs_create_note(contact_id, note)
    except Exception as exc:
        log.debug(f"Note creation skipped for {email}: {exc}")

    return action, contact_id


# ─── State management ─────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed": []}


def save_state(state):
    # Cap at 10 000 IDs to prevent unbounded growth
    state["processed"] = state["processed"][-10_000:]
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ─── One processing pass ──────────────────────────────────────────────────────

def run_pass(service, label_id, state):
    msg_ids = list_unprocessed_messages(service, state)
    if not msg_ids:
        return []

    results = []
    for msg_id in msg_ids:
        result = {"message_id": msg_id, "status": "Errore", "email": "", "hubspot_id": None}
        try:
            from_header, subject, date_str = get_message_metadata(service, msg_id)
            name, email = parse_from_header(from_header)
            result["email"] = email

            skip, reason = should_skip(email, name)
            if skip:
                result["status"] = "Ignorato"
                result["reason"] = reason
                state["processed"].append(msg_id)
                results.append(result)
                continue

            action, contact_id = sync_sender(email, name, subject, date_str)
            result["status"] = action
            result["hubspot_id"] = contact_id

            if label_id:
                label_message(service, msg_id, label_id)

        except requests.HTTPError as exc:
            result["reason"] = str(exc)
            log.error(f"HubSpot error for {result['email']}: {exc}")
        except Exception as exc:
            result["reason"] = str(exc)
            log.error(f"Unexpected error for message {msg_id}: {exc}", exc_info=True)
        finally:
            state["processed"].append(msg_id)

        results.append(result)

    save_state(state)
    return results


def print_results(results):
    icons = {"Creato": "✅", "Aggiornato": "🔄", "Ignorato": "⏭ ", "Errore": "❌"}
    for r in results:
        icon = icons.get(r["status"], "·")
        hs_id = r["hubspot_id"] or "—"
        reason = f" ({r.get('reason', '')})" if r.get("reason") else ""
        print(f"  {icon} [{r['status']}] {r['email'] or '?'}  →  HubSpot ID: {hs_id}{reason}")


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    if not HUBSPOT_TOKEN:
        raise SystemExit("Error: HUBSPOT_TOKEN environment variable is not set.")

    once = "--once" in sys.argv
    reset = "--reset" in sys.argv

    log.info("Authenticating with Gmail...")
    service = get_gmail_service()

    log.info(f'Ensuring Gmail label "{GMAIL_LABEL}" exists...')
    label_id = get_or_create_label(service, GMAIL_LABEL)

    state = {"processed": []} if reset else load_state()
    if reset:
        log.info("State reset — will re-process all inbox messages.")

    if once:
        log.info("Running single sync pass...")
        results = run_pass(service, label_id, state)
        print_results(results)
        log.info(f"Done. {len(results)} email(s) processed.")
        return

    log.info(f"Starting continuous sync (poll every {POLL_INTERVAL}s). Ctrl+C to stop.")
    while True:
        try:
            results = run_pass(service, label_id, state)
            if results:
                print_results(results)
                log.info(f"Pass complete: {len(results)} email(s) processed.")
            else:
                log.debug("No new emails.")
        except KeyboardInterrupt:
            log.info("Stopped.")
            break
        except Exception as exc:
            log.error(f"Pass failed: {exc}", exc_info=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
