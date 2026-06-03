import email as email_lib
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .models import SenderContact

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Domains/emails that are system-generated and should never become contacts
_SYSTEM_SENDERS = {
    "mailer-daemon@googlemail.com",
    "mailer-daemon@google.com",
    "noreply@accounts.google.com",
    "no-reply@accounts.google.com",
}
_SYSTEM_DOMAINS = {"googlemail.com", "google.com"}
_SYSTEM_PREFIXES = ("mailer-daemon", "postmaster", "noreply", "no-reply")


def _is_system_sender(email: str) -> bool:
    if email.lower() in _SYSTEM_SENDERS:
        return True
    domain = email.split("@")[-1].lower() if "@" in email else ""
    if domain in _SYSTEM_DOMAINS:
        return True
    local = email.split("@")[0].lower()
    return any(local.startswith(p) for p in _SYSTEM_PREFIXES)


def _parse_sender(raw_from: str) -> tuple[str, Optional[str], Optional[str]]:
    """Return (email, first_name, last_name) from a raw From header."""
    parsed = email_lib.utils.parseaddr(raw_from)
    display_name, addr = parsed
    addr = addr.strip().lower()

    first_name: Optional[str] = None
    last_name: Optional[str] = None

    if display_name:
        parts = display_name.strip().split()
        if len(parts) >= 2:
            first_name = parts[0].title()
            last_name = " ".join(parts[1:]).title()
        elif len(parts) == 1:
            first_name = parts[0].title()

    return addr, first_name, last_name


def _company_from_domain(domain: str) -> str:
    """Best-effort company name from an email domain."""
    # Strip common TLDs and return a capitalised name
    parts = domain.split(".")
    # Drop the last TLD segment(s); keep the meaningful part
    if len(parts) >= 2:
        name = parts[-2]
    else:
        name = parts[0]
    return name.replace("-", " ").replace("_", " ").title()


class GmailClient:
    def __init__(
        self,
        credentials_file: str = "credentials.json",
        token_file: str = "token.json",
    ):
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._service = None

    def _authenticate(self) -> None:
        creds: Optional[Credentials] = None

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
            with open(self._token_file, "w") as f:
                f.write(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)

    @property
    def service(self):
        if self._service is None:
            self._authenticate()
        return self._service

    def list_inbox_message_ids(
        self,
        since: Optional[datetime] = None,
        skip_emails: Optional[set] = None,
        skip_domains: Optional[set] = None,
        max_results: int = 200,
    ) -> list[str]:
        """Return message IDs of inbound messages since *since*."""
        query_parts = ["in:inbox", "-from:me"]
        if since:
            ts = int(since.timestamp())
            query_parts.append(f"after:{ts}")

        query = " ".join(query_parts)
        message_ids: list[str] = []
        page_token = None

        while True:
            kwargs: dict = {"userId": "me", "q": query, "maxResults": min(max_results, 500)}
            if page_token:
                kwargs["pageToken"] = page_token

            result = self.service.users().messages().list(**kwargs).execute()
            messages = result.get("messages", [])
            message_ids.extend(m["id"] for m in messages)

            page_token = result.get("nextPageToken")
            if not page_token or len(message_ids) >= max_results:
                break

        return message_ids[:max_results]

    def get_sender_contact(
        self,
        message_id: str,
        skip_emails: Optional[set] = None,
        skip_domains: Optional[set] = None,
        own_emails: Optional[set] = None,
    ) -> Optional[SenderContact]:
        """Fetch a Gmail message and return a SenderContact or None if skippable."""
        msg = (
            self.service.users()
            .messages()
            .get(userId="me", id=message_id, format="metadata",
                 metadataHeaders=["From", "Subject"])
            .execute()
        )

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        raw_from = headers.get("From", "")
        subject = headers.get("Subject", "")

        if not raw_from:
            return None

        addr, first_name, last_name = _parse_sender(raw_from)

        if not addr or "@" not in addr:
            return None

        domain = addr.split("@")[-1].lower()

        # Skip system senders
        if _is_system_sender(addr):
            return None

        # Skip own addresses
        if own_emails and addr in own_emails:
            return None

        # Skip explicitly configured emails
        if skip_emails and addr in skip_emails:
            return None

        # Skip explicitly configured domains
        if skip_domains and domain in skip_domains:
            return None

        company = _company_from_domain(domain)

        return SenderContact(
            email=addr,
            first_name=first_name,
            last_name=last_name,
            company=company,
            domain=domain,
            message_id=message_id,
            subject=subject,
        )
