#!/usr/bin/env python3
"""
Gmail → HubSpot contact sync.

Scans the Gmail inbox for recent inbound emails, extracts sender data,
then creates or updates the matching HubSpot contact (keyed by email).

Environment variables:
  GMAIL_CREDENTIALS_FILE   Path to Gmail OAuth2 client-secret JSON
  GMAIL_TOKEN_FILE         Path to persisted OAuth token JSON (created on first run)
  HUBSPOT_API_KEY          HubSpot Private App access token
  LOOKBACK_DAYS            Days of inbox history to scan (default: 1)
"""

import os
import re
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import hubspot
from hubspot.crm.contacts import SimplePublicObjectInput, ApiException
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Patterns for automated / system senders to skip
_SKIP_PATTERNS = [
    r"^mailer-daemon@",
    r"@.*facebookmail\.com$",
    r"noreply@",
    r"no-reply@",
    r"@googlemail\.com$",
    r"^analytics-",
    r"^notification@",
    r"^postmaster@",
    r"^bounce",
    r"^do-not-reply@",
    r"^donotreply@",
]

# Generic free-mail domains — don't derive a company name from these
_GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "libero.it", "alice.it", "live.com", "icloud.com",
    "tiscali.it", "virgilio.it",
}


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def _gmail_credentials() -> Credentials:
    token_file = os.environ["GMAIL_TOKEN_FILE"]
    creds_file = os.environ["GMAIL_CREDENTIALS_FILE"]

    creds: Optional[Credentials] = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_file, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as fh:
            fh.write(creds.to_json())

    return creds


def build_gmail_service():
    return build("gmail", "v1", credentials=_gmail_credentials())


def _is_automated(email: str) -> bool:
    email = email.lower()
    return any(re.search(p, email) for p in _SKIP_PATTERNS)


def _parse_sender(raw_from: str) -> tuple[str, str, str]:
    """Parse a From header into (email, firstname, lastname)."""
    email = first = last = ""

    m = re.match(r'^"?([^"<]+?)"?\s*<([^>]+)>', raw_from)
    if m:
        display = m.group(1).strip()
        email = m.group(2).strip().lower()
        parts = display.split()
        if len(parts) >= 2:
            first, last = parts[0], " ".join(parts[1:])
        elif parts:
            first = parts[0]
    else:
        email = raw_from.strip().lower()

    return email, first, last


def _company_from_domain(email: str) -> str:
    domain = email.split("@")[-1].lower()
    if domain in _GENERIC_DOMAINS:
        return ""
    name = domain.split(".")[0].replace("-", " ").replace("_", " ")
    return name.title()


def get_recent_senders(service, lookback_days: int) -> list[dict]:
    """Return a de-duplicated list of sender dicts from the last N days."""
    query = f"in:inbox newer_than:{lookback_days}d -from:me"
    seen: set[str] = set()
    contacts: list[dict] = []
    page_token = None

    while True:
        kwargs: dict = {"userId": "me", "q": query, "maxResults": 100}
        if page_token:
            kwargs["pageToken"] = page_token

        resp = service.users().messages().list(**kwargs).execute()
        for meta in resp.get("messages", []):
            msg = service.users().messages().get(
                userId="me", id=meta["id"],
                format="metadata", metadataHeaders=["From"],
            ).execute()
            headers = {h["name"]: h["value"]
                       for h in msg.get("payload", {}).get("headers", [])}
            raw_from = headers.get("From", "")
            if not raw_from:
                continue
            email, first, last = _parse_sender(raw_from)
            if not email or email in seen or _is_automated(email):
                continue
            seen.add(email)
            contacts.append({
                "email": email,
                "firstname": first,
                "lastname": last,
                "company": _company_from_domain(email),
            })

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return contacts


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def _find_contact(hs_client, email: str) -> Optional[object]:
    try:
        result = hs_client.crm.contacts.search_api.do_search(
            public_object_search_request={
                "filterGroups": [{
                    "filters": [{"propertyName": "email", "operator": "EQ", "value": email}]
                }],
                "properties": ["email", "firstname", "lastname", "company", "lead_source"],
                "limit": 1,
            }
        )
        return result.results[0] if result.results else None
    except ApiException as exc:
        log.warning("HubSpot search failed for %s: %s", email, exc)
        return None


def upsert_contact(hs_client, sender: dict) -> tuple[str, str]:
    """Create or update a HubSpot contact. Returns (status, hubspot_id)."""
    existing = _find_contact(hs_client, sender["email"])

    new_props: dict = {"lead_source": "Gmail"}
    for field in ("firstname", "lastname", "company"):
        if sender.get(field):
            new_props[field] = sender[field]

    try:
        if existing:
            current = existing.properties or {}
            # Only fill in fields that are currently blank
            update_props = {
                k: v for k, v in new_props.items()
                if k == "lead_source" or not current.get(k)
            }
            if update_props:
                hs_client.crm.contacts.basic_api.update(
                    contact_id=existing.id,
                    simple_public_object_input=SimplePublicObjectInput(
                        properties=update_props
                    ),
                )
            return "updated", existing.id

        new_props["email"] = sender["email"]
        result = hs_client.crm.contacts.basic_api.create(
            simple_public_object_input=SimplePublicObjectInput(properties=new_props)
        )
        return "created", result.id

    except ApiException as exc:
        log.error("HubSpot upsert error for %s: %s", sender["email"], exc)
        return "error", ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> list[dict]:
    lookback_days = int(os.environ.get("LOOKBACK_DAYS", "1"))

    gmail_svc = build_gmail_service()
    hs_client = hubspot.Client.create(access_token=os.environ["HUBSPOT_API_KEY"])

    senders = get_recent_senders(gmail_svc, lookback_days)
    log.info("Unique senders to process: %d", len(senders))

    results = []
    for sender in senders:
        status, hs_id = upsert_contact(hs_client, sender)
        log.info("[%s] %-50s  HubSpot ID: %s", status.upper(), sender["email"], hs_id)
        results.append({"status": status, "email": sender["email"], "hubspot_id": hs_id})

    created = sum(1 for r in results if r["status"] == "created")
    updated = sum(1 for r in results if r["status"] == "updated")
    errors  = sum(1 for r in results if r["status"] == "error")
    log.info("Done — created: %d  updated: %d  errors: %d", created, updated, errors)

    return results


if __name__ == "__main__":
    main()
