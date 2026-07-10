#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox for new emails and syncs senders to HubSpot CRM.

Setup:
    pip install google-auth-oauthlib google-api-python-client hubspot-api-client

Environment variables:
    HUBSPOT_ACCESS_TOKEN   - HubSpot private app access token
    GMAIL_CREDENTIALS_FILE - Path to Gmail OAuth credentials JSON
    GMAIL_TOKEN_FILE       - Path to stored Gmail OAuth token (auto-created)
    LOOKBACK_DAYS          - Days of email history to scan (default: 1)
"""

import os
import re
import json
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from dataclasses import dataclass, field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Sender:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    domain: str = field(init=False)

    def __post_init__(self):
        self.domain = self.email.split("@")[1] if "@" in self.email else ""

    @classmethod
    def from_header(cls, raw: str) -> "Sender":
        """Parse 'Name <email>' or bare 'email' header into a Sender."""
        match = re.match(r'^"?([^"<]+)"?\s*<([^>]+)>', raw.strip())
        if match:
            name, email = match.group(1).strip(), match.group(2).strip().lower()
        else:
            email = raw.strip().lower()
            name = ""

        firstname, lastname = _split_name(name, email)
        domain = email.split("@")[1] if "@" in email else ""
        company = _company_from_domain(domain)
        return cls(email=email, firstname=firstname, lastname=lastname, company=company)


@dataclass
class SyncResult:
    email: str
    hubspot_id: Optional[str]
    status: str  # "Creato" | "Aggiornato" | "Ignorato"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Prefixes that indicate automated senders – skip them
_SKIP_PREFIXES = (
    "noreply", "no-reply", "no_reply", "donotreply",
    "mailer", "mailer-daemon", "mailerdaemon",
    "postmaster", "bounce", "bounces",
    "notification", "notifications",
    "newsletter", "alerts", "alert",
    "automated", "automailer",
    "pinbot", "bot@",
)

# Well-known automated domains to skip entirely
_SKIP_DOMAINS = {
    "google.com", "googlemail.com", "youtube.com", "gmail.com",
    "facebook.com", "twitter.com", "linkedin.com",
    "amazonses.com", "sendgrid.net", "mailchimp.com",
    "constantcontact.com", "klaviyo.com",
}


def _is_automated(email: str) -> bool:
    local, _, domain = email.partition("@")
    if domain in _SKIP_DOMAINS:
        return True
    for prefix in _SKIP_PREFIXES:
        if local.startswith(prefix) or local == prefix.rstrip("@"):
            return True
    return False


def _split_name(display_name: str, email: str) -> tuple[str, str]:
    """Return (firstname, lastname) from a display name or email local part."""
    if display_name:
        parts = display_name.split()
        if len(parts) >= 2:
            return parts[0].capitalize(), " ".join(parts[1:]).capitalize()
        return parts[0].capitalize(), ""

    # Fall back to capitalising the local-part of the email
    local = email.split("@")[0]
    parts = re.split(r"[._\-+]", local)
    if len(parts) >= 2:
        return parts[0].capitalize(), parts[1].capitalize()
    return local.capitalize(), ""


def _company_from_domain(domain: str) -> str:
    """Best-effort company name from domain (strips TLD)."""
    parts = domain.split(".")
    # drop common sub-domains
    if len(parts) > 2 and parts[0] in ("mail", "email", "info", "news", "get",
                                        "go", "hello", "hi", "m", "marketing",
                                        "engage", "notification"):
        parts = parts[1:]
    name = parts[0] if parts else domain
    return name.replace("-", " ").replace("_", " ").title()


# ---------------------------------------------------------------------------
# Gmail client
# ---------------------------------------------------------------------------

def _get_gmail_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
    creds_file = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
    token_file = os.environ.get("GMAIL_TOKEN_FILE", "token.json")

    creds = None
    if os.path.exists(token_file):
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


def fetch_inbox_senders(lookback_days: int = 1) -> list[Sender]:
    """Return unique, non-automated Senders from inbox messages in the last N days."""
    service = _get_gmail_service()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y/%m/%d")
    query = f"in:inbox -from:me after:{cutoff}"

    senders: dict[str, Sender] = {}
    page_token = None

    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 100}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute()

        for msg_ref in resp.get("messages", []):
            msg = service.users().messages().get(
                userId="me", id=msg_ref["id"], format="metadata",
                metadataHeaders=["From"]
            ).execute()
            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            raw_from = headers.get("From", "")
            if not raw_from:
                continue
            sender = Sender.from_header(raw_from)
            if _is_automated(sender.email):
                continue
            if sender.email not in senders:
                senders[sender.email] = sender

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return list(senders.values())


# ---------------------------------------------------------------------------
# HubSpot client
# ---------------------------------------------------------------------------

def _hubspot_client():
    from hubspot import HubSpot
    token = os.environ["HUBSPOT_ACCESS_TOKEN"]
    return HubSpot(access_token=token)


def _find_contact(client, email: str) -> Optional[dict]:
    from hubspot.crm.contacts import PublicObjectSearchRequest

    search = PublicObjectSearchRequest(
        filter_groups=[{
            "filters": [{
                "propertyName": "email",
                "operator": "EQ",
                "value": email,
            }]
        }],
        properties=["email", "firstname", "lastname", "company", "hs_analytics_source"],
        limit=1,
    )
    resp = client.crm.contacts.search_api.do_search(public_object_search_request=search)
    return resp.results[0].to_dict() if resp.results else None


def _create_contact(client, sender: Sender) -> str:
    from hubspot.crm.contacts.models import SimplePublicObjectInputForCreate

    props = {
        "email": sender.email,
        "firstname": sender.firstname,
        # hs_analytics_source: standard HubSpot traffic-source field.
        # EMAIL_MARKETING is the closest built-in value for inbound Gmail.
        # Note: hs_analytics_source_data_1/2 are system-managed (read-only via API).
        "hs_analytics_source": "EMAIL_MARKETING",
    }
    if sender.lastname:
        props["lastname"] = sender.lastname
    if sender.company:
        props["company"] = sender.company

    obj = SimplePublicObjectInputForCreate(properties=props)
    result = client.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=obj
    )
    return result.id


def _update_contact(client, contact_id: str, sender: Sender, existing: dict) -> bool:
    from hubspot.crm.contacts.models import SimplePublicObjectInput

    existing_props = existing.get("properties", {})
    updates: dict[str, str] = {}

    if not existing_props.get("hs_analytics_source"):
        updates["hs_analytics_source"] = "EMAIL_MARKETING"
    if not existing_props.get("company") and sender.company:
        updates["company"] = sender.company
    if not existing_props.get("lastname") and sender.lastname:
        updates["lastname"] = sender.lastname
    if not existing_props.get("firstname") and sender.firstname:
        updates["firstname"] = sender.firstname

    if not updates:
        return False

    obj = SimplePublicObjectInput(properties=updates)
    client.crm.contacts.basic_api.update(
        contact_id=contact_id,
        simple_public_object_input=obj,
    )
    return True


# ---------------------------------------------------------------------------
# Main sync loop
# ---------------------------------------------------------------------------

def sync_once(lookback_days: int = 1) -> list[SyncResult]:
    """Run one pass: fetch Gmail senders → upsert to HubSpot."""
    client = _hubspot_client()
    senders = fetch_inbox_senders(lookback_days)
    log.info("Found %d unique non-automated senders in the last %d day(s).", len(senders), lookback_days)

    results: list[SyncResult] = []

    for sender in senders:
        try:
            existing = _find_contact(client, sender.email)
            if existing is None:
                contact_id = _create_contact(client, sender)
                status = "Creato"
                log.info("[Creato]    %s → HubSpot ID %s", sender.email, contact_id)
            else:
                contact_id = existing["id"]
                updated = _update_contact(client, contact_id, sender, existing)
                status = "Aggiornato" if updated else "Ignorato"
                log.info("[%s] %s → HubSpot ID %s", status.ljust(10), sender.email, contact_id)

            results.append(SyncResult(email=sender.email, hubspot_id=contact_id, status=status))

        except Exception as exc:
            log.error("Error processing %s: %s", sender.email, exc)
            results.append(SyncResult(email=sender.email, hubspot_id=None, status="Errore"))

        time.sleep(0.2)  # stay within HubSpot rate limits

    return results


def print_report(results: list[SyncResult]) -> None:
    print("\n" + "=" * 60)
    print(f"{'STATO':<12} {'EMAIL':<38} {'HUBSPOT ID'}")
    print("-" * 60)
    for r in results:
        print(f"{r.status:<12} {r.email:<38} {r.hubspot_id or 'N/A'}")
    print("=" * 60)
    totals = {s: sum(1 for r in results if r.status == s)
              for s in ("Creato", "Aggiornato", "Ignorato", "Errore")}
    print("  ".join(f"{k}: {v}" for k, v in totals.items() if v))
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    lookback = int(os.environ.get("LOOKBACK_DAYS", "1"))
    results = sync_once(lookback_days=lookback)
    print_report(results)
