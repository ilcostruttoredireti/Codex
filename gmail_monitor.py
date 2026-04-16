"""
Gmail monitor: fetches incoming email threads and extracts sender contact data.
"""

import os
import re
import json
import logging
from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Domains that don't represent real companies
_FREEMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "live.com", "aol.com", "protonmail.com",
    "mail.com", "zoho.com", "yandex.com", "gmx.com",
}


@dataclass
class SenderContact:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    thread_id: str = ""
    message_id: str = ""
    subject: str = ""


def _parse_name(display_name: str) -> tuple[str, str]:
    """Split a display name into first/last name."""
    parts = display_name.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _company_from_domain(domain: str) -> str:
    """Derive a company name from an email domain, skipping free-mail providers."""
    if domain in _FREEMAIL_DOMAINS:
        return ""
    # Strip TLD and capitalise: "acme.io" → "Acme"
    base = domain.split(".")[0]
    return base.capitalize()


_NOREPLY_PATTERNS = ("noreply", "no-reply", "no_reply", "mailer-daemon", "posta-certificata", "donotreply")


def _is_automated_sender(email: str) -> bool:
    local = email.split("@")[0].lower()
    return any(p in local for p in _NOREPLY_PATTERNS)


def _extract_sender(from_header: str, thread_id: str, message_id: str, subject: str) -> Optional[SenderContact]:
    display_name, raw_email = parseaddr(from_header)
    raw_email = raw_email.lower().strip()
    if not raw_email or "@" not in raw_email:
        return None
    if _is_automated_sender(raw_email):
        return None

    domain = raw_email.split("@")[1]
    first, last = _parse_name(display_name)

    # Fall back to local-part when display name is absent
    if not first:
        local = raw_email.split("@")[0]
        # "john.doe" → John Doe
        parts = re.split(r"[._\-+]", local)
        first = parts[0].capitalize() if parts else ""
        last = parts[1].capitalize() if len(parts) > 1 else ""

    return SenderContact(
        email=raw_email,
        first_name=first,
        last_name=last,
        company=_company_from_domain(domain),
        thread_id=thread_id,
        message_id=message_id,
        subject=subject,
    )


class GmailMonitor:
    def __init__(
        self,
        credentials_file: str = "credentials.json",
        token_file: str = "token.json",
    ):
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._service = None

    def authenticate(self) -> None:
        creds = None
        if os.path.exists(self._token_file):
            creds = Credentials.from_authorized_user_file(self._token_file, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(self._token_file, "w") as fh:
                fh.write(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)
        logger.info("Gmail authentication successful.")

    def fetch_inbox_senders(
        self,
        max_results: int = 50,
        processed_ids: set[str] | None = None,
        query: str = "in:inbox",
    ) -> list[SenderContact]:
        """Return SenderContact objects for threads not yet in processed_ids."""
        if self._service is None:
            self.authenticate()

        processed_ids = processed_ids or set()
        results = (
            self._service.users()
            .threads()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        threads = results.get("threads", [])
        contacts: list[SenderContact] = []

        for thread in threads:
            tid = thread["id"]
            if tid in processed_ids:
                continue

            thread_data = (
                self._service.users()
                .threads()
                .get(userId="me", id=tid, format="metadata", metadataHeaders=["From", "Subject"])
                .execute()
            )

            messages = thread_data.get("messages", [])
            if not messages:
                continue

            first_msg = messages[0]
            msg_id = first_msg["id"]
            headers = {
                h["name"]: h["value"]
                for h in first_msg.get("payload", {}).get("headers", [])
            }

            from_header = headers.get("From", "")
            subject = headers.get("Subject", "(no subject)")
            contact = _extract_sender(from_header, tid, msg_id, subject)
            if contact:
                contacts.append(contact)

        logger.info("Fetched %d new sender contacts from Gmail.", len(contacts))
        return contacts


class ProcessedStore:
    """Persist processed thread IDs to a JSON file."""

    def __init__(self, path: str = "processed_threads.json"):
        self._path = path
        self._ids: set[str] = self._load()

    def _load(self) -> set[str]:
        if os.path.exists(self._path):
            with open(self._path) as fh:
                return set(json.load(fh))
        return set()

    def save(self) -> None:
        with open(self._path, "w") as fh:
            json.dump(list(self._ids), fh, indent=2)

    def contains(self, thread_id: str) -> bool:
        return thread_id in self._ids

    def add(self, thread_id: str) -> None:
        self._ids.add(thread_id)

    @property
    def ids(self) -> set[str]:
        return self._ids
