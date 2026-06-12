"""Gmail API client — fetches unread inbox messages and parses sender info."""
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .config import GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES, GMAIL_TOKEN_FILE


@dataclass
class SenderInfo:
    email: str
    first_name: str
    last_name: str
    full_name: str
    domain: str
    company: str           # derived from domain
    message_id: str
    thread_id: str
    subject: str


def _get_credentials() -> Credentials:
    creds = None
    token_path = Path(GMAIL_TOKEN_FILE)
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())
    return creds


def _parse_from_header(raw: str) -> tuple[str, str]:
    """Return (display_name, email) from a From: header value."""
    m = re.match(r'^"?([^"<]*)"?\s*<?([^>]+@[^>]+)>?$', raw.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip().lower()
    return "", raw.strip().lower()


def _company_from_domain(domain: str) -> str:
    """Best-effort company name from email domain."""
    # Strip common TLDs and subdomains
    parts = domain.split(".")
    if parts[0] in ("mail", "m", "smtp", "em", "bounce", "news"):
        parts = parts[1:]
    name = parts[0] if parts else domain
    return name.replace("-", " ").title()


def _split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def build_service():
    return build("gmail", "v1", credentials=_get_credentials())


def fetch_unread_inbox(service, max_results: int = 50) -> list[SenderInfo]:
    """Return SenderInfo for each unread inbox message (newest first)."""
    result = service.users().messages().list(
        userId="me",
        q="in:inbox is:unread",
        maxResults=max_results,
    ).execute()

    messages = result.get("messages", [])
    senders: list[SenderInfo] = []

    for msg_ref in messages:
        msg = service.users().messages().get(
            userId="me", id=msg_ref["id"], format="metadata",
            metadataHeaders=["From", "Subject"],
        ).execute()

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        raw_from = headers.get("From", "")
        subject = headers.get("Subject", "")
        if not raw_from:
            continue

        display_name, email = _parse_from_header(raw_from)
        if "@" not in email:
            continue

        domain = email.split("@")[1]
        first_name, last_name = _split_name(display_name)

        senders.append(SenderInfo(
            email=email,
            first_name=first_name,
            last_name=last_name,
            full_name=display_name,
            domain=domain,
            company=_company_from_domain(domain),
            message_id=msg_ref["id"],
            thread_id=msg.get("threadId", ""),
            subject=subject,
        ))

    return senders
