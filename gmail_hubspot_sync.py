"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail messages and syncs senders as HubSpot contacts.

Run: python gmail_hubspot_sync.py [--days 7]

Dependencies:
    pip install google-auth google-auth-oauthlib google-auth-httplib2
                google-api-python-client hubspot-api-client python-dotenv

Auth:
    Gmail  → OAuth2, credentials in GMAIL_CREDENTIALS_PATH env var
             (download from Google Cloud Console, APIs & Services → Credentials)
    HubSpot→ Private App token in HUBSPOT_ACCESS_TOKEN env var
"""

import argparse
import base64
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SKIP_PREFIXES = (
    "noreply", "no-reply", "no_reply",
    "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster",
    "conferma-ordine", "order-confirm",
    "admanager-noreply",
)
SKIP_DOMAINS = {
    "amazonses.com", "bounce.com", "notifications.hubspot.com",
}
GMAIL_SOURCE_LABEL = "Inbound Gmail"
HUBSPOT_SOURCE_VALUE = "EMAIL_MARKETING"   # nearest standard HS source type
# Note: hs_analytics_source_data_1 is read-only via API; source is tracked
# via hs_analytics_source + an "Inbound Gmail" NOTE on the contact timeline.


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SenderContact:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""

    @property
    def domain(self) -> str:
        return self.email.split("@")[-1] if "@" in self.email else ""

    @property
    def company_from_domain(self) -> str:
        """Best-effort company name: strip common subdomains and TLD."""
        parts = self.domain.split(".")
        if len(parts) >= 2:
            return parts[-2].capitalize()
        return self.domain


@dataclass
class SyncResult:
    email: str
    status: str          # "Creato" | "Aggiornato" | "Ignorato"
    hubspot_id: Optional[str] = None
    reason: str = ""


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def _build_gmail_service():
    """Return an authenticated Gmail API service object."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
    creds_path = os.environ.get("GMAIL_CREDENTIALS_PATH", "credentials.json")
    token_path = os.environ.get("GMAIL_TOKEN_PATH", "token.json")

    creds = None
    if Path(token_path).exists():
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
        Path(token_path).write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _parse_name(display: str) -> tuple[str, str]:
    """Split 'First Last' into (first, last). Single token → first only."""
    parts = display.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return (parts[0] if parts else ""), ""


def _should_skip(email: str) -> bool:
    local, _, domain = email.partition("@")
    if domain in SKIP_DOMAINS:
        return True
    for prefix in SKIP_PREFIXES:
        if local.lower().startswith(prefix):
            return True
    return False


def fetch_senders(days: int = 7, max_results: int = 200) -> list[SenderContact]:
    """Return unique SenderContact objects from recent inbox messages."""
    service = _build_gmail_service()
    query = f"in:inbox -from:me newer_than:{days}d"
    seen: dict[str, SenderContact] = {}

    page_token = None
    fetched = 0
    while fetched < max_results:
        kwargs = dict(userId="me", q=query, maxResults=min(50, max_results - fetched))
        if page_token:
            kwargs["pageToken"] = page_token

        resp = service.users().messages().list(**kwargs).execute()
        messages = resp.get("messages", [])
        if not messages:
            break

        for msg_stub in messages:
            msg = service.users().messages().get(
                userId="me", id=msg_stub["id"], format="metadata",
                metadataHeaders=["From"]
            ).execute()
            headers = {h["name"]: h["value"]
                       for h in msg.get("payload", {}).get("headers", [])}
            raw_from = headers.get("From", "")
            display, email = parseaddr(raw_from)
            email = email.lower().strip()

            if not email or _should_skip(email) or email in seen:
                continue

            first, last = _parse_name(display) if display else ("", "")
            contact = SenderContact(
                email=email, first_name=first, last_name=last
            )
            contact.company = contact.company_from_domain
            seen[email] = contact
            fetched += 1

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    log.info("Gmail: %d unique senders found", len(seen))
    return list(seen.values())


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def _build_hubspot_client():
    from hubspot import HubSpot
    token = os.environ["HUBSPOT_ACCESS_TOKEN"]
    return HubSpot(access_token=token)


def _lookup_contact(client, email: str) -> Optional[dict]:
    """Return existing HubSpot contact dict or None."""
    from hubspot.crm.contacts import ApiException
    try:
        result = client.crm.contacts.search_api.do_search(
            public_object_search_request={
                "filterGroups": [{"filters": [
                    {"propertyName": "email", "operator": "EQ", "value": email}
                ]}],
                "properties": [
                    "email", "firstname", "lastname", "company",
                    "hs_analytics_source", "hs_analytics_source_data_1",
                ],
                "limit": 1,
            }
        )
        if result.results:
            return result.results[0]
    except ApiException as exc:
        log.warning("HubSpot search error for %s: %s", email, exc)
    return None


