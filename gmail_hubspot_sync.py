"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox for inbound emails and syncs senders as HubSpot contacts.
Run directly or schedule via cron (e.g. every 15 minutes).
"""

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Google / Gmail ────────────────────────────────────────────────────────────
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── HubSpot ───────────────────────────────────────────────────────────────────
import hubspot
from hubspot.crm.contacts import (
    ApiException,
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
)
from hubspot.crm.contacts.api import BasicApi, SearchApi
from hubspot.crm.contacts.models import PublicObjectSearchRequest, Filter, FilterGroup

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")

HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")

# Emails belonging to our own account — never sync these as contacts
OWN_EMAILS: set[str] = set(
    e.strip().lower()
    for e in os.getenv("OWN_EMAILS", "").split(",")
    if e.strip()
)

# Domains we never want to capture (mailers, notifications, …)
SKIP_DOMAINS = {
    "googlemail.com",
    "mailer-daemon",
    "noreply",
    "no-reply",
    "bounce",
    "notifications",
    "donotreply",
    "google.com",  # analytics/system emails from Google
}

STATE_FILE = Path(os.getenv("SYNC_STATE_FILE", ".sync_state.json"))

# Number of threads to fetch per run
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "50"))


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SenderInfo:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    domain: str = ""
    thread_id: str = ""
    received_at: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


@dataclass
class SyncResult:
    email: str
    contact_id: str
    status: str  # "created" | "updated" | "skipped"
    reason: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# State persistence  (tracks processed thread IDs across runs)
# ─────────────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_threads": []}


def save_state(state: dict) -> None:
    # Keep only the last 10 000 thread IDs to bound file size
    state["processed_threads"] = state["processed_threads"][-10_000:]
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ─────────────────────────────────────────────────────────────────────────────
# Gmail helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_gmail_service():
    creds: Optional[Credentials] = None

    if Path(GMAIL_TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        Path(GMAIL_TOKEN_FILE).write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _parse_name_from_header(raw: str) -> tuple[str, str]:
    """
    Parse 'First Last <email@domain.com>' → (first_name, last_name).
    Falls back to empty strings when the display name is absent.
    """
    match = re.match(r'"?([^"<]+)"?\s*<[^>]+>', raw.strip())
    if not match:
        return "", ""
    parts = match.group(1).strip().split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:]) if len(parts) > 1 else ""


def _extract_email(raw: str) -> str:
    """Extract bare email address from 'Name <email>' or plain 'email'."""
    m = re.search(r"<([^>]+)>", raw)
    return (m.group(1) if m else raw).strip().lower()


def _company_from_domain(domain: str) -> str:
    """Turn 'samandra.it' → 'Samandra'."""
    name = domain.split(".")[0]
    return name.capitalize() if name else ""


def fetch_inbound_senders(
    service,
    own_emails: set[str],
    processed_threads: set[str],
    batch_size: int = 50,
) -> list[SenderInfo]:
    """
    Fetch inbox threads and return one SenderInfo per unique inbound sender.
    Skips threads we already processed and messages sent by own account.
    """
    results = (
        service.users()
        .threads()
        .list(userId="me", labelIds=["INBOX"], maxResults=batch_size)
        .execute()
    )
    threads = results.get("threads", [])

    senders: dict[str, SenderInfo] = {}  # deduplicated by email

    for thread_meta in threads:
        tid = thread_meta["id"]
        if tid in processed_threads:
            continue

        thread = (
            service.users()
            .threads()
            .get(userId="me", id=tid, format="metadata",
                 metadataHeaders=["From", "Date"])
            .execute()
        )

        for msg in thread.get("messages", []):
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            raw_from = headers.get("From", "")
            if not raw_from:
                continue

            email = _extract_email(raw_from)
            domain = email.split("@")[-1] if "@" in email else ""

            # Skip own account and noisy system domains
            if email in own_emails:
                continue
            if any(skip in domain for skip in SKIP_DOMAINS):
                continue

            if email in senders:
                continue  # already captured this sender

            first, last = _parse_name_from_header(raw_from)
            company = _company_from_domain(domain)
            date_str = headers.get("Date", "")

            senders[email] = SenderInfo(
                email=email,
                first_name=first,
                last_name=last,
                company=company,
                domain=domain,
                thread_id=tid,
                received_at=date_str,
            )

        processed_threads.add(tid)

    return list(senders.values())


# ─────────────────────────────────────────────────────────────────────────────
# HubSpot helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_hubspot_client() -> hubspot.Client:
    return hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)


def find_contact_by_email(client: hubspot.Client, email: str) -> Optional[dict]:
    """Return existing HubSpot contact dict or None."""
    search_request = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(
                filters=[
                    Filter(
                        property_name="email",
                        operator="EQ",
                        value=email,
                    )
                ]
            )
        ],
        properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
        limit=1,
    )
    try:
        resp = client.crm.contacts.search_api.do_search(
            public_object_search_request=search_request
        )
        return resp.results[0].to_dict() if resp.results else None
    except ApiException:
        return None


def _build_properties(sender: SenderInfo, existing: Optional[dict] = None) -> dict:
    """
    Build the property dict for create/update.
    For updates, only include fields that are currently empty in HubSpot.
    """
    ex_props = existing["properties"] if existing else {}

    props: dict[str, str] = {"hs_lead_source": "Gmail"}

    if not ex_props.get("email"):
        props["email"] = sender.email

    if sender.first_name and not ex_props.get("firstname"):
        props["firstname"] = sender.first_name

    if sender.last_name and not ex_props.get("lastname"):
        props["lastname"] = sender.last_name

    if sender.company and not ex_props.get("company"):
        props["company"] = sender.company

    # custom tag stored as a note — HubSpot free tier has no tag property
    # so we embed it in the "hs_lead_status" or a custom property if available
    # For now we rely on hs_lead_source = "Gmail" as the signal.

    return props


def create_or_update_contact(
    client: hubspot.Client, sender: SenderInfo
) -> SyncResult:
    existing = find_contact_by_email(client, sender.email)

    if existing:
        contact_id = existing["id"]
        update_props = _build_properties(sender, existing)
        # Remove email from updates (can't change the key field)
        update_props.pop("email", None)

        if len(update_props) <= 1:  # only hs_lead_source, nothing new to fill
            return SyncResult(
                email=sender.email,
                contact_id=contact_id,
                status="skipped",
                reason="no new fields to fill",
            )

        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=update_props),
        )
        return SyncResult(email=sender.email, contact_id=contact_id, status="updated")

    # Create new contact
    create_props = _build_properties(sender)
    new_contact = client.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
            properties=create_props
        )
    )
    return SyncResult(
        email=sender.email,
        contact_id=new_contact.id,
        status="created",
    )


def add_inbound_note(
    client: hubspot.Client, contact_id: str, sender: SenderInfo
) -> None:
    """Attach a timeline note so the email receipt is recorded on the contact."""
    body = (
        f"Inbound Gmail · {sender.received_at}\n"
        f"Thread: {sender.thread_id}\n"
        f"Tags: Inbound Gmail"
    )
    client.crm.objects.notes.basic_api.create(
        simple_public_object_input_for_create=hubspot.crm.objects.notes.SimplePublicObjectInputForCreate(
            properties={
                "hs_note_body": body,
                "hs_timestamp": str(int(time.time() * 1000)),
            },
            associations=[
                {
                    "to": {"id": contact_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 202,  # note → contact
                        }
                    ],
                }
            ],
        )
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main sync loop
# ─────────────────────────────────────────────────────────────────────────────

def run_sync(add_notes: bool = True) -> list[SyncResult]:
    state = load_state()
    processed = set(state["processed_threads"])

    gmail = get_gmail_service()
    hs = get_hubspot_client()

    # Determine own email(s) if not set via env
    own = OWN_EMAILS.copy()
    if not own:
        profile = gmail.users().getProfile(userId="me").execute()
        own.add(profile["emailAddress"].lower())

    print(f"[{_now()}] Fetching inbox threads …")
    senders = fetch_inbound_senders(gmail, own, processed, BATCH_SIZE)
    print(f"[{_now()}] Found {len(senders)} unique inbound sender(s) to process.")

    results: list[SyncResult] = []
    for sender in senders:
        try:
            result = create_or_update_contact(hs, sender)
            results.append(result)

            if add_notes and result.status in ("created", "updated"):
                try:
                    add_inbound_note(hs, result.contact_id, sender)
                except Exception:
                    pass  # non-critical

            _print_result(result)
        except ApiException as exc:
            results.append(
                SyncResult(
                    email=sender.email,
                    contact_id="",
                    status="error",
                    reason=str(exc),
                )
            )

    state["processed_threads"] = list(processed)
    save_state(state)

    print(f"\n[{_now()}] Sync complete — {len(results)} contact(s) processed.")
    return results


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _print_result(r: SyncResult) -> None:
    icon = {"created": "✚", "updated": "↻", "skipped": "–", "error": "✗"}.get(
        r.status, "?"
    )
    print(f"  {icon} [{r.status.upper():8s}] {r.email:<40s}  id={r.contact_id}")


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run_sync(add_notes=True)
