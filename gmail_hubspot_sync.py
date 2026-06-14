#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox, extracts senders, and syncs them to HubSpot.
Avoids duplicates using email as unique key; updates missing fields on existing contacts.
"""

import os
import re
import logging
import sys
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Optional

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
HUBSPOT_API_BASE = "https://api.hubapi.com"
HUBSPOT_TOKEN = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")

# Free/generic email providers — don't infer company from domain
FREE_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "outlook.it", "libero.it", "alice.it", "virgilio.it",
    "tiscali.it", "fastwebnet.it", "tin.it", "inwind.it",
}

# Senders to skip (automated, internal, bounce)
SKIP_PATTERNS = {
    "noreply", "no-reply", "mailer-daemon", "postmaster", "donotreply",
    "notification", "notifications", "facebookmail.com", "googlemail.com",
    "pubblica.latestata@gmail.com", "redazione@latestata.it",
    "cristian.mameli.editore@gmail.com",
}

# ── HubSpot ───────────────────────────────────────────────────────────────────

def _hs_headers() -> dict:
    return {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}


def hs_find_contact(email: str) -> Optional[dict]:
    """Search HubSpot for a contact by email; returns record or None."""
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [{"filters": [
            {"propertyName": "email", "operator": "EQ", "value": email}
        ]}],
        "properties": ["email", "firstname", "lastname", "company"],
        "limit": 1,
    }
    r = requests.post(url, json=payload, headers=_hs_headers(), timeout=15)
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(props: dict) -> dict:
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts"
    r = requests.post(url, json={"properties": props}, headers=_hs_headers(), timeout=15)
    r.raise_for_status()
    return r.json()


def hs_update_contact(contact_id: str, props: dict) -> None:
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/{contact_id}"
    r = requests.patch(url, json={"properties": props}, headers=_hs_headers(), timeout=15)
    r.raise_for_status()


def hs_add_note(contact_id: str, body: str) -> None:
    """Attach a note activity to a HubSpot contact."""
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/notes"
    ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    payload = {
        "properties": {"hs_timestamp": str(ts_ms), "hs_note_body": body},
        "associations": [{
            "to": {"id": contact_id},
            "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
        }],
    }
    r = requests.post(url, json=payload, headers=_hs_headers(), timeout=15)
    r.raise_for_status()


# ── Contact data extraction ───────────────────────────────────────────────────

def parse_name(display_name: str) -> tuple[str, str]:
    """Split a display name into (firstname, lastname)."""
    parts = display_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def domain_to_company(domain: str) -> Optional[str]:
    """Derive a company name from a domain, or None for free providers."""
    if domain in FREE_DOMAINS:
        return None
    base = domain.split(".")[0]
    return base.replace("-", " ").replace("_", " ").title()


def build_contact_props(name: str, email: str) -> dict:
    """Return a dict of HubSpot contact properties derived from name + email."""
    firstname, lastname = parse_name(name)
    domain = email.split("@")[-1].lower() if "@" in email else ""
    company = domain_to_company(domain)

    props: dict = {"email": email.lower()}
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company
    return props


def should_skip(email: str) -> bool:
    email_lower = email.lower()
    for pattern in SKIP_PATTERNS:
        if pattern in email_lower:
            return True
    return False


# ── Gmail ─────────────────────────────────────────────────────────────────────

def get_gmail_service(token_path: str = "token.json", creds_path: str = "credentials.json"):
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
            with open(token_path, "w") as f:
                f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_inbox_threads(service, query: str, max_results: int = 200) -> list[str]:
    """Return a list of thread IDs matching the Gmail query."""
    thread_ids: list[str] = []
    page_token = None
    while len(thread_ids) < max_results:
        kw: dict = {"userId": "me", "q": query, "maxResults": min(50, max_results - len(thread_ids))}
        if page_token:
            kw["pageToken"] = page_token
        resp = service.users().threads().list(**kw).execute()
        for t in resp.get("threads", []):
            thread_ids.append(t["id"])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return thread_ids


def get_thread_sender(service, thread_id: str) -> tuple[str, str]:
    """Return (display_name, email) for the first message of a thread."""
    thread = service.users().threads().get(
        userId="me", id=thread_id, format="metadata",
        metadataHeaders=["From"],
    ).execute()
    messages = thread.get("messages", [])
    if not messages:
        return "", ""
    headers = messages[0].get("payload", {}).get("headers", [])
    from_hdr = next((h["value"] for h in headers if h["name"] == "From"), "")
    name, addr = parseaddr(from_hdr)
    return name or "", addr or ""


# ── Sync logic ────────────────────────────────────────────────────────────────

SyncResult = dict  # {status, email, hubspot_id, reason?}


def sync(gmail_query: str = "in:inbox -from:me newer_than:1d") -> list[SyncResult]:
    """
    Main sync entry point.
    Returns list of dicts: {status: Creato|Aggiornato|Ignorato, email, hubspot_id}
    """
    service = get_gmail_service()
    thread_ids = fetch_inbox_threads(service, query=gmail_query)
    log.info("Thread trovati: %d", len(thread_ids))

    seen: set[str] = set()
    results: list[SyncResult] = []

    for tid in thread_ids:
        name, email = get_thread_sender(service, tid)
        if not email:
            continue
        email = email.lower()
        if email in seen:
            continue
        seen.add(email)

        if should_skip(email):
            results.append({"status": "Ignorato", "email": email, "hubspot_id": None, "reason": "automated/internal"})
            continue

        new_props = build_contact_props(name, email)
        existing = hs_find_contact(email)

        if existing:
            contact_id = existing["id"]
            existing_props = existing.get("properties", {})
            updates = {
                k: v for k, v in new_props.items()
                if k != "email" and not existing_props.get(k) and v
            }
            if updates:
                hs_update_contact(contact_id, updates)
                results.append({"status": "Aggiornato", "email": email, "hubspot_id": contact_id})
                log.info("Aggiornato %s → %s", email, contact_id)
            else:
                results.append({"status": "Ignorato", "email": email, "hubspot_id": contact_id, "reason": "già aggiornato"})
        else:
            created = hs_create_contact(new_props)
            contact_id = created["id"]
            hs_add_note(
                contact_id,
                f"Contatto acquisito da Gmail (Inbound)\n"
                f"Fonte: Gmail\nTag: Inbound Gmail\nData: {datetime.now(timezone.utc).isoformat()}",
            )
            results.append({"status": "Creato", "email": email, "hubspot_id": contact_id})
            log.info("Creato %s → %s", email, contact_id)

    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    query = sys.argv[1] if len(sys.argv) > 1 else "in:inbox -from:me newer_than:1d"
    log.info("Query Gmail: %s", query)
    results = sync(query)

    print("\n─── Gmail → HubSpot Sync ───────────────────────────────────────────────────")
    print(f"{'Stato':<12} {'Email':<48} {'ID HubSpot'}")
    print("─" * 80)
    for r in results:
        print(f"{r['status']:<12} {r['email']:<48} {r.get('hubspot_id') or '—'}")

    creati = sum(1 for r in results if r["status"] == "Creato")
    aggiornati = sum(1 for r in results if r["status"] == "Aggiornato")
    ignorati = sum(1 for r in results if r["status"] == "Ignorato")
    print(f"\nTotale mittenti unici: {len(results)}")
    print(f"  ✓ Creati:     {creati}")
    print(f"  ↑ Aggiornati: {aggiornati}")
    print(f"  — Ignorati:   {ignorati}")


if __name__ == "__main__":
    main()