def _create_contact(client, contact: SenderContact) -> Optional[str]:
    from hubspot.crm.contacts import ApiException, SimplePublicObjectInputForCreate
    props = {
        "email": contact.email,
        "firstname": contact.first_name or contact.domain.split(".")[0].capitalize(),
        "company": contact.company,
        "hs_analytics_source": HUBSPOT_SOURCE_VALUE,
    }
    if contact.last_name:
        props["lastname"] = contact.last_name

    try:
        created = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return created.id
    except ApiException as exc:
        log.error("HubSpot create error for %s: %s", contact.email, exc)
        return None


def _update_contact(client, contact_id: str, props: dict) -> bool:
    from hubspot.crm.contacts import ApiException, SimplePublicObjectInput
    try:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=props)
        )
        return True
    except ApiException as exc:
        log.error("HubSpot update error for %s: %s", contact_id, exc)
        return False


def _add_note(client, contact_id: str, body: str) -> None:
    """Create a timeline NOTE associated with the contact."""
    from hubspot.crm.objects.notes import ApiException as NoteApiException
    from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate
    from hubspot.crm.associations.v4.models import AssociationSpec, PublicAssociationDefinition
    try:
        note = client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties={"hs_note_body": body, "hs_timestamp": str(int(time.time() * 1000))},
                associations=[{
                    "to": {"id": contact_id},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
                }],
            )
        )
    except Exception as exc:
        log.warning("Could not add note to contact %s: %s", contact_id, exc)


# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------

def sync_contact(client, contact: SenderContact) -> SyncResult:
    existing = _lookup_contact(client, contact.email)

    if existing is None:
        new_id = _create_contact(client, contact)
        if new_id:
            _add_note(client, new_id,
                      f"[{GMAIL_SOURCE_LABEL}] Contatto creato da email ricevuta: {contact.email}.")
            log.info("CREATO   %s  [HS id %s]", contact.email, new_id)
            return SyncResult(contact.email, "Creato", new_id)
        return SyncResult(contact.email, "Ignorato", reason="create failed")

    hs_id = existing.id
    current_props = existing.properties or {}

    updates: dict[str, str] = {}

    # Fill in missing fields
    if not current_props.get("company") and contact.company:
        updates["company"] = contact.company
    if not current_props.get("firstname") and contact.first_name:
        updates["firstname"] = contact.first_name
    if not current_props.get("lastname") and contact.last_name:
        updates["lastname"] = contact.last_name

    # Mark source as Gmail if not already set
    if not current_props.get("hs_analytics_source"):
        updates["hs_analytics_source"] = HUBSPOT_SOURCE_VALUE

    if updates:
        _update_contact(client, hs_id, updates)
        _add_note(client, hs_id,
                  f"[{GMAIL_SOURCE_LABEL}] Email ricevuta da {contact.email}.")
        log.info("AGGIORNATO %s  [HS id %s]  fields=%s",
                 contact.email, hs_id, list(updates))
        return SyncResult(contact.email, "Aggiornato", hs_id,
                          reason=f"updated {list(updates)}")

    log.info("IGNORATO %s  [HS id %s]  (nessuna modifica)", contact.email, hs_id)
    return SyncResult(contact.email, "Ignorato", hs_id, reason="already up to date")


def run_sync(days: int = 7) -> list[SyncResult]:
    client = _build_hubspot_client()
    senders = fetch_senders(days=days)

    results: list[SyncResult] = []
    for contact in senders:
        result = sync_contact(client, contact)
        results.append(result)
        time.sleep(0.1)   # gentle rate-limiting

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Sync Gmail senders to HubSpot")
    parser.add_argument("--days", type=int, default=7,
                        help="Look back N days (default: 7)")
    parser.add_argument("--json", action="store_true",
                        help="Output results as JSON")
    args = parser.parse_args()

    results = run_sync(days=args.days)

    created   = [r for r in results if r.status == "Creato"]
    updated   = [r for r in results if r.status == "Aggiornato"]
    ignored   = [r for r in results if r.status == "Ignorato"]

    if args.json:
        output = [
            {"stato": r.status, "email": r.email,
             "hubspot_id": r.hubspot_id, "note": r.reason}
            for r in results
        ]
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        print(f"\n{'─'*60}")
        print(f"  Gmail → HubSpot Sync  |  ultimi {args.days} giorni")
        print(f"{'─'*60}")
        print(f"  Processati : {len(results)}")
        print(f"  Creati     : {len(created)}")
        print(f"  Aggiornati : {len(updated)}")
        print(f"  Ignorati   : {len(ignored)}")
        print(f"{'─'*60}")
        for r in results:
            icon = {"Creato": "✚", "Aggiornato": "↺", "Ignorato": "·"}[r.status]
            print(f"  {icon} [{r.status:<12}]  {r.email:<45}  HS:{r.hubspot_id or '—'}")
        print(f"{'─'*60}\n")


if __name__ == "__main__":
    main()
