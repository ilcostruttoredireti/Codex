#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Continuously polls Gmail INBOX via the History API and upserts each sender
as a HubSpot contact.  Uses the email address as the dedup key.

Setup:
  1. pip install -r requirements.txt
  2. Copy .env.example → .env and fill in credentials.
  3. Download OAuth2 credentials.json from Google Cloud Console
     (APIs & Services → Credentials → Desktop app).
  4. python gmail_hubspot_sync.py
     (A browser tab will open on first run for Gmail authorisation.)

Output per processed email:
  [CREATED ] email=alice@acme.com       id=123   reason=
  [UPDATED ] email=bob@corp.io          id=456   reason=fields updated
  [IGNORED ] email=noreply@github.com   id=-     reason=Automated sender
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
STATE_FILE = ".sync_state.json"

# Free/personal email domains – company name is not derived from these
_PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it",
    "hotmail.com", "hotmail.it", "outlook.com", "outlook.it",
    "icloud.com", "me.com", "aol.com", "live.com", "live.it", "msn.com",
    "protonmail.com", "pm.me", "fastmail.com", "zohomail.com",
    "libero.it", "tin.it", "alice.it", "virgilio.it", "tiscali.it",
    "email.it", "inwind.it", "iol.it",
}

# Local-part prefixes that indicate automated senders
_AUTOMATED_PREFIXES = (
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "bounces",
    "notifications", "notification", "alert", "alerts",
    "automated", "auto-confirm", "system",
)

# Fully-automated sender domains
_AUTOMATED_DOMAINS = {
    "noreply.github.com",
    "notifications.github.com",
    "mailer.google.com",
    "accounts.google.com",
    "bounce.gmail.com",
}


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class SenderInfo:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    domain: str = ""


@dataclass
class SyncResult:
    status: str           # "created" | "updated" | "ignored"
    email: str
    contact_id: Optional[str] = None
    reason: str = ""


# ── Persistent state ─────────────────────────────────────────────────────────

class StateManager:
    """
    Persists two pieces of state between runs:
      - history_id  : last Gmail historyId consumed
      - processed   : set of Gmail message IDs already handled
    """

    def __init__(self, path: str = STATE_FILE) -> None:
        self._path = path
        self._data: Dict[str, Any] = {}
        if os.path.exists(path):
            with open(path) as fh:
                self._data = json.load(fh)
        self._processed: set = set(self._data.get("processed", []))

    def _persist(self) -> None:
        # Cap the processed log to 10 000 most-recent entries to avoid unbounded growth
        trimmed = list(self._processed)[-10_000:]
        self._data["processed"] = trimmed
        with open(self._path, "w") as fh:
            json.dump(self._data, fh, indent=2)

    # ── history_id property ──────────────────────────────────────────────────

    @property
    def history_id(self) -> Optional[str]:
        return self._data.get("history_id")

    @history_id.setter
    def history_id(self, value: str) -> None:
        self._data["history_id"] = value
        self._persist()

    # ── processed set ────────────────────────────────────────────────────────

    def is_processed(self, msg_id: str) -> bool:
        return msg_id in self._processed

    def mark_processed(self, msg_id: str) -> None:
        self._processed.add(msg_id)
        self._persist()


# ── Parsing helpers ───────────────────────────────────────────────────────────

def parse_sender(from_header: str) -> SenderInfo:
    """Parse a raw From header into a SenderInfo."""
    display_name, email_addr = parseaddr(from_header)
    email_addr = email_addr.strip().lower()
    if not email_addr or "@" not in email_addr:
        return SenderInfo(email="")

    domain = email_addr.split("@")[1]

    first_name = last_name = ""
    if display_name:
        display_name = display_name.strip("'\"")
        parts = display_name.split(maxsplit=1)
        first_name = parts[0] if parts else ""
        last_name = parts[1] if len(parts) > 1 else ""

    company = "" if domain in _PERSONAL_DOMAINS else domain.split(".")[0].capitalize()

    return SenderInfo(
        email=email_addr,
        first_name=first_name,
        last_name=last_name,
        domain=domain,
        company=company,
    )


def is_automated(sender: SenderInfo) -> bool:
    """Return True if this looks like a no-reply / automated address."""
    if not sender.email:
        return True
    local = sender.email.split("@")[0]
    if any(local.startswith(p) for p in _AUTOMATED_PREFIXES):
        return True
    return sender.domain in _AUTOMATED_DOMAINS


# ── Gmail client ──────────────────────────────────────────────────────────────

