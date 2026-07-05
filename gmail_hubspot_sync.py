"""
Gmail → HubSpot Contact Sync
=============================
Monitors Gmail inbox and syncs sender contacts to HubSpot CRM.
- Skips no-reply / automated senders
- Deduplicates by email (uses email as unique key)
- Creates new contacts or updates existing ones with missing fields
- Tags contacts with source = "Gmail"
"""

import os
import re
import base64
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest

# ── Configuration ─────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_TOKEN_FILE = "gmail_token.json"
GMAIL_CREDENTIALS_FILE = "gmail_credentials.json"

HUBSPOT_API_KEY = os.environ["HUBSPOT_ACCESS_TOKEN"]

# How far back to look for new emails (days)
LOOKBACK_DAYS = 1

# Patterns that identify automated / no-reply senders to skip
SKIP_PATTERNS = re.compile(
    r"(no[-_]?reply|noreply|nobody|donotreply|notifications?|"
    r"automailer|bounce|daemon|postmaster|mailer-daemon|"
    r"@amazonses\.com|@bounce\.|@notification\.)",
    re.IGNORECASE,
)

# Prefixes in the local part of the address that signal automated senders
SKIP_LOCALPARTS = {
    "payments-update", "conferma-ordine", "order-update", "order-confirm",
    "admanager-noreply", "notify-noreply", "sc-noreply", "googlebase-noreply",
    "noreply", "no-reply", "nobody", "bounce",
}

CONTACT_SOURCE = "Inbound Gmail"
# HubSpot analytics/source properties (hs_analytics_source*, hs_lead_source) are read-only.
# We record the Gmail source as a NOTE engagement linked to each new contact instead.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class Sender:
    email: str
    name: str = ""
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    domain: str = field(init=False)

    def __post_init__(self):
        self.email = self.email.lower().strip()
        self.domain = self.email.split("@")[-1] if "@" in self.email else ""
        if self.name and not self.firstname:
            parts = self.name.strip().split(" ", 1)
            self.firstname = parts[0]
            self.lastname = parts[1] if len(parts) > 1 else ""
        if not self.company and self.domain:
            # Derive company name from domain (strip TLD, capitalise)
            base = self.domain.split(".")[0]
            self.company = base.replace("-", " ").replace("_", " ").title()


@dataclass
class SyncResult:
    email: str
    status: str   # "Creato" | "Aggiornato" | "Ignorato"
    contact_id: Optional[str] = None
    reason: str = ""


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def _gmail_service():
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
        with open(GMAIL_TOKEN_FILE, "w") as fh:
            fh.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _parse_sender_header(raw: str) -> tuple[str, str]:
    """Parse 'Display Name <email@domain.com>' or bare 'email@domain.com'."""
    m = re.match(r'^"?([^"<]+?)"?\s*<([^>]+)>$', raw.strip())
    if m:
        return m.group(2).lower().strip(), m.group(1).strip()
    return raw.lower().strip(), ""


def _is_automated(email: str) -> bool:
    local = email.split("@")[0].lower()
    if SKIP_PATTERNS.search(email):
        return True
    if local in SKIP_LOCALPARTS:
        return True
    return False


def fetch_inbox_senders(lookback_days: int = LOOKBACK_DAYS) -> list[Sender]:
    """Return deduplicated real senders from recent inbox messages."""
    service = _gmail_service()
    query = f"in:inbox newer_than:{lookback_days}d -in:draft"
    seen: dict[str, Sender] = {}

    page_token = None
    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token

        resp = service.users().messages().list(**kwargs).execute()
        messages = resp.get("messages", [])

        for msg_stub in messages:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=msg_stub["id"], format="metadata",
                     metadataHeaders=["From"])
                .execute()
            )
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            raw_from = headers.get("From", "")
            if not raw_from:
                continue

            email, name = _parse_sender_header(raw_from)
            if not email or _is_automated(email):
                continue
            if email not in seen:
                seen[email] = Sender(email=email, name=name)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    log.info("Found %d unique real senders in inbox", len(seen))
    return list(seen.values())


# ── HubSpot helpers ───────────────────────────────────────────────────────────

def _hs_client():
    return hubspot.Client.create(access_token=HUBSPOT_API_KEY)


