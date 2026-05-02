import json
import os
import re
from dataclasses import dataclass
from email.utils import parseaddr, parsedate_to_datetime
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import config


@dataclass
class SenderInfo:
    email: str
    display_name: str
    first_name: str
    last_name: str
    domain: str
    message_id: str
    subject: str
    date: str


def _get_credentials() -> Credentials:
    creds = None
    if os.path.exists(config.GOOGLE_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(
            config.GOOGLE_TOKEN_FILE, config.GMAIL_SCOPES
        )
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                config.GOOGLE_CREDENTIALS_FILE, config.GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(config.GOOGLE_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds


def build_service():
    return build("gmail", "v1", credentials=_get_credentials())


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _parse_name(display_name: str) -> tuple[str, str]:
    """Split a display name into first and last name."""
    parts = display_name.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _domain_from_email(email: str) -> str:
    """Return the domain part of an email address."""
    match = re.search(r"@([\w.-]+)$", email.lower())
    return match.group(1) if match else ""


def fetch_new_messages(service, since_history_id: Optional[str] = None) -> list[dict]:
    """
    Return Gmail message resources for unread inbox messages.

    Uses historyId-based incremental sync when available; falls back to
    a full query so the first run still works.
    """
    user = "me"

    if since_history_id:
        try:
            history = (
                service.users()
                .history()
                .list(
                    userId=user,
                    startHistoryId=since_history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
            messages = []
            for record in history.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg = added["message"]
                    if "UNREAD" in msg.get("labelIds", []):
                        messages.append(msg)
            return messages
        except Exception:
            # historyId expired or invalid – fall back to full list
            pass

    result = (
        service.users()
        .messages()
        .list(userId=user, q=config.GMAIL_QUERY, maxResults=50)
        .execute()
    )
    return result.get("messages", [])


def get_sender_info(service, message_id: str) -> Optional[SenderInfo]:
    """Fetch a message and extract sender metadata."""
    msg = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="metadata",
             metadataHeaders=["From", "Subject", "Date"])
        .execute()
    )
    headers = msg.get("payload", {}).get("headers", [])
    from_raw = _header(headers, "From")
    if not from_raw:
        return None

    display_name, email = parseaddr(from_raw)
    email = email.lower().strip()
    if not email or "@" not in email:
        return None

    domain = _domain_from_email(email)
    first_name, last_name = _parse_name(display_name)
    subject = _header(headers, "Subject")
    date = _header(headers, "Date")

    return SenderInfo(
        email=email,
        display_name=display_name,
        first_name=first_name,
        last_name=last_name,
        domain=domain,
        message_id=message_id,
        subject=subject,
        date=date,
    )


def get_current_history_id(service) -> str:
    """Return the current mailbox historyId for incremental sync."""
    profile = service.users().getProfile(userId="me").execute()
    return profile["historyId"]