class GmailClient:
    def __init__(self, credentials_file: str, token_file: str = "token.json") -> None:
        self._service = self._build(credentials_file, token_file)

    def _build(self, creds_file: str, token_file: str):
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
        return build("gmail", "v1", credentials=creds)

    def current_history_id(self) -> str:
        return str(self._service.users().getProfile(userId="me").execute()["historyId"])

    def get_history(
        self, start_history_id: str
    ) -> Tuple[List[Tuple[str, List[str]]], str]:
        """
        Fetch all INBOX messageAdded history since start_history_id.

        Returns:
            ([(msg_id, label_ids), ...], new_history_id)
        """
        messages: List[Tuple[str, List[str]]] = []
        new_hid = start_history_id
        page_token: Optional[str] = None

        while True:
            kwargs: Dict[str, Any] = dict(
                userId="me",
                startHistoryId=start_history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            )
            if page_token:
                kwargs["pageToken"] = page_token

            resp = self._service.users().history().list(**kwargs).execute()
            new_hid = str(resp.get("historyId", new_hid))

            for record in resp.get("history", []):
                for item in record.get("messagesAdded", []):
                    msg = item["message"]
                    messages.append((msg["id"], msg.get("labelIds", [])))

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return messages, new_hid

    def fetch_message(self, msg_id: str) -> Dict[str, Any]:
        return self._service.users().messages().get(
            userId="me",
            id=msg_id,
            format="metadata",
            metadataHeaders=["From", "Subject"],
        ).execute()


# ── HubSpot client ────────────────────────────────────────────────────────────

class HubSpotClient:
    _BASE = "https://api.hubapi.com"

    def __init__(self, access_token: str) -> None:
        self._s = requests.Session()
        self._s.headers.update({"Authorization": f"Bearer {access_token}"})

    def _post(self, path: str, body: Dict) -> requests.Response:
        return self._s.post(f"{self._BASE}{path}", json=body, timeout=15)

    def _patch(self, path: str, body: Dict) -> requests.Response:
        return self._s.patch(f"{self._BASE}{path}", json=body, timeout=15)

    # ── Contact lookup ───────────────────────────────────────────────────────

    def find_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """Search for a contact by email. Returns the raw HubSpot object or None."""
        resp = self._post(
            "/crm/v3/objects/contacts/search",
            {
                "filterGroups": [
                    {
                        "filters": [
                            {"propertyName": "email", "operator": "EQ", "value": email}
                        ]
                    }
                ],
                "properties": ["email", "firstname", "lastname", "company"],
                "limit": 1,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["results"][0] if data["total"] > 0 else None

    # ── Contact create / update ──────────────────────────────────────────────

    def create_contact(self, sender: SenderInfo) -> Optional[str]:
        """Create a new contact. Returns the HubSpot contact ID, or None on failure."""
        props: Dict[str, str] = {"email": sender.email}
        if sender.first_name:
            props["firstname"] = sender.first_name
        if sender.last_name:
            props["lastname"] = sender.last_name
        if sender.company:
            props["company"] = sender.company

        resp = self._post("/crm/v3/objects/contacts", {"properties": props})

        if resp.status_code == 409:
            # Contact already exists – extract the existing ID from the error body
            body = resp.json()
            vid = body.get("existingVid") or body.get("id")
            if not vid:
                m = re.search(r"Existing ID:\s*(\d+)", body.get("message", ""))
                vid = m.group(1) if m else None
            return str(vid) if vid else None

        resp.raise_for_status()
        return resp.json()["id"]

    def update_contact(self, contact_id: str, props: Dict[str, str]) -> bool:
        """PATCH only the supplied properties onto an existing contact."""
        if not props:
            return False
        resp = self._patch(
            f"/crm/v3/objects/contacts/{contact_id}", {"properties": props}
        )
        resp.raise_for_status()
        return True

    # ── Timeline activity ────────────────────────────────────────────────────

    def add_note(self, contact_id: str, note_body: str) -> None:
        """
        Attach a note to the contact's timeline.
        Non-fatal: failures are logged at DEBUG level and swallowed.
        """
        ts = str(int(datetime.now(tz=timezone.utc).timestamp() * 1000))
        payload = {
            "properties": {
                "hs_note_body": note_body,
                "hs_timestamp": ts,
            },
            "associations": [
                {
                    "to": {"id": int(contact_id)},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            # 202 = Note → Contact
                            "associationTypeId": 202,
                        }
                    ],
                }
            ],
        }
        resp = self._post("/crm/v3/objects/notes", payload)
        if not resp.ok:
            log.debug("Note skipped (%s): %s", resp.status_code, resp.text[:200])


# ── Sync engine ───────────────────────────────────────────────────────────────

