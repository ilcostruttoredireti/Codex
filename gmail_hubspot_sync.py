#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox for new inbound emails and syncs senders to HubSpot CRM.
Deduplication: uses 'HubSpot Synced' Gmail label (ID: Label_32) to mark
processed messages so they are never re-processed on subsequent runs.

Usage:
    export HUBSPOT_ACCESS_TOKEN="your_token"
    python gmail_hubspot_sync.py

OAuth credentials must be stored in token.json (Gmail OAuth2).
"""

import re
import os
import logging
from typing import Optional

# pip install google-auth google-auth-oauthlib google-api-python-client hubspot-api-client
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from hubspot import HubSpot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate
from hubspot.crm.contacts.exceptions import ApiException

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# Gmail label used as deduplication marker (created once in Gmail, ID is stable)
HUBSPOT_SYNCED_LABEL_ID = "Label_32"

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

# Patterns that identify automated / no-reply senders to skip
_SKIP_PATTERNS = [
    re.compile(r"no.?reply", re.I),
    re.compile(r"noreply", re.I),
    re.compile(r"donotreply", re.I),
    re.compile(r"^notifications?@", re.I),
    re.compile(r"^confirm@", re.I),
    re.compile(r"^nobody@", re.I),
    re.compile(r"^daemon@", re.I),
    re.compile(r"^mailer-daemon@", re.I),
    re.compile(r"^postmaster@", re.I),
]

_SKIP_DOMAINS = frozenset({
    "youtube.com",
    "linkedin.com",
    "mailchimp.com",
    "e.feedspot.com",
    "feedspot.com",
    "bounce.amazon.com",
})


def _should_skip(email: str) -> bool:
    email = email.lower().strip()
    domain = email.split("@")[-1]
    if domain in _SKIP_DOMAINS:
        return True
    local = email.split("@")[0]
    for pattern in _SKIP_PATTERNS:
        if pattern.search(local):
            return True
    return False


def _parse_from_header(header: str) -> tuple[str, Optional[str]]:
    """Return (email, display_name_or_None) from a From: header value."""
    match = re.search(r"<(.+?)>", header)
    if match:
        email = match.group(1).strip().lower()
        name = header[: match.start()].strip().strip('"').strip("'")
        return email, name or None
    return header.strip().lower(), None


def _name_from_email(email: str) -> tuple[str, Optional[str]]:
    """Best-effort first/last name from email local part."""
    local = email.split("@")[0]
    parts = re.split(r"[._\-]", local)
    # Looks like a personal address: john.doe, chelsea.c, etc.
    if len(parts) >= 2 and all(p.isalpha() for p in parts[:2]):
        return parts[0].capitalize(), parts[1].capitalize()
    return parts[0].capitalize(), None


def _company_from_domain(domain: str) -> str:
    """Derive a readable company name from an email domain."""
    # Strip leading subdomain(s), keep registrable part
    parts = domain.split(".")
    if len(parts) > 2:
        domain = ".".join(parts[-2:])
    name = domain.split(".")[0]
    return name.replace("-", " ").title()


def _get_gmail_service():
    creds = None
    token_path = "token.json"
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _label_as_synced(gmail, message_id: str):
    gmail.users().messages().modify(
        userId="me",
        id=message_id,
        body={"addLabelIds": [HUBSPOT_SYNCED_LABEL_ID]},
    ).execute()


def _find_hubspot_contact(hs: HubSpot, email: str) -> Optional[dict]:
    response = hs.crm.contacts.search_api.do_search(
        public_object_search_request={
            "filterGroups": [{"filters": [
                {"propertyName": "email", "operator": "EQ", "value": email}
            ]}],
            "properties": ["email", "firstname", "lastname", "company"],
        }
    )
    results = response.results
    return results[0] if results else None


def sync_gmail_to_hubspot(max_messages: int = 50) -> list[dict]:
    gmail = _get_gmail_service()
    hs = HubSpot(access_token=os.environ["HUBSPOT_ACCESS_TOKEN"])

    # Only fetch inbox messages NOT yet labeled as synced
    query = 'in:inbox -label:"HubSpot Synced" -from:me'
    resp = gmail.users().messages().list(
        userId="me", q=query, maxResults=max_messages
    ).execute()
    messages = resp.get("messages", [])
    logger.info("Found %d unsynced inbox messages", len(messages))

    report: list[dict] = []

    for msg_ref in messages:
        msg = gmail.users().messages().get(
            userId="me",
            id=msg_ref["id"],
            format="metadata",
            metadataHeaders=["From"],
        ).execute()

        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        sender_email, display_name = _parse_from_header(headers.get("From", ""))

        # Always mark the message as processed, even if we skip the contact
        _label_as_synced(gmail, msg_ref["id"])

        if _should_skip(sender_email):
            logger.debug("Skipping automated sender: %s", sender_email)
            report.append({"status": "Ignorato", "email": sender_email, "hubspot_id": None})
            continue

        domain = sender_email.split("@")[-1]
        company = _company_from_domain(domain)

        if display_name and " " in display_name:
            parts = display_name.split(" ", 1)
            firstname, lastname = parts[0], parts[1]
        elif display_name:
            firstname, lastname = display_name, None
        else:
            firstname, lastname = _name_from_email(sender_email)

        try:
            existing = _find_hubspot_contact(hs, sender_email)

            if existing:
                contact_id = existing.id
                props = existing.properties or {}
                updates = {}
                if not props.get("company") and company:
                    updates["company"] = company
                if not props.get("firstname") and firstname:
                    updates["firstname"] = firstname
                if not props.get("lastname") and lastname:
                    updates["lastname"] = lastname

                if updates:
                    hs.crm.contacts.basic_api.update(
                        contact_id=contact_id,
                        simple_public_object_input={"properties": updates},
                    )
                    status = "Aggiornato"
                    logger.info("Aggiornato: %s (ID %s)", sender_email, contact_id)
                else:
                    status = "Ignorato"
                    logger.debug("Nessuna modifica: %s (ID %s)", sender_email, contact_id)

            else:
                properties = {
                    "email": sender_email,
                    "firstname": firstname or sender_email.split("@")[0],
                    "company": company,
                    "hs_analytics_source_data_1": "Gmail",
                }
                if lastname:
                    properties["lastname"] = lastname

                result = hs.crm.contacts.basic_api.create(
                    simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                        properties=properties
                    )
                )
                contact_id = result.id
                status = "Creato"
                logger.info("Creato: %s (ID %s)", sender_email, contact_id)

        except ApiException as exc:
            logger.error("Errore HubSpot per %s: %s", sender_email, exc)
            report.append({"status": "Errore", "email": sender_email, "hubspot_id": None})
            continue

        report.append({"status": status, "email": sender_email, "hubspot_id": contact_id})

    return report


def _print_report(report: list[dict]):
    print("\n=== REPORT SINCRONIZZAZIONE GMAIL → HUBSPOT ===")
    print(f"{'Stato':<12} {'Email':<48} {'HubSpot ID'}")
    print("-" * 75)
    for row in report:
        print(f"{row['status']:<12} {row['email']:<48} {row['hubspot_id'] or 'N/A'}")
    totals = {}
    for row in report:
        totals[row["status"]] = totals.get(row["status"], 0) + 1
    print()
    for status, count in totals.items():
        print(f"  {status}: {count}")


if __name__ == "__main__":
    report = sync_gmail_to_hubspot()
    _print_report(report)