def _search_contact_by_email(client, email: str) -> Optional[dict]:
    """Return the first matching HubSpot contact dict or None."""
    f = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[f])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company", CONTACT_TAG_PROPERTY],
        limit=1,
    )
    resp = client.crm.contacts.search_api.do_search(public_object_search_request=req)
    if resp.results:
        return resp.results[0]
    return None


def _build_properties(sender: Sender, existing: Optional[dict] = None) -> dict:
    """Build HubSpot property dict, only filling missing values."""
    existing_props = existing.properties if existing else {}
    props = {}

    def _missing(key):
        return not existing_props.get(key)

    if _missing("email"):
        props["email"] = sender.email
    if _missing("firstname") and sender.firstname:
        props["firstname"] = sender.firstname
    if _missing("lastname") and sender.lastname:
        props["lastname"] = sender.lastname
    if _missing("company") and sender.company:
        props["company"] = sender.company

    return props


def _add_source_note(client, contact_id: str) -> None:
    """Create a HubSpot NOTE engagement tagged with the Gmail source."""
    from hubspot.crm.objects import SimplePublicObjectInputForCreate as ObjInput
    note_props = {
        "hs_note_body": f"Fonte contatto: {CONTACT_SOURCE}",
        "hs_timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
    }
    note = client.crm.objects.basic_api.create(
        object_type="notes",
        simple_public_object_input_for_create=ObjInput(properties=note_props),
    )
    client.crm.objects.associations_api.create(
        object_type="notes",
        object_id=note.id,
        to_object_type="contacts",
        to_object_id=contact_id,
        association_type="note_to_contact",
    )


def sync_sender_to_hubspot(client, sender: Sender) -> SyncResult:
    """Create or update a HubSpot contact for this sender."""
    try:
        existing = _search_contact_by_email(client, sender.email)

        if existing:
            props = _build_properties(sender, existing)
            if props:
                client.crm.contacts.basic_api.update(
                    contact_id=existing.id,
                    simple_public_object_input={"properties": props},
                )
                return SyncResult(
                    email=sender.email,
                    status="Aggiornato",
                    contact_id=existing.id,
                    reason=f"Campi aggiornati: {list(props.keys())}",
                )
            return SyncResult(
                email=sender.email,
                status="Ignorato",
                contact_id=existing.id,
                reason="Nessun campo mancante",
            )

        # Create new contact
        props = _build_properties(sender)
        props.setdefault("email", sender.email)
        new_contact = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        # Record Gmail source as a linked note (source properties are read-only in HubSpot)
        try:
            _add_source_note(client, new_contact.id)
        except Exception as note_exc:
            log.warning("Could not add source note for %s: %s", sender.email, note_exc)
        return SyncResult(
            email=sender.email,
            status="Creato",
            contact_id=new_contact.id,
            reason="Nuovo contatto Gmail",
        )

    except ApiException as exc:
        log.error("HubSpot API error for %s: %s", sender.email, exc)
        return SyncResult(
            email=sender.email,
            status="Errore",
            reason=str(exc),
        )


# ── Main ──────────────────────────────────────────────────────────────────────

def run_sync(lookback_days: int = LOOKBACK_DAYS) -> list[SyncResult]:
    log.info("=== Gmail → HubSpot sync started (lookback=%dd) ===", lookback_days)
    senders = fetch_inbox_senders(lookback_days)
    client = _hs_client()
    results: list[SyncResult] = []

    for sender in senders:
        result = sync_sender_to_hubspot(client, sender)
        results.append(result)
        log.info(
            "[%s] %s  id=%s  %s",
            result.status,
            result.email,
            result.contact_id or "-",
            result.reason,
        )

    created = sum(1 for r in results if r.status == "Creato")
    updated = sum(1 for r in results if r.status == "Aggiornato")
    skipped = sum(1 for r in results if r.status == "Ignorato")
    errors = sum(1 for r in results if r.status == "Errore")

    log.info(
        "=== Sync completato: %d creati, %d aggiornati, %d ignorati, %d errori ===",
        created, updated, skipped, errors,
    )
    return results


if __name__ == "__main__":
    run_sync()
