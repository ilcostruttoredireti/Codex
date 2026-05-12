"""
Gmail → HubSpot contact sync.

Polls Gmail INBOX for new messages, extracts sender info, and creates or
updates HubSpot contacts — using email address as the unique key.

Usage:
    python gmail_hubspot_sync.py              # run once
    python gmail_hubspot_sync.py --loop       # poll continuously
    python gmail_hubspot_sync.py --loop --interval 120   # poll every 2 min

Required environment variables (copy .env.example → .env):
    HUBSPOT_ACCESS_TOKEN
    GOOGLE_CREDENTIALS_FILE   (OAuth2 credentials JSON from Google Cloud)
    GOOGLE_TOKEN_FILE         (auto-created on first run after browser auth)
"""

from __future__ import annotations

import argparse
import base64
import email as email_lib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
HUBSPOT_BASE = "https://api.hubapi.com"
STATE_FILE = Path(".processed_message_ids.json")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SenderInfo:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    raw_name: str = ""

    @property
    def domain(self) -> str:
        parts = self.email.split("@")
        return parts[1] if len(parts) == 2 else ""


@dataclass
class SyncResult:
    status: str          # "created" | "updated" | "ignored" | "error"
    email: str
    contact_id: Optional[str] = None
    detail: str = ""


# ---------------------------------------------------------------------------
# Persistent state (tracks which Gmail message IDs were already processed)
# ---------------------------------------------------------------------------

def load_state() -> set[str]:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except Exception:
            pass
    return set()


def save_state(ids: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(ids)))


# ---------------------------------------------------------------------------
# Gmail client
# ---------------------------------------------------------------------------

def _get_gmail_credentials():  # -> google.oauth2.credentials.Credentials
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_file = os.environ.get("GOOGLE_TOKEN_FILE", "token.json")
    creds_file = os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json")

    creds = None
    if Path(token_file).exists():
        creds = Credentials.from_authorized_user_file(token_file, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_file, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        Path(token_file).write_text(creds.to_json())

    return creds


def _parse_sender(raw_from: str) -> SenderInfo:
    """Parse a RFC-5322 From header into structured SenderInfo."""
    raw_from = raw_from.strip()
    # "First Last <user@example.com>" or "user@example.com"
    match = re.match(r"^(.*?)\s*<([^>]+)>$", raw_from)
    if match:
        display_name = match.group(1).strip().strip('"')
        addr = match.group(2).strip().lower()
    else:
        display_name = ""
        addr = raw_from.lower()

    info = SenderInfo(email=addr, raw_name=display_name)

    if display_name:
        parts = display_name.split(None, 1)
        info.first_name = parts[0]
        info.last_name = parts[1] if len(parts) > 1 else ""

    # Infer company from domain (skip generic free-mail providers)
    _free_domains = {
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
        "live.com", "icloud.com", "me.com", "protonmail.com",
        "aol.com", "mail.com", "zoho.com",
    }
    if info.domain and info.domain not in _free_domains:
        info.company = info.domain.split(".")[0].capitalize()

    return info


def fetch_new_messages(service, processed_ids: set[str]) -> list[tuple[str, SenderInfo]]:
    from googleapiclient.errors import HttpError
    """Return (message_id, SenderInfo) for unprocessed INBOX messages."""
    results = []
    try:
        response = (
            service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=100)
            .execute()
        )
    except HttpError as exc:
        log.error("Gmail list error: %s", exc)
        return results

    messages = response.get("messages", [])
    for msg in messages:
        msg_id = msg["id"]
        if msg_id in processed_ids:
            continue
        try:
            full = (
                service.users()
                .messages()
                .get(userId="me", id=msg_id, format="metadata",
                     metadataHeaders=["From", "Subject"])
                .execute()
            )
        except HttpError as exc:
            log.warning("Could not fetch message %s: %s", msg_id, exc)
            continue

        headers = {
            h["name"].lower(): h["value"]
            for h in full.get("payload", {}).get("headers", [])
        }
        raw_from = headers.get("from", "")
        if not raw_from:
            continue

        sender = _parse_sender(raw_from)
        if not sender.email or "@" not in sender.email:
            continue

        results.append((msg_id, sender))

    return results


# ---------------------------------------------------------------------------
# HubSpot client
# ---------------------------------------------------------------------------

