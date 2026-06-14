#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail threads, extracts sender contacts, and syncs them to HubSpot.
Skips duplicates, updates existing contacts with missing fields.
"""

import json
import os
import re
import sys
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

# pip install google-auth-oauthlib google-api-python-client hubspot-api-client
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest

# ── Configuration ─────────────────────────────────────────────────────────────

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
STATE_FILE = Path(__file__).parent / "state.json"
LOGS_DIR = Path(__file__).parent / "logs"
TOKEN_FILE = Path(__file__).parent / "gmail_token.json"
CREDENTIALS_FILE = Path(__file__).parent / "gmail_credentials.json"

# Senders to ignore (forwarding relays, self, automated notifications)
IGNORED_SENDER_PATTERNS = [
    r"noreply",
    r"no-reply",
    r"notification",
    r"facebookmail\.com",
    r"mailer-daemon",
    r"postmaster",
    r"bounce",
    r"redazione@latestata\.it",   # internal forwarding relay
    r"cristian\.mameli\.editore@gmail\.com",  # self
    r"pubblica\.latestata@gmail\.com",        # own alias
]

CONTACT_SOURCE = "Gmail"
CONTACT_TAG = "Inbound Gmail"

# ── Logging ───────────────────────────────────────────────────────────────────

LOGS_DIR.mkdir(exist_ok=True)
log_file = LOGS_DIR / f"sync_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(log_file), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── State management ──────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_sync": None, "processed_thread_ids": [], "stats": {"created": 0, "updated": 0, "ignored": 0}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ── Gmail auth ────────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


# ── Contact extraction ────────────────────────────────────────────────────────

def should_ignore(email: str) -> bool:
    email_lower = email.lower()
    return any(re.search(p, email_lower) for p in IGNORED_SENDER_PATTERNS)


def parse_sender(sender_header: str) -> tuple[str, str]:
    """Return (name, email) from a From header like 'John Doe <john@example.com>'."""
    m = re.match(r'^"?([^"<]+?)"?\s*<([^>]+)>', sender_header.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip().lower()
    m = re.match(r'^([^\s@]+@[^\s]+)$', sender_header.strip())
    if m:
        return "", m.group(1).lower()
    return "", sender_header.strip().lower()


def extract_forwarded_sender(snippet: str) -> tuple[str, str] | None:
    """Extract original sender from forwarded email snippet (Italian format: Da "Name" email)."""
    # Pattern: Da "Name" email@domain or Da: Name <email>
    m = re.search(r'Da[:\s]+"?([^"<\n]+?)"?\s+([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})', snippet)
    if m:
        name = m.group(1).strip()
        email = m.group(2).strip().lower()
        if not should_ignore(email):
            return name, email
    return None


def domain_to_company(domain: str) -> str:
    """Infer company name from email domain (best effort)."""
    if domain in ("gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "libero.it", "tin.it"):
        return ""
    parts = domain.split(".")
    # Drop TLD and common subdomains
    if parts[0] in ("mail", "email", "press", "ufficio", "info", "noreply"):
        parts = parts[1:]
    name = parts[0] if parts else domain
    return name.replace("-", " ").replace("_", " ").title()


def split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0].capitalize(), parts[1]
    elif len(parts) == 1:
        return parts[0].capitalize(), ""
    return "", ""


def build_contact_payload(name: str, email: str) -> dict:
    domain = email.split("@")[-1] if "@" in email else ""
    company = domain_to_company(domain)
    firstname, lastname = split_name(name) if name else ("", "")

    props = {
        "email": email,
        "hs_lead_source": CONTACT_SOURCE,
    }
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company
    return props


# ── HubSpot operations ────────────────────────────────────────────────────────

def get_hubspot_client() -> hubspot.Client:
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise ValueError("Set HUBSPOT_ACCESS_TOKEN environment variable")
    return hubspot.Client.create(access_token=token)


def find_contact_by_email(hs: hubspot.Client, email: str) -> dict | None:
    f = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[f])
    req = PublicObjectSearchRequest(filter_groups=[fg], properties=["email", "firstname", "lastname", "company", "hs_lead_source"])
    try:
        result = hs.crm.contacts.search_api.do_search(public_object_search_request=req)
        if result.total > 0:
            return result.results[0]
    except ApiException as e:
        log.warning("HubSpot search error for %s: %s", email, e)
    return None


def create_contact(hs: hubspot.Client, props: dict) -> str | None:
    try:
        result = hs.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(properties=props)
        )
        return result.id
    except ApiException as e:
        log.warning("HubSpot create error for %s: %s", props.get("email"), e)
        return None


def update_contact(hs: hubspot.Client, contact_id: str, props: dict, existing: dict) -> bool:
    """Update only fields that are currently empty in HubSpot."""
    from hubspot.crm.contacts import SimplePublicObjectInput
    existing_props = existing.properties
    updates = {k: v for k, v in props.items() if v and not existing_props.get(k)}
    if not updates:
        return False
    try:
        hs.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return True
    except ApiException as e:
        log.warning("HubSpot update error for %s: %s", contact_id, e)
        return False


# ── Main sync loop ────────────────────────────────────────────────────────────

def run_sync():
    log.info("=== Gmail → HubSpot sync started ===")
    state = load_state()
    processed_ids: set = set(state.get("processed_thread_ids", []))
    stats = {"created": 0, "updated": 0, "ignored": 0, "errors": 0}
    results = []

    gmail = get_gmail_service()
    hs = get_hubspot_client()

    # Fetch recent inbox threads
    response = gmail.users().threads().list(
        userId="me", q="in:inbox", maxResults=50
    ).execute()
    threads = response.get("threads", [])

    for thread in threads:
        thread_id = thread["id"]
        if thread_id in processed_ids:
            continue

        try:
            thread_data = gmail.users().threads().get(
                userId="me", threadId=thread_id, format="metadata",
                metadataHeaders=["From", "Subject"]
            ).execute()
        except Exception as e:
            log.warning("Could not fetch thread %s: %s", thread_id, e)
            stats["errors"] += 1
            continue

        contacts_to_process = []

        for msg in thread_data.get("messages", []):
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            sender_raw = headers.get("From", "")
            if not sender_raw:
                continue

            name, email = parse_sender(sender_raw)

            if not email or should_ignore(email):
                # Try to extract forwarded sender from snippet
                snippet = msg.get("snippet", "")
                if snippet:
                    fwd = extract_forwarded_sender(snippet)
                    if fwd:
                        contacts_to_process.append(fwd)
                stats["ignored"] += 1
                continue

            contacts_to_process.append((name, email))

        for name, email in contacts_to_process:
            props = build_contact_payload(name, email)
            existing = find_contact_by_email(hs, email)

            if existing:
                updated = update_contact(hs, existing.id, props, existing)
                status = "Aggiornato" if updated else "Ignorato (nessun campo mancante)"
                contact_id = existing.id
                if updated:
                    stats["updated"] += 1
                else:
                    stats["ignored"] += 1
            else:
                contact_id = create_contact(hs, props)
                status = "Creato" if contact_id else "Errore"
                if contact_id:
                    stats["created"] += 1
                else:
                    stats["errors"] += 1

            log.info("[%s] %s → ID: %s", status, email, contact_id)
            results.append({"status": status, "email": email, "hubspot_id": contact_id})

            time.sleep(0.1)  # gentle rate limit

        processed_ids.add(thread_id)

    state["last_sync"] = datetime.now(timezone.utc).isoformat()
    state["processed_thread_ids"] = list(processed_ids)
    state["stats"]["created"] += stats["created"]
    state["stats"]["updated"] += stats["updated"]
    state["stats"]["ignored"] += stats["ignored"]
    save_state(state)

    log.info("=== Sync complete — Creati: %d | Aggiornati: %d | Ignorati: %d | Errori: %d ===",
             stats["created"], stats["updated"], stats["ignored"], stats["errors"])
    return results, stats


if __name__ == "__main__":
    run_sync()
