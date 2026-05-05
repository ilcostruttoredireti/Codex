"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail messages and upserts senders as HubSpot contacts.
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from contact_parser import SenderContact, parse_sender

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gmail_hubspot_sync")

# ── Config ────────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
STATE_FILE = os.getenv("STATE_FILE", "sync_state.json")

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
HUBSPOT_BASE_URL = "https://api.hubapi.com"

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class SyncResult:
    status: str          # "created" | "updated" | "ignored"
    email: str
    hubspot_id: Optional[str] = None
    reason: str = ""


# ── State persistence ─────────────────────────────────────────────────────────

class SyncState:
    """Persists the last-processed Gmail history id and processed message ids."""

    def __init__(self, path: str = STATE_FILE):
        self._path = Path(path)
        self._data: dict = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            return json.loads(self._path.read_text())
        return {"history_id": None, "processed_ids": []}

    def _save(self):
        self._path.write_text(json.dumps(self._data, indent=2))

    @property
    def history_id(self) -> Optional[str]:
        return self._data.get("history_id")

    @history_id.setter
    def history_id(self, value: str):
        self._data["history_id"] = value
        self._save()

    def is_processed(self, msg_id: str) -> bool:
        return msg_id in self._data.get("processed_ids", [])

    def mark_processed(self, msg_id: str):
        ids: list = self._data.setdefault("processed_ids", [])
        if msg_id not in ids:
            ids.append(msg_id)
            # Keep only the last 10 000 ids to bound memory
            self._data["processed_ids"] = ids[-10_000:]
            self._save()


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if Path(TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        Path(TOKEN_FILE).write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def fetch_new_messages(service, state: SyncState) -> list[SenderContact]:
    """Use Gmail history API (incremental) or fall back to recent messages."""
    contacts: list[SenderContact] = []

    if state.history_id:
        # Incremental: only messages since last run
        try:
            resp = (
                service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=state.history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
            histories = resp.get("history", [])
            new_history_id = resp.get("historyId", state.history_id)
            state.history_id = new_history_id

            msg_ids = []
            for h in histories:
                for ma in h.get("messagesAdded", []):
                    msg_ids.append(ma["message"]["id"])

        except Exception as exc:
            log.warning("History fetch failed (%s); falling back to list.", exc)
            msg_ids = _list_recent_msg_ids(service)
    else:
        # First run: seed with the 20 most-recent inbox messages
        msg_ids = _list_recent_msg_ids(service, count=20)
        # Capture current historyId so next run is incremental
        profile = service.users().getProfile(userId="me").execute()
        state.history_id = profile.get("historyId", "")

    for msg_id in msg_ids:
        if state.is_processed(msg_id):
            continue
        sender = _fetch_message_sender(service, msg_id)
        if sender:
            contacts.append(sender)
        state.mark_processed(msg_id)

    return contacts


def _list_recent_msg_ids(service, count: int = 20) -> list[str]:
    resp = (
        service.users()
        .messages()
        .list(userId="me", labelIds=["INBOX"], maxResults=count)
        .execute()
    )
    return [m["id"] for m in resp.get("messages", [])]


def _fetch_message_sender(service, msg_id: str) -> Optional[SenderContact]:
    try:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=msg_id, format="metadata",
                 metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )
        headers = msg.get("payload", {}).get("headers", [])
        from_val = _header(headers, "From")
        subject_val = _header(headers, "Subject")
        date_val = _header(headers, "Date")
        return parse_sender(from_val, msg_id=msg_id,
                            subject=subject_val, received_at=date_val)
    except Exception as exc:
        log.error("Could not fetch message %s: %s", msg_id, exc)
        return None


# ── HubSpot helpers ───────────────────────────────────────────────────────────

class HubSpotClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("HUBSPOT_API_KEY environment variable is required.")
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict = None) -> dict:
        resp = requests.get(
            f"{HUBSPOT_BASE_URL}{path}",
            headers=self._headers,
            params=params or {},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, payload: dict) -> dict:
        resp = requests.post(
            f"{HUBSPOT_BASE_URL}{path}",
            headers=self._headers,
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path: str, payload: dict) -> dict:
        resp = requests.patch(
            f"{HUBSPOT_BASE_URL}{path}",
            headers=self._headers,
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    # ── Contact lookup ────────────────────────────────────────────────────────

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return existing contact dict or None."""
        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email}
                    ]
                }
            ],
            "properties": [
                "email", "firstname", "lastname", "company",
                "hs_lead_status", "hs_analytics_source",
            ],
            "limit": 1,
        }
        try:
            data = self._post("/crm/v3/objects/contacts/search", payload)
            results = data.get("results", [])
            return results[0] if results else None
        except requests.HTTPError as exc:
            log.error("HubSpot search error: %s", exc)
            return None

    # ── Contact create/update ─────────────────────────────────────────────────

    def create_contact(self, sender: SenderContact) -> dict:
        props = self._build_properties(sender)
        return self._post("/crm/v3/objects/contacts", {"properties": props})

    def update_contact(self, contact_id: str, sender: SenderContact,
                       existing_props: dict) -> dict:
        props = {}
        # Only fill in genuinely missing fields
        if sender.first_name and not existing_props.get("firstname"):
            props["firstname"] = sender.first_name
        if sender.last_name and not existing_props.get("lastname"):
            props["lastname"] = sender.last_name
        if sender.company and not existing_props.get("company"):
            props["company"] = sender.company
        # Always update lead source tag
        existing_source = existing_props.get("hs_analytics_source", "")
        if not existing_source:
            props["hs_analytics_source"] = "OTHER"

        if not props:
            return {}  # nothing to update
        return self._patch(f"/crm/v3/objects/contacts/{contact_id}",
                           {"properties": props})

    def _build_properties(self, sender: SenderContact) -> dict:
        props: dict = {
            "email": sender.email,
            "hs_analytics_source": "OTHER",   # closest standard value to "Gmail"
            "hs_lead_status": "NEW",
        }
        if sender.first_name:
            props["firstname"] = sender.first_name
        if sender.last_name:
            props["lastname"] = sender.last_name
        if sender.company:
            props["company"] = sender.company
        return props

    # ── Timeline activity ─────────────────────────────────────────────────────

    def log_email_activity(self, contact_id: str, sender: SenderContact):
        """Create an Engagement (note) recording the received email."""
        payload = {
            "engagement": {"active": True, "type": "NOTE"},
            "associations": {"contactIds": [int(contact_id)]},
            "metadata": {
                "body": (
                    f"<b>Inbound Gmail</b><br>"
                    f"From: {sender.email}<br>"
                    f"Subject: {sender.subject or '(no subject)'}<br>"
                    f"Date: {sender.received_at}<br>"
                    f"Source: Gmail"
                )
            },
        }
        try:
            self._post("/engagements/v1/engagements", payload)
        except requests.HTTPError as exc:
            log.warning("Could not create timeline engagement: %s", exc)


# ── Sync orchestration ────────────────────────────────────────────────────────

def process_sender(hs: HubSpotClient, sender: SenderContact) -> SyncResult:
    existing = hs.find_contact_by_email(sender.email)

    if existing is None:
        try:
            created = hs.create_contact(sender)
            contact_id = created.get("id", "?")
            hs.log_email_activity(contact_id, sender)
            log.info("[CREATED]  %-40s  id=%s", sender.email, contact_id)
            return SyncResult(status="created", email=sender.email,
                              hubspot_id=contact_id)
        except requests.HTTPError as exc:
            log.error("Create failed for %s: %s", sender.email, exc)
            return SyncResult(status="ignored", email=sender.email,
                              reason=str(exc))
    else:
        contact_id = existing["id"]
        existing_props = existing.get("properties", {})
        try:
            updated = hs.update_contact(contact_id, sender, existing_props)
            if updated:
                hs.log_email_activity(contact_id, sender)
                log.info("[UPDATED]  %-40s  id=%s", sender.email, contact_id)
                return SyncResult(status="updated", email=sender.email,
                                  hubspot_id=contact_id)
            else:
                log.info("[IGNORED]  %-40s  id=%s  (no new data)", sender.email, contact_id)
                return SyncResult(status="ignored", email=sender.email,
                                  hubspot_id=contact_id, reason="no new data")
        except requests.HTTPError as exc:
            log.error("Update failed for %s: %s", sender.email, exc)
            return SyncResult(status="ignored", email=sender.email,
                              hubspot_id=contact_id, reason=str(exc))


def run_sync_cycle(gmail_service, hs: HubSpotClient, state: SyncState) -> list[SyncResult]:
    log.info("=== Sync cycle started ===")
    senders = fetch_new_messages(gmail_service, state)
    log.info("Found %d new sender(s) to process.", len(senders))

    results = []
    for sender in senders:
        result = process_sender(hs, sender)
        results.append(result)

    _print_summary(results)
    return results


def _print_summary(results: list[SyncResult]):
    if not results:
        log.info("No new contacts to process.")
        return
    print("\n" + "─" * 65)
    print(f"{'STATUS':<12} {'EMAIL':<38} {'HUBSPOT ID'}")
    print("─" * 65)
    for r in results:
        print(f"{r.status.upper():<12} {r.email:<38} {r.hubspot_id or '—'}")
    print("─" * 65 + "\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    log.info("Starting Gmail → HubSpot sync  (interval: %ds)", POLL_INTERVAL_SECONDS)

    try:
        gmail_service = get_gmail_service()
    except FileNotFoundError:
        log.error(
            "credentials.json not found. Download it from Google Cloud Console "
            "and place it in the project root (or set GMAIL_CREDENTIALS_FILE)."
        )
        return

    hs = HubSpotClient(HUBSPOT_API_KEY)
    state = SyncState(STATE_FILE)

    while True:
        try:
            run_sync_cycle(gmail_service, hs, state)
        except Exception as exc:
            log.error("Unexpected error in sync cycle: %s", exc, exc_info=True)
        log.info("Sleeping %d seconds until next cycle …", POLL_INTERVAL_SECONDS)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
