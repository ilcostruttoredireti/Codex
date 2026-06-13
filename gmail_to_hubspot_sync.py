#!/usr/bin/env python3
"""
gmail_to_hubspot_sync.py

Monitors Gmail inbox, extracts sender contacts, and syncs them to HubSpot.
For each new email: creates a contact if not found, or updates missing fields
on an existing one. Uses email address as the deduplication key.

Usage:
    python gmail_to_hubspot_sync.py [--days N]  # default: 7 days back
    HUBSPOT_ACCESS_TOKEN=xxx python gmail_to_hubspot_sync.py

Required env vars:
    HUBSPOT_ACCESS_TOKEN  – HubSpot private-app token (contacts read/write)

Google OAuth:
    Place credentials.json (Desktop OAuth client) in the working directory.
    On first run, a browser window opens to authorize. Token is cached in token.json.
"""

import os
import re
import sys
import json
import logging
import argparse
from datetime import datetime
from email.utils import parseaddr
from typing import Optional, Tuple

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

HUBSPOT_API_BASE = "https://api.hubapi.com"
CONTACT_SOURCE = "Gmail"

# Automated / system sender signals — skip these
SKIP_DOMAINS = {
    "facebookmail.com",
    "notifications.google.com",
    "accounts.google.com",
    "bounce.google.com",
}
SKIP_LOCAL_PREFIXES = (
    "noreply",
    "no-reply",
    "donotreply",
    "mailer-daemon",
    "notification",
    "notifications",
    "bounce",
    "automated",
    "postmaster",
    "analytics-noreply",
)
# Generic free-mail domains — company name cannot be inferred from these
GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "libero.it", "alice.it", "tiscali.it", "virgilio.it", "live.it",
    "icloud.com", "me.com",
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def should_skip(email: str) -> bool:
    """Return True for automated / system / own-account senders."""
    email = email.lower().strip()
    local, _, domain = email.partition("@")
    if domain in SKIP_DOMAINS:
        return True
    if any(local.startswith(p) for p in SKIP_LOCAL_PREFIXES):
        return True
    return False


