"""
Gmail → HubSpot Contact Sync
=============================
Monitors Gmail inbox for incoming emails, extracts sender contact info,
and syncs to HubSpot (create or update, no duplicates).

Requirements:
    pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib hubspot-api-client

Setup:
    1. Gmail: create OAuth2 credentials at https://console.cloud.google.com
       - Enable Gmail API, download credentials.json
    2. HubSpot: create a private app at https://app.hubspot.com/
       - Scopes: crm.objects.contacts.read, crm.objects.contacts.write
       - Copy the access token
    3. Set environment variables:
         HUBSPOT_ACCESS_TOKEN=your_token
         GMAIL_CREDENTIALS_PATH=./credentials.json   (optional, default shown)
         GMAIL_TOKEN_PATH=./token.json               (optional)
         SYNC_LOOKBACK_HOURS=24                      (optional, default: 24)
"""

import os
import re
import json
import email as email_lib
from datetime import datetime, timedelta, timezone
from typing import Optional

# ── Google / Gmail ───────────────────────────────────────────────────────────
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── HubSpot ───────────────────────────────────────────────────────────────────
from hubspot import HubSpot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate
from hubspot.crm.contacts.exceptions import ApiException

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Senders to skip (automated/no-reply)
SKIP_PREFIXES = ("noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon",
                 "postmaster", "bounce", "notifications", "notify", "alert",
                 "admanager", "googlebase", "sc-noreply")
SKIP_DOMAINS = ("google.com", "youtube.com", "revolut.com", "facebook.com",
                "twitter.com", "linkedin.com", "instagram.com", "e.feedspot.com")


# ── Auth helpers ──────────────────────────────────────────────────────────────

def get_gmail_service():
    """Authenticate and return a Gmail API service object."""
    creds_path = os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json")
    token_path = os.getenv("GMAIL_TOKEN_PATH", "token.json")
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


def get_hubspot_client() -> HubSpot:
    token = os.environ["HUBSPOT_ACCESS_TOKEN"]
    return HubSpot(access_token=token)


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def should_skip(sender_email: str) -> bool:
    """Return True for automated/no-reply senders that should not become contacts."""
    local, _, domain = sender_email.partition("@")
    local_lower = local.lower()
    domain_lower = domain.lower()
    if any(local_lower.startswith(p) for p in SKIP_PREFIXES):
        return True
    if any(domain_lower == d or domain_lower.endswith("." + d) for d in SKIP_DOMAINS):
        return True
    return False


def parse_sender(raw_from: str) -> tuple[str, Optional[str], Optional[str]]:
    """
    Parse a 'From' header like 'John Doe <john@example.com>' or 'john@example.com'.
    Returns (email, firstname, lastname).
    """
    match = re.match(r'"?([^"<]+)"?\s*<([^>]+)>', raw_from.strip())
    if match:
        display_name = match.group(1).strip()
        sender_email = match.group(2).strip().lower()
    else:
        sender_email = raw_from.strip().lower()
        display_name = ""

    firstname, lastname = None, None
    if display_name:
        parts = display_name.split()
        if len(parts) >= 2:
            firstname = parts[0].capitalize()
            lastname = " ".join(parts[1:]).capitalize()
        elif len(parts) == 1:
            firstname = parts[0].capitalize()

    return sender_email, firstname, lastname


def company_from_domain(domain: str) -> str:
    """Derive a company name from the email domain (best-effort)."""
    # strip subdomains, common TLDs; turn dashes into spaces; title-case
    root = domain.split(".")[-2] if "." in domain else domain
    return root.replace("-", " ").title()