class SyncEngine:
    def __init__(
        self, gmail: GmailClient, hs: HubSpotClient, state: StateManager
    ) -> None:
        self._gmail = gmail
        self._hs = hs
        self._state = state

    def _bootstrap(self) -> None:
        """On first run, seed the historyId so we only process future emails."""
        if not self._state.history_id:
            hid = self._gmail.current_history_id()
            self._state.history_id = hid
            log.info(
                "First run: seeded historyId=%s (existing emails will not be processed)",
                hid,
            )

    def poll(self) -> List[SyncResult]:
        """Fetch new INBOX messages and upsert their senders into HubSpot."""
        self._bootstrap()
        results: List[SyncResult] = []

        try:
            messages, new_hid = self._gmail.get_history(self._state.history_id)

            for msg_id, label_ids in messages:
                if self._state.is_processed(msg_id):
                    continue
                # History API with labelId=INBOX already filters, but verify in payload too
                if "INBOX" not in label_ids:
                    self._state.mark_processed(msg_id)
                    continue

                result = self._process_message(msg_id)
                results.append(result)
                _log_result(result)
                self._state.mark_processed(msg_id)

            self._state.history_id = new_hid

        except HttpError as exc:
            if exc.resp.status == 404:
                # historyId expired (Gmail keeps history for ~7 days)
                log.warning("Gmail historyId expired – resetting to current position")
                self._state.history_id = self._gmail.current_history_id()
            else:
                log.error("Gmail API error: %s", exc)

        return results

    # ── Private helpers ──────────────────────────────────────────────────────

    def _process_message(self, msg_id: str) -> SyncResult:
        try:
            msg = self._gmail.fetch_message(msg_id)
        except HttpError as exc:
            return SyncResult("ignored", "", reason=f"Gmail fetch error: {exc}")

        headers = msg.get("payload", {}).get("headers", [])
        from_val = next(
            (h["value"] for h in headers if h["name"].lower() == "from"), None
        )
        subject = next(
            (h["value"] for h in headers if h["name"].lower() == "subject"),
            "(no subject)",
        )

        if not from_val:
            return SyncResult("ignored", "", reason="Missing From header")

        sender = parse_sender(from_val)
        if not sender.email:
            return SyncResult("ignored", "", reason="Unparseable From header")
        if is_automated(sender):
            return SyncResult("ignored", sender.email, reason="Automated sender")

        return self._upsert(sender, subject)

    def _upsert(self, sender: SenderInfo, subject: str) -> SyncResult:
        existing = self._hs.find_by_email(sender.email)

        if existing:
            contact_id: str = existing["id"]
            ep = existing.get("properties") or {}

            # Fill in only fields that are currently blank
            update_props: Dict[str, str] = {}
            if sender.first_name and not ep.get("firstname"):
                update_props["firstname"] = sender.first_name
            if sender.last_name and not ep.get("lastname"):
                update_props["lastname"] = sender.last_name
            if sender.company and not ep.get("company"):
                update_props["company"] = sender.company

            updated = self._hs.update_contact(contact_id, update_props)
            self._hs.add_note(contact_id, _build_note(sender, subject))

            status = "updated" if updated else "ignored"
            reason = "fields updated" if updated else "no new fields"
            return SyncResult(status, sender.email, contact_id=contact_id, reason=reason)

        # New contact
        contact_id = self._hs.create_contact(sender)
        if not contact_id:
            return SyncResult("ignored", sender.email, reason="HubSpot create failed")

        self._hs.add_note(contact_id, _build_note(sender, subject))
        return SyncResult("created", sender.email, contact_id=contact_id)


# ── Module-level helpers ──────────────────────────────────────────────────────

def _build_note(sender: SenderInfo, subject: str) -> str:
    return (
        f"[Inbound Gmail]  Tag: Inbound Gmail | Fonte: Gmail\n"
        f"Da: {sender.email}\n"
        f"Oggetto: {subject}"
    )


def _log_result(r: SyncResult) -> None:
    log.info(
        "[%-8s] email=%-40s id=%-12s reason=%s",
        r.status.upper(),
        r.email or "-",
        r.contact_id or "-",
        r.reason,
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    creds_file = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
    token_file = os.environ.get("GMAIL_TOKEN_FILE", "token.json")
    hs_token = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))

    if not hs_token:
        raise SystemExit("ERROR: HUBSPOT_ACCESS_TOKEN is not set")
    if not os.path.exists(creds_file):
        raise SystemExit(f"ERROR: Gmail credentials file not found: {creds_file!r}")

    gmail = GmailClient(creds_file, token_file)
    hs = HubSpotClient(hs_token)
    state = StateManager()
    engine = SyncEngine(gmail, hs, state)

    log.info("Gmail→HubSpot sync started (poll interval=%ds)", interval)

    while True:
        try:
            engine.poll()
        except KeyboardInterrupt:
            log.info("Stopped by user")
            break
        except Exception as exc:
            log.exception("Unexpected error in sync loop: %s", exc)

        time.sleep(interval)


if __name__ == "__main__":
    main()
