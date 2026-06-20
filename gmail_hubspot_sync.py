"""
Gmail → HubSpot Contact Sync
Monitors the Gmail inbox, extracts sender info, and creates/updates HubSpot contacts.

Usage:
    python gmail_hubspot_sync.py [--days N]

Requires env vars:
    GMAIL_CREDENTIALS_FILE  – path to OAuth2 credentials JSON
    GMAIL_TOKEN_FILE        – path to stored OAuth2 token JSON
    HUBSPOT_ACCESS_TOKEN    – private-app access token

Output (stdout): one JSON line per processed email, schema:
    {"status": "Creato"|"Aggiornato"|"Ignorato", "email": "...", "hubspot_id": "..."}
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── Gmail ──────────────────────────────────────────────────────────────────

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

SKIP_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "libero.it", "virgilio.it", "tiscali.it",
}

PERSONAL_EMAIL_RE = re.compile(r"^\d{4,}@|noreply@|no-reply@|mailer@|postmaster@|bounce@")


def _gmail_service():
    token_file = os.environ.get("GMAIL_TOKEN_FILE", "token.json")
    creds_file = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def list_inbox_senders(service, since_days: int = 1) -> list[dict]:
    """Return unique senders from inbox messages newer than `since_days` days."""
    after = int((datetime.now(timezone.utc) - timedelta(days=since_days)).timestamp())
    query = f"in:inbox -from:me after:{after}"
    senders: dict[str, dict] = {}
    page_token = None
    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 500, "fields": "messages(id),nextPageToken"}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute()
        for msg in resp.get("messages", []):
            msg_data = service.users().messages().get(
                userId="me", id=msg["id"],
                format="metadata",
                metadataHeaders=["From", "Date"],
            ).execute()
            headers = {h["name"]: h["value"] for h in msg_data.get("payload", {}).get("headers", [])}
            raw_from = headers.get("From", "")
            display_name, address = parseaddr(raw_from)
            address = address.strip().lower()
            if not address or address in senders:
                continue
            if PERSONAL_EMAIL_RE.search(address):
                continue
            domain = address.split("@")[-1] if "@" in address else ""
            senders[address] = {
                "email": address,
                "display_name": display_name.strip(),
                "domain": domain,
            }
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return list(senders.values())


# ── Name parsing ───────────────────────────────────────────────────────────

def _split_name(display_name: str) -> tuple[str, str]:
    parts = display_name.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _company_from_domain(domain: str) -> str:
    if not domain:
        return ""
    # strip common TLDs and format
    name = domain.split(".")[0]
    return name.replace("-", " ").replace("_", " ").title()


# ── HubSpot ────────────────────────────────────────────────────────────────

HS_BASE = "https://api.hubapi.com"


def _hs_headers() -> dict:
    token = os.environ["HUBSPOT_ACCESS_TOKEN"]
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def hs_find_contact(email: str) -> dict | None:
    payload = {
        "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_source"],
        "limit": 1,
    }
    r = requests.post(f"{HS_BASE}/crm/v3/objects/contacts/search", headers=_hs_headers(), json=payload)
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(sender: dict) -> str:
    first, last = _split_name(sender["display_name"])
    company = (
        sender.get("company")
        or (None if sender["domain"] in SKIP_DOMAINS else _company_from_domain(sender["domain"]))
        or ""
    )
    props = {
        "email": sender["email"],
        "firstname": first or sender["email"].split("@")[0],
        "lastname": last,
        "company": company,
        "hs_lead_source": "OTHER",           # HubSpot enum; maps to custom label below
        "lead_source_detail": "Gmail",        # custom property if available
    }
    # remove empty strings so HubSpot doesn't complain
    props = {k: v for k, v in props.items() if v}
    r = requests.post(f"{HS_BASE}/crm/v3/objects/contacts", headers=_hs_headers(), json={"properties": props})
    r.raise_for_status()
    return r.json()["id"]


def hs_update_contact(contact_id: str, sender: dict, existing: dict) -> bool:
    """Fill in any blank fields. Returns True if an update was actually sent."""
    existing_props = existing.get("properties", {})
    updates = {}
    if not existing_props.get("company"):
        company = (
            None if sender["domain"] in SKIP_DOMAINS else _company_from_domain(sender["domain"])
        )
        if company:
            updates["company"] = company
    if not existing_props.get("hs_lead_source"):
        updates["hs_lead_source"] = "OTHER"
    if not updates:
        return False
    r = requests.patch(
        f"{HS_BASE}/crm/v3/objects/contacts/{contact_id}",
        headers=_hs_headers(),
        json={"properties": updates},
    )
    r.raise_for_status()
    return True


# ── Main ───────────────────────────────────────────────────────────────────

def run(since_days: int = 1) -> list[dict]:
    service = _gmail_service()
    senders = list_inbox_senders(service, since_days)
    results = []
    for sender in senders:
        try:
            existing = hs_find_contact(sender["email"])
            if existing is None:
                hs_id = hs_create_contact(sender)
                status = "Creato"
            else:
                hs_id = existing["id"]
                updated = hs_update_contact(hs_id, sender, existing)
                status = "Aggiornato" if updated else "Ignorato"
        except Exception as exc:
            status = f"Errore: {exc}"
            hs_id = ""
        record = {"status": status, "email": sender["email"], "hubspot_id": hs_id}
        results.append(record)
        print(json.dumps(record, ensure_ascii=False))
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync Gmail senders to HubSpot")
    parser.add_argument("--days", type=int, default=1, help="How many days back to scan (default 1)")
    args = parser.parse_args()
    run(since_days=args.days)
