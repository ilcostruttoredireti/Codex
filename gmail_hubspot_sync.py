"""
Gmail → HubSpot Contact Sync
Monitors all incoming Gmail messages and syncs sender contacts to HubSpot.
Avoids duplicates by using email as the unique key.
"""

import os
import re
import base64
import json
from datetime import datetime, timezone
from typing import Optional

# ── Google Gmail API ──────────────────────────────────────────────────────────
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── HubSpot API ───────────────────────────────────────────────────────────────
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInput
from hubspot.crm.contacts.exceptions import ApiException

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
HUBSPOT_TOKEN = os.environ["HUBSPOT_ACCESS_TOKEN"]   # Private App token
GMAIL_TOKEN_FILE = os.environ.get("GMAIL_TOKEN_FILE", "gmail_token.json")
GMAIL_CREDENTIALS_FILE = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")

# Senders to skip – cannot be contacted back, no value as CRM contacts
NOREPLY_PREFIXES = (
    "no-reply", "noreply", "nobody", "do-not-reply", "donotreply",
    "mailer-daemon", "postmaster", "bounce", "notify-noreply",
    "admanager-noreply", "googlebase-noreply", "sc-noreply",
)


# ─────────────────────────────────────────────────────────────────────────────
# Gmail helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if os.path.exists(GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def is_noreply(email: str) -> bool:
    local = email.split("@")[0].lower()
    return local.startswith(NOREPLY_PREFIXES)


def extract_sender(raw_from: str) -> dict:
    """Parse 'Name <email>' or bare 'email' into {name, email, domain}."""
    match = re.match(r"^(?P<name>.+?)\s+<(?P<email>[^>]+)>$", raw_from.strip())
    if match:
        name = match.group("name").strip().strip('"')
        email = match.group("email").strip().lower()
    else:
        email = raw_from.strip().lower()
        name = ""

    domain = email.split("@")[-1] if "@" in email else ""
    return {"name": name, "email": email, "domain": domain}


def parse_name(name: str) -> tuple[str, str]:
    """Split a display name into (firstname, lastname)."""
    parts = name.strip().split(None, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0] if parts else "", "")


def company_from_domain(domain: str) -> str:
    """Best-effort company name from domain (e.g. 'martes-ai.com' → 'Martes Ai')."""
    stem = domain.split(".")[0]
    return stem.replace("-", " ").replace("_", " ").title()


def fetch_inbox_senders(service, max_results: int = 100, newer_than: str = "7d") -> list[dict]:
    """Return deduped list of sender dicts from recent inbox messages."""
    query = f"in:inbox -from:me newer_than:{newer_than}"
    seen: dict[str, dict] = {}

    page_token = None
    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": min(max_results, 500)}
        if page_token:
            kwargs["pageToken"] = page_token

        resp = service.users().messages().list(**kwargs).execute()
        messages = resp.get("messages", [])

        for msg_ref in messages:
            msg = service.users().messages().get(
                userId="me", id=msg_ref["id"], format="metadata",
                metadataHeaders=["From", "Date"]
            ).execute()

            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            raw_from = headers.get("From", "")
            if not raw_from:
                continue

            sender = extract_sender(raw_from)
            email = sender["email"]

            if not email or is_noreply(email):
                continue
            if email not in seen:
                seen[email] = sender

        page_token = resp.get("nextPageToken")
        if not page_token or len(seen) >= max_results:
            break

    return list(seen.values())


# ─────────────────────────────────────────────────────────────────────────────
# HubSpot helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_hubspot_client():
    return hubspot.Client.create(access_token=HUBSPOT_TOKEN)


def find_contact_by_email(client, email: str) -> Optional[dict]:
    """Return existing HubSpot contact dict or None."""
    try:
        results = client.crm.contacts.search_api.do_search(
            public_object_search_request={
                "filterGroups": [
                    {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
                ],
                "properties": ["email", "firstname", "lastname", "company", "hs_lead_source"],
                "limit": 1,
            }
        )
        if results.total > 0:
            return results.results[0]
    except ApiException:
        pass
    return None


def create_contact(client, sender: dict) -> Optional[str]:
    """Create a new HubSpot contact. Returns hs_object_id or None on failure."""
    firstname, lastname = parse_name(sender["name"]) if sender["name"] else ("", "")
    company = company_from_domain(sender["domain"])

    props = {
        "email": sender["email"],
        "hs_lead_source": "OTHER",      # closest standard value; annotated below
        "hs_analytics_source": "OTHER",
    }
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company

    try:
        resp = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create={"properties": props}
        )
        # Add a note tagging this contact as Inbound Gmail
        _add_gmail_note(client, resp.id, sender["email"])
        return resp.id
    except ApiException:
        return None


def update_contact(client, contact_id: str, sender: dict, existing_props: dict) -> bool:
    """Fill in any missing fields on an existing contact. Returns True if updated."""
    updates = {}

    if not existing_props.get("company"):
        updates["company"] = company_from_domain(sender["domain"])

    if not existing_props.get("hs_lead_source"):
        updates["hs_lead_source"] = "OTHER"

    if sender["name"] and not existing_props.get("firstname"):
        fn, ln = parse_name(sender["name"])
        if fn:
            updates["firstname"] = fn
        if ln:
            updates["lastname"] = ln

    if not updates:
        return False

    try:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return True
    except ApiException:
        return False


def _add_gmail_note(client, contact_id: str, email: str):
    """Log an 'Inbound Gmail' engagement note on the contact."""
    try:
        client.crm.objects.basic_api.create(
            object_type="notes",
            simple_public_object_input_for_create={
                "properties": {
                    "hs_note_body": f"Contatto acquisito via Gmail Inbound da: {email}",
                    "hs_timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                },
                "associations": [
                    {
                        "to": {"id": contact_id},
                        "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
                    }
                ],
            },
        )
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Main sync loop
# ─────────────────────────────────────────────────────────────────────────────

def run_sync(newer_than: str = "1d") -> list[dict]:
    gmail = get_gmail_service()
    hs = get_hubspot_client()

    senders = fetch_inbox_senders(gmail, newer_than=newer_than)
    print(f"Found {len(senders)} unique contactable senders in inbox (last {newer_than})\n")

    results = []

    for sender in senders:
        email = sender["email"]
        existing = find_contact_by_email(hs, email)

        if existing is None:
            contact_id = create_contact(hs, sender)
            status = "Creato" if contact_id else "Errore"
            results.append({"stato": status, "email": email, "hubspot_id": contact_id or "—"})

        else:
            contact_id = existing.id
            props = existing.properties or {}
            updated = update_contact(hs, contact_id, sender, props)
            status = "Aggiornato" if updated else "Ignorato"
            results.append({"stato": status, "email": email, "hubspot_id": contact_id})

    return results


def print_report(results: list[dict]):
    print(f"{'Stato':<12} {'Email':<45} {'HubSpot ID'}")
    print("-" * 80)
    for r in results:
        print(f"{r['stato']:<12} {r['email']:<45} {r['hubspot_id']}")

    totals = {"Creato": 0, "Aggiornato": 0, "Ignorato": 0, "Errore": 0}
    for r in results:
        totals[r["stato"]] = totals.get(r["stato"], 0) + 1

    print("\nRiepilogo:")
    for k, v in totals.items():
        if v:
            print(f"  {k}: {v}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sync Gmail senders → HubSpot contacts")
    parser.add_argument("--newer-than", default="1d", help="Gmail time range (e.g. 1d, 7d)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    report = run_sync(newer_than=args.newer_than)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)
