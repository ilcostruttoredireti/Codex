"""Gmail inbox monitor — fetches new messages and extracts sender info."""

import json
import os
import re
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

_DISPLAY_NAME_RE = re.compile(r'^"?([^"<]+?)"?\s*<')


def _build_service(credentials_file: str, token_file: str):
    creds = None
    if Path(token_file).exists():
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)
        Path(token_file).write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _extract_header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _parse_sender(raw_from: str) -> tuple[str, str, str]:
    """Return (email, first_name, last_name) parsed from a From header."""
    raw_from = raw_from.strip()

    # "Display Name" <email@domain.com>  or  Display Name <email@domain.com>
    angle_match = re.search(r"<([^>]+)>", raw_from)
    if angle_match:
        email = angle_match.group(1).strip().lower()
        display = raw_from[: angle_match.start()].strip().strip('"').strip()
    else:
        email = raw_from.lower()
        display = ""

    parts = display.split() if display else []
    first = parts[0] if parts else ""
    last = " ".join(parts[1:]) if len(parts) > 1 else ""

    return email, first, last


def _domain_from_email(email: str) -> str:
    """Return company domain, skipping common free-mail providers."""
    _free_domains = {
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
        "icloud.com", "live.com", "aol.com", "protonmail.com",
        "me.com", "msn.com",
    }
    parts = email.split("@")
    if len(parts) == 2 and parts[1] not in _free_domains:
        return parts[1]
    return ""


class GmailMonitor:
    def __init__(self, credentials_file: str, token_file: str, state_file: str):
        self._service = _build_service(credentials_file, token_file)
        self._state_file = state_file
        self._processed_ids: set[str] = self._load_state()

    # ------------------------------------------------------------------
    # State persistence (tracks which message IDs were already handled)
    # ------------------------------------------------------------------

    def _load_state(self) -> set[str]:
        if Path(self._state_file).exists():
            data = json.loads(Path(self._state_file).read_text())
            return set(data.get("processed_ids", []))
        return set()

    def _save_state(self) -> None:
        data = {"processed_ids": list(self._processed_ids)}
        Path(self._state_file).write_text(json.dumps(data, indent=2))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_new_messages(self) -> list[dict]:
        """
        Return a list of sender dicts for messages not yet processed.

        Each dict: {message_id, email, first_name, last_name, company, subject}
        """
        results = (
            self._service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=100)
            .execute()
        )
        messages = results.get("messages", [])
        new_senders = []

        for msg_ref in messages:
            msg_id = msg_ref["id"]
            if msg_id in self._processed_ids:
                continue

            msg = (
                self._service.users()
                .messages()
                .get(userId="me", id=msg_id, format="metadata",
                     metadataHeaders=["From", "Subject"])
                .execute()
            )
            headers = msg.get("payload", {}).get("headers", [])
            raw_from = _extract_header(headers, "From")
            subject = _extract_header(headers, "Subject")

            if not raw_from:
                self._processed_ids.add(msg_id)
                continue

            email, first, last = _parse_sender(raw_from)
            company = _domain_from_email(email)

            new_senders.append({
                "message_id": msg_id,
                "email": email,
                "first_name": first,
                "last_name": last,
                "company": company,
                "subject": subject,
            })

        return new_senders

    def mark_processed(self, message_id: str) -> None:
        self._processed_ids.add(message_id)
        self._save_state()
