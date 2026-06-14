#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox for new inbound emails, extracts sender info,
and creates or updates HubSpot contacts, avoiding duplicates.

Requirements:
    pip install google-auth google-auth-oauthlib google-auth-httplib2
                google-api-python-client hubspot-api-client python-dotenv

Environment variables (set in .env or shell):
    HUBSPOT_ACCESS_TOKEN   – HubSpot private-app token
    GMAIL_CREDENTIALS_FILE – path to OAuth2 credentials JSON (default: credentials.json)
    GMAIL_TOKEN_FILE       – path to token cache file (default: token.json)
    SYNC_DAYS_BACK         – how many days back to scan (default: 1)
    SKIP_DOMAINS           – comma-separated domains to ignore (e.g. gmail.com internal)

Usage:
    python gmail_hubspot_sync.py            # single run
    python gmail_hubspot_sync.py --watch    # poll every 5 min
"""

import os
import re
import json
import time
import argparse
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

# ── Gmail ─────────────────────────────────────────────────────────────────────
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── HubSpot ───────────────────────────────────────────────────────────────────
from hubspot import HubSpot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import PublicObjectSearchRequest, Filter, FilterGroup

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")

HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
SYNC_DAYS_BACK = int(os.getenv("SYNC_DAYS_BACK", "1"))

# Domains that should never be synced (internal, bots, notifications)
_default_skip = "facebookmail.com,googlemail.com,mailer-daemon.google.com"
SKIP_DOMAINS = set(
    d.strip().lower()
    for d in os.getenv("SKIP_DOMAINS", _default_skip).split(",")
    if d.strip()
)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Data types ────────────────────────────────────────────────────────────────
@dataclass
class SenderContact:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    domain: str = field(init=False)

    def __post_init__(self):
        self.email = self.email.strip().lower()
        self.domain = self.email.split("@")[-1] if "@" in self.email else ""

    @property
    def company_from_domain(self) -> str:
        """Best-effort company name derived from the email domain."""
        if not self.domain:
            return ""
        # strip TLD and convert - / _ to spaces
        parts = self.domain.split(".")
        name = parts[0] if len(parts) >= 2 else self.domain
        return name.replace("-", " ").replace("_", " ").title()


@dataclass
class SyncResult:
    email: str
    status: str          # "created" | "updated" | "skipped" | "error"
    contact_id: Optional[str] = None
    detail: str = ""


# ── Gmail helpers ──────────────────────────────────────────────────────────────

def _gmail_credentials() -> Credentials:
    creds = None
    if os.path.exists(GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(GMAIL_CREDENTIALS_FILE):
                raise FileNotFoundError(
                    f"Gmail credentials file not found: {GMAIL_CREDENTIALS_FILE}\n"
                    "Download it from Google Cloud Console → APIs & Services → Credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds


def fetch_inbound_senders(days_back: int = 1) -> list[SenderContact]:
    """Return unique external senders from inbox messages in the last `days_back` days."""
    service = build("gmail", "v1", credentials=_gmail_credentials())

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime(
        "%Y/%m/%d"
    )
    query = f"in:inbox -from:me newer_than:{days_back}d after:{cutoff}"

    seen: dict[str, SenderContact] = {}
    page_token = None

    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 50}
        if page_token:
            kwargs["pageToken"] = page_token

        resp = service.users().messages().list(**kwargs).execute()
        messages = resp.get("messages", [])

        for msg_ref in messages:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=msg_ref["id"], format="metadata",
                     metadataHeaders=["From", "Reply-To"])
                .execute()
            )
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            raw_from = headers.get("Reply-To") or headers.get("From", "")
            contact = _parse_sender(raw_from)
            if contact and contact.email not in seen:
                seen[contact.email] = contact
                log.debug("Found sender: %s", contact.email)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    log.info("Fetched %d unique external senders from Gmail", len(seen))
    return list(seen.values())


def _parse_sender(raw_from: str) -> Optional[SenderContact]:
    """Parse 'Name <email>' or 'email' into a SenderContact, or return None to skip."""
    if not raw_from:
        return None

    display_name, email = parseaddr(raw_from)
    if not email or "@" not in email:
        return None

    email = email.strip().lower()
    domain = email.split("@")[-1]

    if domain in SKIP_DOMAINS:
        return None
    # skip noreply / mailer-daemon style addresses
    local = email.split("@")[0]
    if re.match(r"^(noreply|no-reply|mailer-daemon|postmaster|bounce|notify|notification)", local):
        return None

    firstname = lastname = ""
    if display_name:
        parts = display_name.strip().split(None, 1)
        firstname = parts[0].title()
        lastname = parts[1].title() if len(parts) > 1 else ""

    return SenderContact(email=email, firstname=firstname, lastname=lastname)


# ── HubSpot helpers ────────────────────────────────────────────────────────────

def _hs_client() -> HubSpot:
    if not HUBSPOT_TOKEN:
        raise ValueError(
            "HUBSPOT_ACCESS_TOKEN is not set. "
            "Create a private app in HubSpot → Settings → Integrations → Private Apps."
        )
    return HubSpot(access_token=HUBSPOT_TOKEN)


def _search_contact(client: HubSpot, email: str) -> Optional[dict]:
    """Return the first HubSpot contact matching this email, or None."""
    filter_ = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[filter_])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
        limit=1,
    )
    resp = client.crm.contacts.search_api.do_search(public_object_search_request=req)
    return resp.results[0].to_dict() if resp.results else None


def _contact_properties(sender: SenderContact) -> dict:
    company = sender.company or sender.company_from_domain
    props = {
        "email": sender.email,
        "hs_lead_source": "OFFLINE",   # closest writable standard value; notes "Gmail" below
        "lead_source_detail": "Gmail Inbound",
    }
    if sender.firstname:
        props["firstname"] = sender.firstname
    if sender.lastname:
        props["lastname"] = sender.lastname
    if company:
        props["company"] = company
    return props


def _build_update_props(sender: SenderContact, existing: dict) -> dict:
    """Return only the properties that need to be set (fill blanks, never overwrite)."""
    ep = existing.get("properties", {})
    updates: dict = {}

    if sender.firstname and not ep.get("firstname"):
        updates["firstname"] = sender.firstname
    if sender.lastname and not ep.get("lastname"):
        updates["lastname"] = sender.lastname
    if not ep.get("company"):
        company = sender.company or sender.company_from_domain
        if company:
            updates["company"] = company
    if not ep.get("hs_lead_source"):
        updates["hs_lead_source"] = "OFFLINE"
        updates["lead_source_detail"] = "Gmail Inbound"

    return updates


def sync_to_hubspot(sender: SenderContact, client: HubSpot) -> SyncResult:
    """Create or update a HubSpot contact. Returns a SyncResult."""
    try:
        existing = _search_contact(client, sender.email)

        if existing is None:
            # ── CREATE ─────────────────────────────────────────────────────
            props = _contact_properties(sender)
            body = SimplePublicObjectInputForCreate(properties=props, associations=[])
            created = client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=body
            )
            log.info("CREATED  %s  (id=%s)", sender.email, created.id)
            return SyncResult(email=sender.email, status="created", contact_id=str(created.id))

        # ── UPDATE ─────────────────────────────────────────────────────────
        contact_id = str(existing["id"])
        updates = _build_update_props(sender, existing)

        if not updates:
            log.info("SKIPPED  %s  (id=%s, no new data)", sender.email, contact_id)
            return SyncResult(email=sender.email, status="skipped", contact_id=contact_id,
                              detail="all fields already populated")

        from hubspot.crm.contacts import SimplePublicObjectInput
        patch = SimplePublicObjectInput(properties=updates)
        client.crm.contacts.basic_api.update(contact_id=contact_id, simple_public_object_input=patch)
        log.info("UPDATED  %s  (id=%s)  fields=%s", sender.email, contact_id, list(updates))
        return SyncResult(email=sender.email, status="updated", contact_id=contact_id,
                          detail=f"filled: {', '.join(updates)}")

    except ApiException as exc:
        log.error("HubSpot API error for %s: %s", sender.email, exc)
        return SyncResult(email=sender.email, status="error", detail=str(exc))
    except Exception as exc:
        log.error("Unexpected error for %s: %s", sender.email, exc)
        return SyncResult(email=sender.email, status="error", detail=str(exc))


# ── Main ───────────────────────────────────────────────────────────────────────

def run_once(days_back: int = 1) -> list[SyncResult]:
    log.info("=== Gmail → HubSpot sync  (last %d day(s)) ===", days_back)
    senders = fetch_inbound_senders(days_back=days_back)
    client = _hs_client()
    results: list[SyncResult] = []

    for sender in senders:
        result = sync_to_hubspot(sender, client)
        results.append(result)

    # ── Summary ────────────────────────────────────────────────────────────
    created  = [r for r in results if r.status == "created"]
    updated  = [r for r in results if r.status == "updated"]
    skipped  = [r for r in results if r.status == "skipped"]
    errors   = [r for r in results if r.status == "error"]

    log.info(
        "Done. Created=%d  Updated=%d  Skipped=%d  Errors=%d",
        len(created), len(updated), len(skipped), len(errors),
    )

    print("\n╔══════════════════════════════════════════════════════════════╗")
    print("║              Gmail → HubSpot  Sync  Report                  ║")
    print("╠══════════════╦════════════╦════════════════════════════════╣")
    print(f"║ {'Status':<12} ║ {'HubSpot ID':<10} ║ {'Email':<30} ║")
    print("╠══════════════╬════════════╬════════════════════════════════╣")
    for r in sorted(results, key=lambda x: x.status):
        cid = r.contact_id or "-"
        print(f"║ {r.status.upper():<12} ║ {cid:<10} ║ {r.email[:30]:<30} ║")
    print("╚══════════════╩════════════╩════════════════════════════════╝")
    print(f"\nCreated: {len(created)}  Updated: {len(updated)}  Skipped: {len(skipped)}  Errors: {len(errors)}\n")

    return results


def main():
    parser = argparse.ArgumentParser(description="Sync Gmail inbound senders to HubSpot")
    parser.add_argument("--days", type=int, default=SYNC_DAYS_BACK,
                        help="How many days back to scan (default: %(default)s)")
    parser.add_argument("--watch", action="store_true",
                        help="Run continuously, polling every 5 minutes")
    parser.add_argument("--interval", type=int, default=300,
                        help="Poll interval in seconds when --watch is set (default: 300)")
    args = parser.parse_args()

    if args.watch:
        log.info("Watch mode enabled — polling every %ds", args.interval)
        while True:
            try:
                run_once(days_back=1)  # only last day when polling
            except KeyboardInterrupt:
                log.info("Interrupted. Bye.")
                break
            except Exception as exc:
                log.error("Run failed: %s", exc)
            time.sleep(args.interval)
    else:
        run_once(days_back=args.days)


if __name__ == "__main__":
    main()
