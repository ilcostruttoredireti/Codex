"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox and upserts sender contacts into HubSpot.

Requirements:
  pip install google-auth google-auth-oauthlib google-api-python-client hubspot-api-client python-dotenv

Environment variables (.env):
  GOOGLE_CREDENTIALS_FILE  - path to OAuth2 credentials JSON from Google Cloud Console
  GOOGLE_TOKEN_FILE        - path where the access token is cached (created on first run)
  HUBSPOT_ACCESS_TOKEN     - HubSpot private app access token
  STATE_FILE               - JSON file tracking last-processed email timestamp (default: .state.json)

Usage:
  python sync.py          # process emails from the last 24 hours
  python sync.py --hours 48  # look back 48 hours
"""

import json
import os
import re
import time
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import SimplePublicObjectInput

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
OWN_DOMAINS = {"gmail.com"}  # add your own sending domains here
IGNORED_SENDERS = {
    "no-reply", "noreply", "mailer-daemon", "postmaster",
    "donotreply", "do-not-reply", "notifications",
}
GMAIL_SOURCE_LABEL = "Gmail"
STATE_FILE = os.getenv("STATE_FILE", ".state.json")


# ── helpers ──────────────────────────────────────────────────────────────────

def _gmail_service():
    token_file = os.getenv("GOOGLE_TOKEN_FILE", "token.json")
    creds = None
    if Path(token_file).exists():
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                os.environ["GOOGLE_CREDENTIALS_FILE"], SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as fh:
            fh.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _hubspot_client():
    return hubspot.Client.create(access_token=os.environ["HUBSPOT_ACCESS_TOKEN"])


def _load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as fh:
            return json.load(fh)
    return {"last_run": None, "processed_ids": []}


def _save_state(state: dict):
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=2)


def _parse_sender(raw: str) -> tuple[str, str, str]:
    """Return (email, firstname, lastname) from a raw From header value."""
    match = re.match(r'^(?:"?([^"<]*)"?\s+)?<([^>]+)>$', raw.strip())
    if match:
        name_raw = (match.group(1) or "").strip()
        email = match.group(2).strip().lower()
    else:
        email = raw.strip().lower()
        name_raw = ""

    parts = name_raw.split() if name_raw else []
    firstname = parts[0] if parts else ""
    lastname = " ".join(parts[1:]) if len(parts) > 1 else ""
    return email, firstname, lastname


def _company_from_domain(domain: str) -> str:
    """Best-effort company name from email domain."""
    skip = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
            "icloud.com", "live.com", "me.com", "protonmail.com", "libero.it",
            "virgilio.it", "tiscali.it", "alice.it"}
    if domain in skip:
        return ""
    name = domain.split(".")[0].replace("-", " ").replace("_", " ").title()
    return name


def _is_automated(email: str) -> bool:
    local = email.split("@")[0]
    return any(p in local for p in IGNORED_SENDERS)


# ── HubSpot note helper ───────────────────────────────────────────────────────

def _add_gmail_note(hs: hubspot.Client, contact_id: str, sender: dict):
    """Attach a note to a contact recording the Gmail inbound activity."""
    today = datetime.now().strftime("%d/%m/%Y")
    body = (
        f"\U0001f4e7 Email inbound ricevuta via Gmail il {today}. "
        f"Fonte contatto: Gmail. Tag: Inbound Gmail."
    )
    note = hs.crm.objects.basic_api.create(
        object_type="notes",
        simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
            properties={
                "hs_note_body": body,
                "hs_timestamp": datetime.now(timezone.utc).isoformat(),
            },
            associations=[{
                "to": {"id": contact_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
            }],
        ),
    )
    return note


# ── core sync logic ───────────────────────────────────────────────────────────

def fetch_recent_senders(service, hours: int, processed_ids: list) -> list[dict]:
    """Return a deduplicated list of sender dicts from inbox messages."""
    after = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp())
    query = f"in:inbox -from:me after:{after}"
    result = service.users().messages().list(
        userId="me", q=query, maxResults=200
    ).execute()

    messages = result.get("messages", [])
    seen_emails: set[str] = set()
    senders: list[dict] = []

    for msg_ref in messages:
        msg_id = msg_ref["id"]
        if msg_id in processed_ids:
            continue

        msg = service.users().messages().get(
            userId="me", id=msg_id, format="metadata",
            metadataHeaders=["From", "Date"]
        ).execute()

        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        raw_from = headers.get("From", "")
        email, firstname, lastname = _parse_sender(raw_from)
        domain = email.split("@")[-1] if "@" in email else ""

        if not email or _is_automated(email):
            continue
        if email in seen_emails:
            continue
        seen_emails.add(email)

        senders.append({
            "message_id": msg_id,
            "email": email,
            "firstname": firstname,
            "lastname": lastname,
            "domain": domain,
            "company": _company_from_domain(domain),
        })

    return senders


def upsert_contact(hs: hubspot.Client, sender: dict) -> dict:
    """Create or update a HubSpot contact. Returns result dict."""
    email = sender["email"]

    # Search for existing contact by email
    try:
        search_resp = hs.crm.contacts.search_api.do_search(
            public_object_search_request={
                "filterGroups": [{
                    "filters": [{
                        "propertyName": "email",
                        "operator": "EQ",
                        "value": email,
                    }]
                }],
                "properties": ["email", "firstname", "lastname", "company",
                               "hs_analytics_source_data_1"],
                "limit": 1,
            }
        )
    except ApiException as e:
        return {"status": "Errore", "email": email, "hubspot_id": None, "error": str(e)}

    results = search_resp.results

    props_to_set = {}
    if sender.get("firstname"):
        props_to_set["firstname"] = sender["firstname"]
    if sender.get("lastname"):
        props_to_set["lastname"] = sender["lastname"]
    if sender.get("company"):
        props_to_set["company"] = sender["company"]

    if not results:
        # CREATE
        props_to_set["email"] = email
        try:
            contact = hs.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props_to_set
                )
            )
            _add_gmail_note(hs, contact.id, sender)
            return {"status": "Creato", "email": email, "hubspot_id": contact.id}
        except ApiException as e:
            return {"status": "Errore", "email": email, "hubspot_id": None, "error": str(e)}

    # UPDATE — only fill missing fields
    existing = results[0]
    ex_props = existing.properties
    update_props = {}

    for field in ("firstname", "lastname", "company"):
        if props_to_set.get(field) and not ex_props.get(field):
            update_props[field] = props_to_set[field]

    if update_props:
        try:
            hs.crm.contacts.basic_api.update(
                contact_id=existing.id,
                simple_public_object_input=SimplePublicObjectInput(properties=update_props),
            )
        except ApiException as e:
            return {"status": "Errore", "email": email, "hubspot_id": None, "error": str(e)}

    # Always log the Gmail activity as a note
    _add_gmail_note(hs, existing.id, sender)
    status = "Aggiornato" if update_props else "Ignorato"
    return {"status": status, "email": email, "hubspot_id": existing.id}


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=24,
                        help="Look-back window in hours (default: 24)")
    args = parser.parse_args()

    state = _load_state()
    gmail = _gmail_service()
    hs = _hubspot_client()

    print(f"\n{'─'*60}")
    print(f"  Gmail → HubSpot Sync  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'─'*60}")

    senders = fetch_recent_senders(gmail, args.hours, state.get("processed_ids", []))
    print(f"Nuovi mittenti da elaborare: {len(senders)}\n")

    results = []
    for s in senders:
        res = upsert_contact(hs, s)
        results.append(res)
        icon = {"Creato": "✚", "Aggiornato": "↑", "Ignorato": "–", "Errore": "✗"}.get(res["status"], "?")
        print(f"  {icon} [{res['status']:<10}] {res['email']:<45} ID: {res.get('hubspot_id', 'N/A')}")
        state.setdefault("processed_ids", []).append(s["message_id"])
        time.sleep(0.1)  # respect HubSpot rate limits

    print(f"\n{'─'*60}")
    counts = {s: sum(1 for r in results if r["status"] == s) for s in ("Creato", "Aggiornato", "Ignorato", "Errore")}
    print(f"  Creati: {counts['Creato']}  Aggiornati: {counts['Aggiornato']}  Ignorati: {counts['Ignorato']}  Errori: {counts['Errore']}")
    print(f"{'─'*60}\n")

    state["last_run"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)
    return results


if __name__ == "__main__":
    main()
