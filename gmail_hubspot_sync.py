"""
Gmail → HubSpot Contact Sync
Monitors the Gmail inbox and upserts each sender as a HubSpot contact.

Outputs per processed email:
  Status : Created | Updated | Ignored
  Email  : sender email address
  ID     : HubSpot contact ID
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import hubspot
from hubspot.crm.contacts import ApiException
from hubspot.crm.contacts.models import (
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.objects.notes import (
    SimplePublicObjectInputForCreate as NoteInput,
)

load_dotenv()

# ─── Configuration ─────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials/gmail_credentials.json")
TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "credentials/gmail_token.json")
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
CREATE_TIMELINE = os.getenv("CREATE_TIMELINE_ACTIVITY", "true").lower() == "true"
ADD_TAG = os.getenv("ADD_INBOUND_TAG", "true").lower() == "true"
IGNORED_DOMAINS: set[str] = {
    d.strip()
    for d in os.getenv(
        "IGNORED_DOMAINS",
        "gmail.com,yahoo.com,hotmail.com,outlook.com,icloud.com,aol.com,live.com,protonmail.com",
    ).split(",")
    if d.strip()
}

STATE_FILE = "sync_state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ─── Data models ───────────────────────────────────────────────────────────────


@dataclass
class SenderInfo:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""

    @property
    def domain(self) -> str:
        return self.email.split("@")[-1].lower() if "@" in self.email else ""


@dataclass
class SyncResult:
    status: str          # "Created" | "Updated" | "Ignored"
    email: str
    contact_id: str = ""

    def display(self) -> None:
        icon = {"Created": "✚", "Updated": "↺", "Ignored": "–"}.get(self.status, " ")
        log.info("  %s  Status=%-8s  Email=%-40s  ID=%s", icon, self.status, self.email, self.contact_id)


# ─── State persistence ─────────────────────────────────────────────────────────


def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"history_id": None, "processed": []}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# ─── Gmail helpers ─────────────────────────────────────────────────────────────


def build_gmail_service():
    creds = None
    token_path = Path(TOKEN_FILE)
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(CREDENTIALS_FILE).exists():
                raise FileNotFoundError(
                    f"Gmail credentials not found at '{CREDENTIALS_FILE}'. "
                    "Download them from Google Cloud Console and set GMAIL_CREDENTIALS_FILE."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)

        token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_new_message_ids(service, state: dict) -> tuple[list[str], str | None]:
    """Return (message_ids, new_history_id). Uses History API when possible."""
    history_id = state.get("history_id")

    if history_id:
        try:
            resp = (
                service.users()
                .history()
                .list(userId="me", startHistoryId=history_id, historyTypes=["messageAdded"])
                .execute()
            )
            new_history_id = resp.get("historyId")
            ids: list[str] = []
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg = added.get("message", {})
                    labels = msg.get("labelIds", [])
                    if "INBOX" in labels:
                        ids.append(msg["id"])
            return ids, new_history_id
        except HttpError as exc:
            if exc.resp.status == 404:
                log.warning("History ID expired; falling back to full inbox scan.")
            else:
                raise

    # First run or expired history: list recent unread inbox messages
    resp = (
        service.users()
        .messages()
        .list(userId="me", labelIds=["INBOX", "UNREAD"], maxResults=100)
        .execute()
    )
    ids = [m["id"] for m in resp.get("messages", [])]

    # Fetch the current profile to seed historyId for next poll
    profile = service.users().getProfile(userId="me").execute()
    return ids, profile.get("historyId")


def extract_sender(service, message_id: str) -> SenderInfo | None:
    """Fetch a message and parse the From header into a SenderInfo."""
    try:
        msg = service.users().messages().get(userId="me", id=message_id, format="metadata",
                                              metadataHeaders=["From", "Date"]).execute()
    except HttpError:
        return None

    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    raw_from = headers.get("From", "")
    display_name, addr = parseaddr(raw_from)
    addr = addr.lower().strip()

    if not addr or "@" not in addr:
        return None

    first, last = _split_display_name(display_name)
    company = _company_from_domain(addr.split("@")[1])
    return SenderInfo(email=addr, first_name=first, last_name=last, company=company)


def _split_display_name(name: str) -> tuple[str, str]:
    name = name.strip().strip('"')
    if not name:
        return "", ""
    parts = name.split(None, 1)
    return parts[0], parts[1] if len(parts) > 1 else ""


def _company_from_domain(domain: str) -> str:
    if domain in IGNORED_DOMAINS:
        return ""
    # Strip common TLD suffixes and capitalise
    base = domain.split(".")[0]
    return base.capitalize() if base else ""


# ─── HubSpot helpers ───────────────────────────────────────────────────────────


def build_hubspot_client() -> hubspot.Client:
    if not HUBSPOT_ACCESS_TOKEN:
        raise ValueError("HUBSPOT_ACCESS_TOKEN is not set. Add it to your .env file.")
    return hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)


def find_contact_by_email(hs: hubspot.Client, email: str) -> dict | None:
    """Return the existing HubSpot contact dict or None."""
    search_req = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(filters=[Filter(property_name="email", operator="EQ", value=email)])
        ],
        properties=["email", "firstname", "lastname", "company", "leadsource"],
        limit=1,
    )
    try:
        resp = hs.crm.contacts.search_api.do_search(public_object_search_request=search_req)
        return resp.results[0].to_dict() if resp.results else None
    except ApiException as exc:
        log.error("HubSpot search failed: %s", exc)
        return None


def _build_properties(sender: SenderInfo, existing: dict | None) -> dict:
    """Merge sender data with existing contact properties; fill blanks only."""
    existing_props = (existing or {}).get("properties", {})

    props: dict = {"email": sender.email, "leadsource": "Gmail"}

    if sender.first_name and not existing_props.get("firstname"):
        props["firstname"] = sender.first_name
    if sender.last_name and not existing_props.get("lastname"):
        props["lastname"] = sender.last_name
    if sender.company and not existing_props.get("company"):
        props["company"] = sender.company
    if ADD_TAG:
        props["hs_analytics_source"] = "OTHER"  # closest built-in source
        # Store the inbound tag in a standard notes field visible on the record
        # (a dedicated custom property "inbound_source" can be created in HubSpot)

    return props


def upsert_contact(hs: hubspot.Client, sender: SenderInfo) -> SyncResult:
    existing = find_contact_by_email(hs, sender.email)
    props = _build_properties(sender, existing)

    try:
        if existing:
            contact_id = existing["id"]
            hs.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input={"properties": props},
            )
            status = "Updated"
        else:
            created = hs.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            contact_id = created.id
            status = "Created"
    except ApiException as exc:
        log.error("HubSpot upsert failed for %s: %s", sender.email, exc)
        return SyncResult(status="Ignored", email=sender.email)

    if CREATE_TIMELINE:
        _add_timeline_note(hs, contact_id, sender)

    return SyncResult(status=status, email=sender.email, contact_id=contact_id)


def _add_timeline_note(hs: hubspot.Client, contact_id: str, sender: SenderInfo) -> None:
    """Create a HubSpot Note engagement and associate it with the contact."""
    timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    tag_line = "  Tag: Inbound Gmail" if ADD_TAG else ""
    note_body = (
        f"Inbound email received from {sender.email}.\n"
        f"Source: Gmail{tag_line}\n"
        f"Synced: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    try:
        note = hs.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=NoteInput(
                properties={
                    "hs_timestamp": str(timestamp_ms),
                    "hs_note_body": note_body,
                }
            )
        )
        hs.crm.objects.notes.associations_api.create(
            note_id=note.id,
            to_object_type="contacts",
            to_object_id=contact_id,
            association_type="note_to_contact",
        )
    except ApiException as exc:
        log.warning("Could not create timeline note for contact %s: %s", contact_id, exc)


# ─── Main sync loop ────────────────────────────────────────────────────────────


def sync_once(gmail, hs: hubspot.Client, state: dict) -> dict:
    message_ids, new_history_id = get_new_message_ids(gmail, state)
    processed: list[str] = state.get("processed", [])
    processed_set = set(processed)

    new_ids = [mid for mid in message_ids if mid not in processed_set]
    if not new_ids:
        log.info("No new messages to process.")
    else:
        log.info("Processing %d new message(s)…", len(new_ids))

    for mid in new_ids:
        sender = extract_sender(gmail, mid)
        if sender is None:
            log.debug("Skipped message %s (no valid sender).", mid)
            processed.append(mid)
            continue

        if sender.domain in IGNORED_DOMAINS:
            result = SyncResult(status="Ignored", email=sender.email)
        else:
            result = upsert_contact(hs, sender)

        result.display()
        processed.append(mid)

    # Keep only the last 5 000 IDs to bound memory
    state["processed"] = processed[-5000:]
    if new_history_id:
        state["history_id"] = new_history_id
    return state


def main() -> None:
    log.info("=== Gmail → HubSpot Sync starting ===")
    log.info("Poll interval: %ds | Timeline notes: %s | Inbound tag: %s",
             POLL_INTERVAL, CREATE_TIMELINE, ADD_TAG)

    gmail = build_gmail_service()
    hs = build_hubspot_client()
    state = load_state()

    log.info("Services authenticated. Watching inbox…\n")

    while True:
        try:
            state = sync_once(gmail, hs, state)
            save_state(state)
        except HttpError as exc:
            log.error("Gmail API error: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("Unexpected error: %s", exc)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
