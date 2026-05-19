import os
import email as email_lib
from dataclasses import dataclass
from typing import Optional

from . import config


@dataclass
class SenderInfo:
    email: str
    first_name: str
    last_name: str
    domain: str
    subject: str
    message_id: str


def _get_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if os.path.exists(config.GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(
            config.GMAIL_TOKEN_FILE, config.GMAIL_SCOPES
        )
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                config.GMAIL_CREDENTIALS_FILE, config.GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(config.GMAIL_TOKEN_FILE, "w") as fh:
            fh.write(creds.to_json())
    return creds


def get_service():
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=_get_credentials())


def _parse_name(display_name: str) -> tuple[str, str]:
    """Split 'First Last' into (first, last). Returns ('', '') on empty."""
    parts = display_name.strip().split(None, 1)
    if not parts:
        return "", ""
    first = parts[0]
    last = parts[1] if len(parts) > 1 else ""
    return first, last


def _is_skippable(address: str) -> bool:
    local, _, domain = address.partition("@")
    if domain.lower() in config.SKIP_DOMAINS:
        return True
    for prefix in config.SKIP_EMAILS:
        if local.lower().startswith(prefix):
            return True
    return False


def fetch_new_messages(service, since_history_id: Optional[str]) -> tuple[list[SenderInfo], str]:
    """
    Return (list_of_senders, new_history_id).
    On first run (since_history_id is None) fetches the last 50 unread messages.
    """
    if since_history_id is None:
        return _fetch_recent(service)
    return _fetch_via_history(service, since_history_id)


def _fetch_recent(service) -> tuple[list[SenderInfo], str]:
    result = service.users().messages().list(
        userId="me", labelIds=["INBOX"], maxResults=50
    ).execute()
    messages = result.get("messages", [])
    senders, last_id = _extract_senders(service, messages)
    return senders, last_id


def _fetch_via_history(service, history_id: str) -> tuple[list[SenderInfo], str]:
    try:
        result = service.users().history().list(
            userId="me",
            startHistoryId=history_id,
            historyTypes=["messageAdded"],
            labelId="INBOX",
        ).execute()
    except Exception:
        # History expired; fall back to recent
        return _fetch_recent(service)

    added = []
    for record in result.get("history", []):
        for item in record.get("messagesAdded", []):
            added.append(item["message"])

    new_history_id = result.get("historyId", history_id)
    if not added:
        return [], new_history_id

    senders, _ = _extract_senders(service, added)
    return senders, new_history_id


def _extract_senders(service, messages: list) -> tuple[list[SenderInfo], str]:
    senders = []
    last_history_id = ""
    for msg_ref in messages:
        msg = service.users().messages().get(
            userId="me", id=msg_ref["id"], format="metadata",
            metadataHeaders=["From", "Subject"]
        ).execute()
        last_history_id = msg.get("historyId", last_history_id)

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        from_raw = headers.get("From", "")
        subject = headers.get("Subject", "")

        parsed = email_lib.utils.parseaddr(from_raw)
        display_name, address = parsed
        address = address.lower().strip()
        if not address or _is_skippable(address):
            continue

        _, _, domain = address.partition("@")
        first, last = _parse_name(display_name)
        senders.append(SenderInfo(
            email=address,
            first_name=first,
            last_name=last,
            domain=domain,
            subject=subject,
            message_id=msg["id"],
        ))
    return senders, last_history_id


def get_current_history_id(service) -> str:
    profile = service.users().getProfile(userId="me").execute()
    return profile.get("historyId", "")
