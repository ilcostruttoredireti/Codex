"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox, extracts sender contacts, creates/updates HubSpot records.
Run on a schedule (cron, GitHub Actions, etc.) to keep HubSpot up to date.
"""

import os
import re
import json
import base64
import logging
import hashlib
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── Config ──────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_TOKEN_FILE = Path(os.getenv("GMAIL_TOKEN_FILE", "token.json"))
GMAIL_CREDENTIALS_FILE = Path(os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"))

HUBSPOT_ACCESS_TOKEN = os.environ["HUBSPOT_ACCESS_TOKEN"]
HUBSPOT_BASE = "https://api.hubapi.com"

# How many days back to scan on each run (use a state file to avoid re-processing)
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "1"))
STATE_FILE = Path(os.getenv("STATE_FILE", ".sync_state.json"))

# Domains / prefixes that are never real contacts
SKIP_DOMAINS = {
    "facebookmail.com", "linkedin.com", "google.com", "googleapis.com",
    "googlemail.com", "notification.circle.so", "moneya.es", "betflag.it",
    "discord.com", "skool.com", "revolut.com", "fastweb.it",
    "serpapi.com", "feedspot.com", "unsplash.com", "freemius.com",
    "shop.tiktok.com", "yoast.com", "thomsonreuters.com",
    "hotel-bb.com", "newsletter.polsia.com",
}
SKIP_LOCAL_PREFIXES = {
    "no-reply", "noreply", "notifications-noreply", "notify-noreply",
    "invitations", "ads-noreply", "sc-noreply", "dont-reply",
    "nobody", "system", "notifications", "bounce",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)


# ── Gmail helpers ────────────────────────────────────────────────────────────

def gmail_service():
    creds = None
    if GMAIL_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_FILE), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(GMAIL_CREDENTIALS_FILE), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        GMAIL_TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_inbox_messages(service, after_timestamp: Optional[int] = None) -> list[dict]:
    """Return a flat list of message metadata from the inbox."""
    query = "in:inbox -from:me"
    if after_timestamp:
        query += f" after:{after_timestamp}"

    messages = []
    page_token = None
    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        result = service.users().messages().list(**kwargs).execute()
        messages.extend(result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return messages


def get_sender(service, msg_id: str) -> Optional[tuple[str, str, str]]:
    """
    Returns (display_name, email_address, raw_from_header) for a message.
    Returns None if the message cannot be fetched or has no From header.
    """
    try:
        msg = service.users().messages().get(
            userId="me", id=msg_id, format="metadata",
            metadataHeaders=["From", "Date"]
        ).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        raw_from = headers.get("From", "")
        display_name, email = parseaddr(raw_from)
        return display_name.strip(), email.strip().lower(), raw_from
    except Exception as exc:
        log.warning("Failed to fetch message %s: %s", msg_id, exc)
        return None


# ── Contact extraction ───────────────────────────────────────────────────────

def should_skip(email: str) -> bool:
    if not email or "@" not in email:
        return True
    local, domain = email.split("@", 1)
    if domain in SKIP_DOMAINS:
        return True
    if any(local.startswith(p) for p in SKIP_LOCAL_PREFIXES):
        return True
    return False


def parse_name(display_name: str, local_part: str) -> tuple[str, str]:
    """Return (firstname, lastname) from display name or email local part."""
    if display_name:
        parts = display_name.split()
        if len(parts) >= 2:
            return parts[0], " ".join(parts[1:])
        return parts[0], ""
    # fall back to email local part (e.g. "john.doe" → John, Doe)
    clean = re.sub(r"[._\-+]", " ", local_part).strip()
    parts = clean.split()
    if len(parts) >= 2:
        return parts[0].capitalize(), " ".join(p.capitalize() for p in parts[1:])
    return clean.capitalize(), ""


def company_from_domain(domain: str) -> str:
    """Best-effort company name from domain (strips TLD and common subdomains)."""
    domain = re.sub(r"^(mail|support|info|marketing|newsletter|noreply|no-reply)\.", "", domain)
    stem = domain.rsplit(".", 1)[0]  # remove TLD
    return stem.replace("-", " ").replace("_", " ").title()


# ── HubSpot helpers ──────────────────────────────────────────────────────────

def hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def hs_find_contact(email: str) -> Optional[dict]:
    """Return the existing HubSpot contact for an email address, or None."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [{"filters": [
            {"propertyName": "email", "operator": "EQ", "value": email}
        ]}],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_source"],
        "limit": 1,
    }
    resp = requests.post(url, headers=hs_headers(), json=payload)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(props: dict) -> dict:
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts"
    resp = requests.post(url, headers=hs_headers(), json={"properties": props})
    resp.raise_for_status()
    return resp.json()


