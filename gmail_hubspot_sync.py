#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox for new emails and syncs sender contacts to HubSpot.
Tracks processed emails via a Gmail label to avoid reprocessing.
"""

import os
import re
import json
import time
import logging
from datetime import datetime, timezone
from email.utils import parseaddr

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
]

GMAIL_PROCESSED_LABEL = "HubSpot-Synced"
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")

HUBSPOT_ACCESS_TOKEN = os.environ["HUBSPOT_ACCESS_TOKEN"]
HUBSPOT_API_BASE = "https://api.hubapi.com"

# Domains to skip (automated senders, no-reply, mailing daemons)
SKIP_DOMAINS = {
    "facebookmail.com",
    "googlemail.com",
    "mailer-daemon.googlemail.com",
    "notifications.google.com",
    "bounce.linkedin.com",
    "amazonses.com",
}

# Local-parts that signal automated/role accounts
SKIP_LOCAL_PARTS = {
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "abuse", "bounce",
    "notification", "notifications", "pageupdates",
    "automailer", "auto-mailer",
}

# ── Gmail helpers ────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if os.path.exists(GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def get_or_create_label(service, label_name: str) -> str:
    results = service.users().labels().list(userId="me").execute()
    for label in results.get("labels", []):
        if label["name"] == label_name:
            return label["id"]
    new_label = service.users().labels().create(
        userId="me", body={"name": label_name}
    ).execute()
    log.info("Created Gmail label '%s' (id=%s)", label_name, new_label["id"])
    return new_label["id"]


def fetch_unprocessed_messages(service, label_id: str, max_results: int = 200):
    """Return inbox messages NOT yet tagged with the processed label."""
    query = f"in:inbox -label:{GMAIL_PROCESSED_LABEL} -from:me"
    messages = []
    page_token = None
    while True:
        resp = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=min(max_results - len(messages), 100),
            pageToken=page_token,
        ).execute()
        messages.extend(resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token or len(messages) >= max_results:
            break
    return messages


def get_message_sender(service, msg_id: str):
    """Return (raw_from, email, display_name) for a message."""
    msg = service.users().messages().get(
        userId="me", id=msg_id, format="metadata",
        metadataHeaders=["From", "Subject"],
    ).execute()
    headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    raw_from = headers.get("From", "")
    display_name, email_addr = parseaddr(raw_from)
    return raw_from, email_addr.lower().strip(), display_name.strip()


def mark_message_processed(service, msg_id: str, label_id: str):
    service.users().messages().modify(
        userId="me", id=msg_id,
        body={"addLabelIds": [label_id]},
    ).execute()


# ── Contact extraction helpers ────────────────────────────────────────────────

def is_automated_sender(email: str) -> bool:
    if not email or "@" not in email:
        return True
    local, domain = email.rsplit("@", 1)
    if domain in SKIP_DOMAINS:
        return True
    if local in SKIP_LOCAL_PARTS:
        return True
    return False


def extract_name_parts(display_name: str, email: str):
    """
    Derive firstname / lastname from display_name or email local-part.
    Returns (firstname, lastname).
    """
    if display_name:
        parts = display_name.strip().split(None, 1)
        if len(parts) == 2:
            return parts[0], parts[1]
        if len(parts) == 1:
            return parts[0], ""
    # Fall back to parsing the local part of the email
    local = email.split("@")[0]
    # Replace separators with spaces
    cleaned = re.sub(r"[._\-+]", " ", local).strip()
    parts = cleaned.split(None, 1)
    if len(parts) == 2:
        return parts[0].capitalize(), parts[1].capitalize()
    return cleaned.capitalize(), ""


def extract_company_from_domain(email: str) -> str:
    """
    Derive a company name from the email domain.
    e.g. info@borgoinjazzfestival.it → "Borgoinjazzfestival"
    gmail/yahoo/libero/hotmail → empty string (personal domains)
    """
    personal_domains = {
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
        "libero.it", "virgilio.it", "alice.it", "tiscali.it",
        "live.com", "icloud.com", "me.com",
    }
    domain = email.split("@")[-1].lower()
    if domain in personal_domains:
        return ""
    # Strip TLD and capitalise each word
    name_part = domain.rsplit(".", 1)[0]  # remove TLD
    words = re.split(r"[.\-_]", name_part)
    return " ".join(w.capitalize() for w in words if w)


# ── HubSpot helpers ───────────────────────────────────────────────────────────

def _hs_headers():
    return {
        "Authorization": f"Bearer {HUBSPOT_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def search_contact_by_email(email: str):
    """Return the HubSpot contact dict or None."""
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [
            {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
        ],
        "properties": ["email", "firstname", "lastname", "company", "phone", "jobtitle"],
        "limit": 1,
    }
    resp = requests.post(url, headers=_hs_headers(), json=payload, timeout=15)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def create_contact(props: dict) -> dict:
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts"
    resp = requests.post(url, headers=_hs_headers(), json={"properties": props}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def update_contact(contact_id: str, props: dict) -> dict:
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/{contact_id}"
    resp = requests.patch(url, headers=_hs_headers(), json={"properties": props}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def upsert_contact(email: str, display_name: str) -> dict:
    """
    Look up contact by email. Create if absent, update missing fields if present.
    Returns {"status": "CREATED"|"UPDATED"|"IGNORED", "email": ..., "hubspot_id": ...}
    """
    firstname, lastname = extract_name_parts(display_name, email)
    company = extract_company_from_domain(email)

    existing = search_contact_by_email(email)

    if existing is None:
        props = {
            "email": email,
            "firstname": firstname,
            "lastname": lastname,
            "leadsource": "Gmail",
        }
        if company:
            props["company"] = company
        contact = create_contact(props)
        return {"status": "CREATED", "email": email, "hubspot_id": contact["id"]}

    # Contact exists — patch only blank fields
    contact_id = existing["id"]
    current = existing.get("properties", {})
    updates = {}

    if not current.get("firstname") and firstname:
        updates["firstname"] = firstname
    if not current.get("lastname") and lastname:
        updates["lastname"] = lastname
    if not current.get("company") and company:
        updates["company"] = company
    if not current.get("leadsource"):
        updates["leadsource"] = "Gmail"

    if updates:
        update_contact(contact_id, updates)
        return {"status": "UPDATED", "email": email, "hubspot_id": contact_id}

    return {"status": "IGNORED", "email": email, "hubspot_id": contact_id}


# ── Main loop ─────────────────────────────────────────────────────────────────

def run_once():
    log.info("Starting Gmail → HubSpot sync run at %s", datetime.now(timezone.utc).isoformat())
    service = get_gmail_service()
    label_id = get_or_create_label(service, GMAIL_PROCESSED_LABEL)

    messages = fetch_unprocessed_messages(service, label_id)
    log.info("Found %d unprocessed inbox messages", len(messages))

    seen_emails: dict[str, str] = {}  # email → display_name
    for m in messages:
        _, email_addr, display_name = get_message_sender(service, m["id"])
        if email_addr and not is_automated_sender(email_addr):
            # Prefer the richer display_name if we've seen this email before
            if email_addr not in seen_emails or (display_name and not seen_emails[email_addr]):
                seen_emails[email_addr] = display_name

    log.info("Unique human senders: %d", len(seen_emails))

    results = []
    for email_addr, display_name in seen_emails.items():
        try:
            result = upsert_contact(email_addr, display_name)
            results.append(result)
            log.info("[%s] %s → HubSpot ID %s", result["status"], email_addr, result["hubspot_id"])
        except Exception as exc:
            log.error("Error processing %s: %s", email_addr, exc)
            results.append({"status": "ERROR", "email": email_addr, "error": str(exc)})

    # Mark all messages as processed
    for m in messages:
        mark_message_processed(service, m["id"], label_id)

    # Summary
    counts = {"CREATED": 0, "UPDATED": 0, "IGNORED": 0, "ERROR": 0}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    log.info(
        "Sync complete — Created: %d | Updated: %d | Ignored: %d | Errors: %d",
        counts["CREATED"], counts["UPDATED"], counts["IGNORED"], counts["ERROR"],
    )
    return results


def main():
    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
    once = os.getenv("RUN_ONCE", "false").lower() == "true"

    if once:
        run_once()
        return

    log.info("Running in continuous mode (poll every %ds). Set RUN_ONCE=true for one-shot.", poll_interval)
    while True:
        try:
            run_once()
        except Exception as exc:
            log.error("Run failed: %s", exc)
        log.info("Sleeping %d seconds until next check…", poll_interval)
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