def split_name(display_name: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (firstname, lastname) from a display-name string."""
    if not display_name:
        return None, None
    parts = display_name.strip().split(None, 1)
    return (parts[0] or None), (parts[1] if len(parts) > 1 else None)


def company_from_domain(domain: str) -> Optional[str]:
    """Infer a company name from the email domain when possible."""
    if domain in GENERIC_DOMAINS:
        return None
    base = domain.split(".")[0]
    return base.replace("-", " ").title() if base else None


# ── Gmail (via REST) ─────────────────────────────────────────────────────────

def _gmail_headers() -> dict:
    """Return Authorization header for Gmail API using a stored OAuth token."""
    token_file = os.environ.get("GMAIL_TOKEN_FILE", "token.json")
    if not os.path.exists(token_file):
        raise FileNotFoundError(
            f"Gmail OAuth token not found at '{token_file}'. "
            "Run the OAuth flow once to generate it (see google-auth-oauthlib)."
        )
    with open(token_file) as f:
        token_data = json.load(f)

    # Refresh if expired
    if token_data.get("expiry") and datetime.fromisoformat(
        token_data["expiry"].rstrip("Z")
    ) < datetime.utcnow():
        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": token_data["client_id"],
                "client_secret": token_data["client_secret"],
                "refresh_token": token_data["refresh_token"],
                "grant_type": "refresh_token",
            },
        )
        resp.raise_for_status()
        new_token = resp.json()
        token_data["token"] = new_token["access_token"]
        with open(token_file, "w") as f:
            json.dump(token_data, f)

    return {"Authorization": f"Bearer {token_data['token']}"}


def fetch_inbox_senders(days_back: int = 7, max_results: int = 200) -> list[dict]:
    """
    Return a deduplicated list of sender dicts from the Gmail inbox.
    Each dict: {email, display_name, subject, date, message_id}
    """
    query = (
        f"in:inbox newer_than:{days_back}d "
        "-from:noreply -from:no-reply -from:mailer-daemon"
    )
    base_url = "https://gmail.googleapis.com/gmail/v1/users/me"
    headers = _gmail_headers()

    # 1. List message IDs
    resp = requests.get(
        f"{base_url}/messages",
        headers=headers,
        params={"q": query, "maxResults": max_results},
    )
    resp.raise_for_status()
    message_refs = resp.json().get("messages", [])

    seen: dict[str, dict] = {}
    for ref in message_refs:
        try:
            msg = requests.get(
                f"{base_url}/messages/{ref['id']}",
                headers=headers,
                params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]},
            ).json()
            h = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            display_name, email = parseaddr(h.get("From", ""))
            email = email.lower().strip()
            if email and email not in seen:
                seen[email] = {
                    "email": email,
                    "display_name": display_name,
                    "subject": h.get("Subject", "(no subject)"),
                    "date": h.get("Date", ""),
                    "message_id": ref["id"],
                }
        except Exception as exc:
            logger.warning("Could not fetch message %s: %s", ref["id"], exc)

    return list(seen.values())


# ── HubSpot ──────────────────────────────────────────────────────────────────

def _hs_headers() -> dict:
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise EnvironmentError("HUBSPOT_ACCESS_TOKEN is not set.")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def hs_find_contact(email: str) -> Optional[dict]:
    """Search HubSpot for a contact by email. Returns the contact dict or None."""
    resp = requests.post(
        f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/search",
        headers=_hs_headers(),
        json={
            "filterGroups": [
                {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
            ],
            "properties": ["email", "firstname", "lastname", "company", "hs_lead_source"],
        },
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(
    email: str,
    firstname: Optional[str],
    lastname: Optional[str],
    company: Optional[str],
) -> dict:
    """Create a new HubSpot contact and return the created object."""
    props: dict = {"email": email, "hs_lead_source": CONTACT_SOURCE}
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company

    resp = requests.post(
        f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts",
        headers=_hs_headers(),
        json={"properties": props},
    )
    resp.raise_for_status()
    return resp.json()


def hs_update_contact(contact_id: str, updates: dict) -> dict:
    """Patch an existing HubSpot contact with the given property updates."""
    resp = requests.patch(
        f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/{contact_id}",
        headers=_hs_headers(),
        json={"properties": updates},
    )
    resp.raise_for_status()
    return resp.json()


def hs_log_note(contact_id: str, subject: str, date: str) -> None:
    """Attach a timeline note (email received) to a HubSpot contact."""
    body = f"Email ricevuta via Gmail\nOggetto: {subject}\nData: {date}"
    try:
        resp = requests.post(
            f"{HUBSPOT_API_BASE}/crm/v3/objects/notes",
            headers=_hs_headers(),
            json={
                "properties": {
                    "hs_note_body": body,
                    "hs_timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                "associations": [
                    {
                        "to": {"id": contact_id},
                        "types": [
                            {
                                "associationCategory": "HUBSPOT_DEFINED",
                                "associationTypeId": 202,
                            }
                        ],
                    }
                ],
            },
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("Could not log note for contact %s: %s", contact_id, exc)


# ── Core logic ────────────────────────────────────────────────────────────────

def process_sender(item: dict) -> dict:
    """
    Resolve one sender against HubSpot.
    Returns: {status: CREATO|AGGIORNATO|IGNORATO, email, hubspot_id}
    """
    email = item["email"]
    if should_skip(email):
        return {"status": "IGNORATO", "email": email, "hubspot_id": None}

    domain = email.partition("@")[2]
    firstname, lastname = split_name(item["display_name"])
    company = company_from_domain(domain)

    existing = hs_find_contact(email)
    if existing:
        contact_id = existing["id"]
        props = existing.get("properties", {})
        updates: dict = {}
        if not props.get("firstname") and firstname:
            updates["firstname"] = firstname
        if not props.get("lastname") and lastname:
            updates["lastname"] = lastname
        if not props.get("company") and company:
            updates["company"] = company
        if updates:
            hs_update_contact(contact_id, updates)
        hs_log_note(contact_id, item["subject"], item["date"])
        return {"status": "AGGIORNATO", "email": email, "hubspot_id": contact_id}
    else:
        created = hs_create_contact(email, firstname, lastname, company)
        contact_id = created["id"]
        hs_log_note(contact_id, item["subject"], item["date"])
        return {"status": "CREATO", "email": email, "hubspot_id": contact_id}


# ── Entry point ───────────────────────────────────────────────────────────────

def run(days_back: int = 7) -> list[dict]:
    logger.info("Gmail → HubSpot sync  |  lookback: %d days", days_back)

    senders = fetch_inbox_senders(days_back=days_back)
    logger.info("Unique senders found in inbox: %d", len(senders))

    results = []
    for item in senders:
        result = process_sender(item)
        logger.info(
            "[%-10s]  %-45s  HubSpot ID: %s",
            result["status"],
            result["email"],
            result["hubspot_id"] or "—",
        )
        results.append(result)

    created  = sum(1 for r in results if r["status"] == "CREATO")
    updated  = sum(1 for r in results if r["status"] == "AGGIORNATO")
    ignored  = sum(1 for r in results if r["status"] == "IGNORATO")

    logger.info("─" * 60)
    logger.info("TOTALE: %d   CREATO: %d   AGGIORNATO: %d   IGNORATO: %d",
                len(results), created, updated, ignored)
    return results


def main():
    parser = argparse.ArgumentParser(description="Sync Gmail senders → HubSpot contacts")
    parser.add_argument("--days", type=int, default=7, help="Look back N days (default 7)")
    args = parser.parse_args()
    run(days_back=args.days)


if __name__ == "__main__":
    main()
