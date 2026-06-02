"""Gmail API client for reading incoming emails."""

import os
import re
import email.utils
from typing import Optional
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
]


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = self._authenticate()

    def _authenticate(self):
        creds = None
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(self.token_file, "w") as f:
                f.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    def get_inbox_messages(self, after_timestamp: Optional[int] = None, max_results: int = 50) -> list[dict]:
        """Fetch messages from INBOX, optionally filtered by timestamp (Unix seconds)."""
        query = "in:inbox -from:me"
        if after_timestamp:
            query += f" after:{after_timestamp}"

        result = (
            self.service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        return result.get("messages", [])

    def get_message_details(self, message_id: str) -> Optional[dict]:
        """Fetch full message headers and extract sender info."""
        msg = (
            self.service.users()
            .messages()
            .get(userId="me", id=message_id, format="metadata",
                 metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        from_header = headers.get("From", "")
        if not from_header:
            return None

        parsed_name, parsed_email = email.utils.parseaddr(from_header)
        if not parsed_email or "@" not in parsed_email:
            return None

        parsed_email = parsed_email.lower().strip()
        domain = parsed_email.split("@")[1]

        first_name, last_name = _split_name(parsed_name)
        company = _company_from_domain(domain)

        return {
            "message_id": message_id,
            "thread_id": msg.get("threadId"),
            "email": parsed_email,
            "display_name": parsed_name.strip(),
            "first_name": first_name,
            "last_name": last_name,
            "domain": domain,
            "company": company,
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
            "internal_date": int(msg.get("internalDate", 0)) // 1000,
        }

    def ensure_label(self, label_name: str) -> str:
        """Get or create a Gmail label, return its ID."""
        labels = self.service.users().labels().list(userId="me").execute()
        for label in labels.get("labels", []):
            if label["name"] == label_name:
                return label["id"]

        new_label = (
            self.service.users()
            .labels()
            .create(userId="me", body={"name": label_name})
            .execute()
        )
        return new_label["id"]

    def apply_label(self, message_id: str, label_id: str):
        """Apply a label to a Gmail message."""
        self.service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [label_id]},
        ).execute()


_PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "live.com", "aol.com", "protonmail.com",
    "me.com", "mac.com", "msn.com",
}

_SKIP_PREFIXES = (
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "notifications", "bounce", "mailer-daemon", "postmaster",
    "support", "info", "admin", "hello", "contact",
)


def _split_name(display_name: str) -> tuple[str, str]:
    name = display_name.strip().strip('"')
    if not name:
        return "", ""
    parts = name.split(None, 1)
    return parts[0], parts[1] if len(parts) > 1 else ""


def _company_from_domain(domain: str) -> str:
    if domain in _PERSONAL_DOMAINS:
        return ""
    root = domain.split(".")[0]
    return root.replace("-", " ").replace("_", " ").title()


def is_skippable(email_addr: str, skip_domains: set[str]) -> bool:
    """Return True if this sender should be ignored."""
    local, domain = email_addr.split("@")
    if domain in skip_domains:
        return True
    if any(local.startswith(p) for p in _SKIP_PREFIXES):
        return True
    return False