def get_recent_senders(service, lookback_hours: int = 24) -> list[dict]:
    """Return a deduplicated list of {email, firstname, lastname, company, domain} dicts."""
    since = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).strftime("%Y/%m/%d")
    query = f"in:inbox after:{since} -in:draft -in:sent"

    results = service.users().messages().list(userId="me", q=query, maxResults=100).execute()
    messages = results.get("messages", [])

    seen: dict[str, dict] = {}
    for msg_stub in messages:
        msg = service.users().messages().get(
            userId="me", id=msg_stub["id"], format="metadata",
            metadataHeaders=["From"],
        ).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        raw_from = headers.get("From", "")
        if not raw_from:
            continue

        email_addr, firstname, lastname = parse_sender(raw_from)
        if not email_addr or "@" not in email_addr:
            continue
        if should_skip(email_addr):
            continue
        if email_addr in seen:
            continue  # keep first occurrence

        _, _, domain = email_addr.partition("@")
        seen[email_addr] = {
            "email": email_addr,
            "firstname": firstname,
            "lastname": lastname,
            "domain": domain,
            "company": company_from_domain(domain),
        }

    return list(seen.values())


# ── HubSpot helpers ───────────────────────────────────────────────────────────

def find_contact_by_email(hs: HubSpot, email: str) -> Optional[dict]:
    """Return the HubSpot contact dict if found, else None."""
    from hubspot.crm.contacts import PublicObjectSearchRequest, Filter, FilterGroup

    f = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[f])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company", "leadsource"],
        limit=1,
    )
    resp = hs.crm.contacts.search_api.do_search(public_object_search_request=req)
    if resp.results:
        return resp.results[0]
    return None


def upsert_contact(hs: HubSpot, sender: dict) -> dict:
    """
    Create or update a HubSpot contact for the given sender.
    Returns {'status': 'Creato'|'Aggiornato'|'Ignorato', 'email': ..., 'id': ...}.
    """
    existing = find_contact_by_email(hs, sender["email"])

    props_to_set: dict[str, str] = {"leadsource": "Gmail"}

    if existing:
        contact_id = existing.id
        current = existing.properties or {}

        # Only fill genuinely empty fields
        if sender.get("firstname") and not current.get("firstname"):
            props_to_set["firstname"] = sender["firstname"]
        if sender.get("lastname") and not current.get("lastname"):
            props_to_set["lastname"] = sender["lastname"]
        if sender.get("company") and not current.get("company"):
            props_to_set["company"] = sender["company"]

        hs.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input={"properties": props_to_set},
        )
        return {"status": "Aggiornato", "email": sender["email"], "id": contact_id}

    # Create new
    all_props = {
        "email": sender["email"],
        "leadsource": "Gmail",
    }
    if sender.get("firstname"):
        all_props["firstname"] = sender["firstname"]
    if sender.get("lastname"):
        all_props["lastname"] = sender["lastname"]
    if sender.get("company"):
        all_props["company"] = sender["company"]

    created = hs.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
            properties=all_props
        )
    )
    return {"status": "Creato", "email": sender["email"], "id": created.id}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    lookback = int(os.getenv("SYNC_LOOKBACK_HOURS", "24"))

    print(f"[{datetime.now().isoformat()}] Avvio sync Gmail → HubSpot (ultime {lookback}h)")

    gmail = get_gmail_service()
    hs = get_hubspot_client()

    senders = get_recent_senders(gmail, lookback_hours=lookback)
    print(f"  Mittenti unici trovati: {len(senders)}")

    results = []
    for sender in senders:
        try:
            result = upsert_contact(hs, sender)
            results.append(result)
            print(f"  [{result['status']:10s}] {result['email']} → ID {result['id']}")
        except ApiException as exc:
            print(f"  [ERRORE    ] {sender['email']}: {exc}")
            results.append({"status": "Errore", "email": sender["email"], "id": None})

    # Summary
    created = sum(1 for r in results if r["status"] == "Creato")
    updated = sum(1 for r in results if r["status"] == "Aggiornato")
    errors = sum(1 for r in results if r["status"] == "Errore")
    print(f"\nRiepilogo: {created} creati | {updated} aggiornati | {errors} errori")

    return results


if __name__ == "__main__":
    main()
