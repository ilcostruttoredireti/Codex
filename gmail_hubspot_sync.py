#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox for new emails, extracts sender contacts,
and creates or updates them in HubSpot. Avoids duplicates by
using the sender email as the unique key.

Dependencies:
    pip install google-auth google-auth-oauthlib google-api-python-client hubspot-api-client

Environment variables required:
    GMAIL_CREDENTIALS_FILE   Path to OAuth2 credentials JSON (from Google Cloud Console)
    GMAIL_TOKEN_FILE         Path to store/load the OAuth2 token (default: gmail_token.json)
    HUBSPOT_API_KEY          HubSpot Private App access token
    GMAIL_USER               Gmail address to monitor (default: me)
    STATE_FILE               Path to sync-state JSON (default: sync_state.json)
"""

import json
import os
import re
import sys
import time
import logging
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Skip-list: automated / noreply / system senders ─────────────────────────
SKIP_DOMAINS = {
    "facebookmail.com",
    "googlemail.com",
    "google.com",
    "mailer-daemon",
    "accounts.google.com",
    "noreply.github.com",
    "notifications.google.com",
    "bounce.em.sendgrid.net",
}
SKIP_PREFIXES = {"noreply", "no-reply", "mailer-daemon", "postmaster", "bounce", "analytics-noreply"}


def _should_skip(email: str, own_addresses: set[str]) -> bool:
    email = email.lower()
    if email in own_addresses:
        return True
    local, _, domain = email.partition("@")
    if domain in SKIP_DOMAINS:
        return True
    if any(domain.endswith("." + d) for d in SKIP_DOMAINS):
        return True
    if any(local.startswith(p) for p in SKIP_PREFIXES):
        return True
    return False


def _parse_name(display_name: str) -> tuple[str, str]:
    """Split a display name into (firstname, lastname). Best-effort."""
    parts = display_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def _domain_to_company(email: str) -> str:
    """Heuristic: strip TLD and capitalise the domain root as company name."""
    domain = email.split("@")[-1].lower()
    root = domain.split(".")[0]
    # Remove common generic prefixes
    for strip in ("mail", "smtp", "info", "hello", "contact"):
        if root == strip and len(domain.split(".")) > 1:
            root = domain.split(".")[1]
    return root.replace("-", " ").title()


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def _build_gmail_service():
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
    creds_file = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
    token_file = os.environ.get("GMAIL_TOKEN_FILE", "gmail_token.json")

    creds = None
    if Path(token_file).exists():
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as fh:
            fh.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_new_senders(service, user_id: str, since_timestamp: int, own_addresses: set[str]) -> list[dict]:
    """
    Returns a de-duplicated list of contact dicts for every new sender
    found in the inbox since `since_timestamp` (Unix ms).

    Contact dict keys: email, firstname, lastname, company, message_id, date
    """
    query_parts = ["in:inbox", f"after:{since_timestamp // 1000}"]
    query = " ".join(query_parts)
    log.info("Gmail query: %s", query)

    seen_emails: set[str] = set()
    contacts: list[dict] = []
    page_token = None

    while True:
        kwargs = {"userId": user_id, "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        result = service.users().messages().list(**kwargs).execute()
        messages = result.get("messages", [])
        log.info("  fetched %d messages", len(messages))

        for msg_stub in messages:
            msg = service.users().messages().get(
                userId=user_id, id=msg_stub["id"], format="metadata",
                metadataHeaders=["From", "Date"]
            ).execute()

            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            raw_from = headers.get("From", "")
            raw_date = headers.get("Date", "")
            label_ids = msg.get("labelIds", [])

            # Only process inbox messages
            if "INBOX" not in label_ids:
                continue

            display_name, email = parseaddr(raw_from)
            email = email.lower().strip()
            if not email or email in seen_emails:
                continue
            if _should_skip(email, own_addresses):
                log.debug("  skip %s", email)
                continue

            seen_emails.add(email)
            firstname, lastname = _parse_name(display_name) if display_name else ("", "")
            company = _domain_to_company(email)

            contacts.append({
                "email": email,
                "firstname": firstname,
                "lastname": lastname,
                "company": company,
                "message_id": msg_stub["id"],
                "date": raw_date,
            })

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return contacts


# ── HubSpot helpers ───────────────────────────────────────────────────────────

def _build_hubspot_client():
    from hubspot import HubSpot
    api_key = os.environ.get("HUBSPOT_API_KEY")
    if not api_key:
        raise RuntimeError("HUBSPOT_API_KEY environment variable is not set")
    return HubSpot(access_token=api_key)


def find_contact_by_email(client, email: str) -> dict | None:
    from hubspot.crm.contacts import ApiException

    try:
        result = client.crm.contacts.search_api.do_search(
            public_object_search_request={
                "filterGroups": [{"filters": [
                    {"propertyName": "email", "operator": "EQ", "value": email}
                ]}],
                "properties": ["email", "firstname", "lastname", "company", "hs_lead_status"],
                "limit": 1,
            }
        )
        if result.results:
            return result.results[0]
    except ApiException as exc:
        log.warning("HubSpot search error for %s: %s", email, exc)
    return None


def create_contact(client, contact: dict) -> str:
    """Create a new HubSpot contact. Returns the new contact ID."""
    props = {
        "email": contact["email"],
        "hs_lead_status": "NEW",
    }
    if contact.get("firstname"):
        props["firstname"] = contact["firstname"]
    if contact.get("lastname"):
        props["lastname"] = contact["lastname"]
    if contact.get("company"):
        props["company"] = contact["company"]

    result = client.crm.contacts.basic_api.create(
        simple_public_object_input_for_create={"properties": props}
    )
    return result.id


def update_contact(client, contact_id: str, contact: dict, existing: dict) -> bool:
    """
    Update a contact only if there are missing fields to fill in.
    Returns True if an update was actually sent.
    """
    existing_props = existing.properties if hasattr(existing, "properties") else {}
    updates = {}

    for field in ("firstname", "lastname", "company"):
        if contact.get(field) and not existing_props.get(field):
            updates[field] = contact[field]

    if not existing_props.get("hs_lead_status"):
        updates["hs_lead_status"] = "NEW"

    if not updates:
        return False

    client.crm.contacts.basic_api.update(
        contact_id=contact_id,
        simple_public_object_input={"properties": updates}
    )
    return True


# ── State management ──────────────────────────────────────────────────────────

def load_state(path: str) -> dict:
    if Path(path).exists():
        with open(path) as fh:
            return json.load(fh)
    # Default: look back 7 days
    seven_days_ago = int((time.time() - 7 * 86400) * 1000)
    return {"last_sync_ts": seven_days_ago, "processed_ids": []}


def save_state(path: str, state: dict) -> None:
    with open(path, "w") as fh:
        json.dump(state, fh, indent=2)


# ── Main sync loop ────────────────────────────────────────────────────────────

def run_sync():
    state_file = os.environ.get("STATE_FILE", "sync_state.json")
    gmail_user = os.environ.get("GMAIL_USER", "me")

    own_addresses = {gmail_user.lower()} if gmail_user != "me" else set()

    state = load_state(state_file)
    since_ts = state["last_sync_ts"]
    processed_ids: set[str] = set(state.get("processed_ids", []))

    log.info("Starting sync — last run: %s",
             datetime.fromtimestamp(since_ts / 1000, tz=timezone.utc).isoformat())

    gmail = _build_gmail_service()
    hubspot = _build_hubspot_client()

    senders = fetch_new_senders(gmail, gmail_user, since_ts, own_addresses)
    log.info("Unique new senders to process: %d", len(senders))

    results = []
    new_max_ts = since_ts

    for contact in senders:
        msg_id = contact["message_id"]
        email = contact["email"]

        if msg_id in processed_ids:
            results.append({"status": "IGNORATO", "email": email, "hubspot_id": None})
            continue

        existing = find_contact_by_email(hubspot, email)

        if existing is None:
            try:
                new_id = create_contact(hubspot, contact)
                results.append({"status": "CREATO", "email": email, "hubspot_id": new_id})
                log.info("  CREATO  %s  → ID %s", email, new_id)
            except Exception as exc:
                log.error("  ERROR creating %s: %s", email, exc)
                results.append({"status": "ERRORE", "email": email, "hubspot_id": None})
        else:
            contact_id = existing.id if hasattr(existing, "id") else existing["id"]
            updated = update_contact(hubspot, contact_id, contact, existing)
            if updated:
                results.append({"status": "AGGIORNATO", "email": email, "hubspot_id": contact_id})
                log.info("  AGGIORNATO  %s  → ID %s", email, contact_id)
            else:
                results.append({"status": "IGNORATO", "email": email, "hubspot_id": contact_id})
                log.info("  IGNORATO  %s  (nessun campo mancante)", email)

        processed_ids.add(msg_id)

    # Print results table
    print("\n" + "=" * 72)
    print(f"{'STATO':<12}  {'EMAIL':<42}  {'HUBSPOT ID'}")
    print("-" * 72)
    for r in results:
        print(f"{r['status']:<12}  {r['email']:<42}  {r['hubspot_id'] or '-'}")
    print("=" * 72)
    created = sum(1 for r in results if r["status"] == "CREATO")
    updated = sum(1 for r in results if r["status"] == "AGGIORNATO")
    ignored = sum(1 for r in results if r["status"] == "IGNORATO")
    print(f"\nRisultato: {created} creati, {updated} aggiornati, {ignored} ignorati\n")

    # Save state
    state["last_sync_ts"] = int(time.time() * 1000)
    state["processed_ids"] = list(processed_ids)[-5000:]  # keep last 5k
    save_state(state_file, state)
    log.info("State saved to %s", state_file)

    return results


if __name__ == "__main__":
    run_sync()