class HubSpotClient:
    def __init__(self, token: str) -> None:
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _get(self, path: str, **kwargs) -> requests.Response:
        return requests.get(f"{HUBSPOT_BASE}{path}", headers=self._headers, **kwargs)

    def _post(self, path: str, payload: dict) -> requests.Response:
        return requests.post(
            f"{HUBSPOT_BASE}{path}", headers=self._headers, json=payload
        )

    def _patch(self, path: str, payload: dict) -> requests.Response:
        return requests.patch(
            f"{HUBSPOT_BASE}{path}", headers=self._headers, json=payload
        )

    def find_contact_by_email(self, email: str) -> Optional[dict]:
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
                "properties": ["email", "firstname", "lastname", "company", "hs_lead_status"],
                "limit": 1,
            },
        )
        if resp.status_code != 200:
            log.warning("HubSpot search failed (%s): %s", resp.status_code, resp.text)
            return None
        data = resp.json()
        results = data.get("results", [])
        return results[0] if results else None

    def create_contact(self, sender: SenderInfo) -> Optional[str]:
        props = self._build_properties(sender, is_new=True)
        resp = self._post("/crm/v3/objects/contacts", {"properties": props})
        if resp.status_code in (200, 201):
            return resp.json().get("id")
        log.warning("HubSpot create failed (%s): %s", resp.status_code, resp.text)
        return None

    def update_contact(self, contact_id: str, sender: SenderInfo, existing: dict) -> bool:
        existing_props = existing.get("properties", {})
        props = self._build_properties(sender, is_new=False, existing=existing_props)
        if not props:
            return False  # nothing to update
        resp = self._patch(f"/crm/v3/objects/contacts/{contact_id}", {"properties": props})
        return resp.status_code == 200

    def add_timeline_activity(self, contact_id: str, sender: SenderInfo) -> None:
        """Create a note on the contact timeline recording the inbound email."""
        note_body = (
            f"Inbound email received from {sender.raw_name or sender.email} "
            f"<{sender.email}>. Source: Gmail."
        )
        resp = self._post(
            "/crm/v3/objects/notes",
            {
                "properties": {
                    "hs_note_body": note_body,
                    "hs_timestamp": str(int(time.time() * 1000)),
                },
                "associations": [
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
            },
        )
        if resp.status_code not in (200, 201):
            log.debug("Timeline note failed (%s): %s", resp.status_code, resp.text)

    @staticmethod
    def _build_properties(
        sender: SenderInfo,
        *,
        is_new: bool,
        existing: Optional[dict] = None,
    ) -> dict:
        existing = existing or {}
        props: dict[str, str] = {}

        if is_new:
            props["email"] = sender.email
            props["hs_lead_source"] = "GMAIL"

        def set_if_missing(hs_key: str, value: str) -> None:
            if value and not existing.get(hs_key):
                props[hs_key] = value

        set_if_missing("firstname", sender.first_name)
        set_if_missing("lastname", sender.last_name)
        set_if_missing("company", sender.company)

        return props


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def process_email(
    msg_id: str,
    sender: SenderInfo,
    hs: HubSpotClient,
    *,
    add_timeline: bool = True,
) -> SyncResult:
    existing = hs.find_contact_by_email(sender.email)

    if existing is None:
        contact_id = hs.create_contact(sender)
        if contact_id is None:
            return SyncResult("error", sender.email, detail="HubSpot create failed")
        if add_timeline:
            hs.add_timeline_activity(contact_id, sender)
        return SyncResult("created", sender.email, contact_id=contact_id)

    contact_id = existing["id"]
    updated = hs.update_contact(contact_id, sender, existing)
    if add_timeline:
        hs.add_timeline_activity(contact_id, sender)
    status = "updated" if updated else "ignored"
    return SyncResult(status, sender.email, contact_id=contact_id)


def run_sync(hs: HubSpotClient, gmail_service) -> list[SyncResult]:
    processed = load_state()
    new_messages = fetch_new_messages(gmail_service, processed)

    if not new_messages:
        log.info("No new messages to process.")
        return []

    results: list[SyncResult] = []
    newly_processed: set[str] = set()

    for msg_id, sender in new_messages:
        log.info("Processing message %s from %s", msg_id, sender.email)
        result = process_email(msg_id, sender, hs)
        results.append(result)
        newly_processed.add(msg_id)

        icon = {"created": "+", "updated": "~", "ignored": "=", "error": "!"}.get(
            result.status, "?"
        )
        print(
            f"[{icon}] {result.status.upper():8s}  {result.email:<40s}  "
            f"ID: {result.contact_id or 'N/A'}"
            + (f"  ({result.detail})" if result.detail else "")
        )

    save_state(processed | newly_processed)
    return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--loop", action="store_true", help="Poll continuously")
    parser.add_argument(
        "--interval", type=int, default=60, help="Seconds between polls (default: 60)"
    )
    args = parser.parse_args()

    hubspot_token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not hubspot_token:
        raise SystemExit("HUBSPOT_ACCESS_TOKEN environment variable is required.")

    hs = HubSpotClient(hubspot_token)
    from googleapiclient.discovery import build
    creds = _get_gmail_credentials()
    gmail = build("gmail", "v1", credentials=creds)

    log.info("Gmail → HubSpot sync started. Loop=%s, interval=%ss", args.loop, args.interval)

    if args.loop:
        while True:
            try:
                run_sync(hs, gmail)
            except Exception as exc:
                log.error("Sync cycle error: %s", exc)
            log.info("Sleeping %s seconds...", args.interval)
            time.sleep(args.interval)
    else:
        run_sync(hs, gmail)


if __name__ == "__main__":
    main()