def hs_update_contact(contact_id: str, props: dict) -> dict:
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}"
    resp = requests.patch(url, headers=hs_headers(), json={"properties": props})
    resp.raise_for_status()
    return resp.json()


def hs_add_note(contact_id: str, body: str) -> None:
    """Attach an engagement note to a contact."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/notes"
    note = requests.post(url, headers=hs_headers(), json={
        "properties": {
            "hs_note_body": body,
            "hs_timestamp": datetime.now(timezone.utc).isoformat(),
        }
    })
    note.raise_for_status()
    note_id = note.json()["id"]
    # Associate with contact
    assoc_url = (
        f"{HUBSPOT_BASE}/crm/v3/objects/notes/{note_id}/associations"
        f"/contacts/{contact_id}/note_to_contact"
    )
    requests.put(assoc_url, headers=hs_headers())


# ── State (to avoid re-processing the same messages) ────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_ids": [], "last_run_ts": 0}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Main sync logic ──────────────────────────────────────────────────────────

def sync_contacts() -> list[dict]:
    state = load_state()
    processed_set = set(state.get("processed_ids", []))
    last_run_ts = state.get("last_run_ts", 0)

    service = gmail_service()

    # Fetch messages newer than last run
    messages = fetch_inbox_messages(service, after_timestamp=last_run_ts or None)
    log.info("Found %d messages in inbox query", len(messages))

    # Deduplicate by message id
    new_messages = [m for m in messages if m["id"] not in processed_set]
    log.info("%d new messages to process", len(new_messages))

    # Collect unique sender emails (skip already-seen in this run)
    seen_emails: set[str] = set()
    results = []

    for msg in new_messages:
        sender_info = get_sender(service, msg["id"])
        processed_set.add(msg["id"])

        if not sender_info:
            continue
        display_name, email, _ = sender_info

        if not email or should_skip(email) or email in seen_emails:
            continue
        seen_emails.add(email)

        local, domain = email.split("@", 1)
        firstname, lastname = parse_name(display_name, local)
        company = company_from_domain(domain)

        # Check HubSpot
        existing = hs_find_contact(email)

        if existing:
            contact_id = existing["id"]
            existing_props = existing.get("properties", {})
            updates: dict[str, str] = {}

            # Fill in missing fields only
            if not existing_props.get("firstname") and firstname:
                updates["firstname"] = firstname
            if not existing_props.get("lastname") and lastname:
                updates["lastname"] = lastname
            if not existing_props.get("company") and company:
                updates["company"] = company
            if not existing_props.get("hs_lead_source"):
                updates["hs_lead_source"] = "OTHER_CAMPAIGNS"  # closest standard value

            if updates:
                hs_update_contact(contact_id, updates)
                status = "Aggiornato"
                log.info("AGGIORNATO  %s  (id=%s)  fields=%s", email, contact_id, list(updates))
            else:
                status = "Ignorato"
                log.info("IGNORATO    %s  (id=%s)  già completo", email, contact_id)

        else:
            props = {
                "email": email,
                "firstname": firstname,
                "lastname": lastname,
                "company": company,
                "hs_lead_source": "OTHER_CAMPAIGNS",
            }
            # Remove empty values
            props = {k: v for k, v in props.items() if v}
            created = hs_create_contact(props)
            contact_id = created["id"]
            # Optionally add a note recording the inbound Gmail activity
            hs_add_note(contact_id, f"Contatto acquisito via Gmail inbound. Sorgente: Gmail.")
            status = "Creato"
            log.info("CREATO      %s  (id=%s)", email, contact_id)

        results.append({
            "status": status,
            "email": email,
            "hubspot_id": contact_id,
        })

    # Persist state
    state["processed_ids"] = list(processed_set)
    state["last_run_ts"] = int(datetime.now(timezone.utc).timestamp())
    save_state(state)

    return results


def print_report(results: list[dict]) -> None:
    print("\n" + "=" * 60)
    print(f"{'STATO':<12} {'EMAIL':<40} {'HUBSPOT ID'}")
    print("-" * 60)
    counts = {"Creato": 0, "Aggiornato": 0, "Ignorato": 0}
    for r in results:
        print(f"{r['status']:<12} {r['email']:<40} {r['hubspot_id']}")
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("-" * 60)
    print(f"Totale: {len(results)}  |  Creati: {counts['Creato']}  "
          f"Aggiornati: {counts['Aggiornato']}  Ignorati: {counts['Ignorato']}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    results = sync_contacts()
    print_report(results)
