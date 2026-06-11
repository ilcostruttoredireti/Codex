"""Gmail API client: authentication, message fetching, header parsing."""

import os
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from email.header import decode_header as _mime_decode_header

from .config import GMAIL_SCOPES, GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE

logger = logging.getLogger(__name__)


def authenticate() -> object:
    """Return an authenticated Gmail API service, running OAuth flow if needed."""
    creds = None

    if os.path.exists(GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Refreshing Gmail token...")
            creds.refresh(Request())
        else:
            logger.info("Running Gmail OAuth2 flow (browser will open)...")
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)

        with open(GMAIL_TOKEN_FILE, "w") as fh:
            fh.write(creds.to_json())
        logger.info("Gmail token saved.")

    return build("gmail", "v1", credentials=creds)


def _decode_mime(value: str) -> str:
    """Decode MIME encoded-word sequences in email header values."""
    parts = []
    for raw, charset in _mime_decode_header(value):
        if isinstance(raw, bytes):
            parts.append(raw.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(raw)
    return "".join(parts)


def parse_sender(from_header: str) -> Tuple[str, str, str]:
    """
    Parse a From header into (display_name, email_address, domain).

    Handles:
      - "Display Name <user@domain.com>"
      - '"Name" <user@domain.com>'
      - "user@domain.com"
    """
    from_header = _decode_mime(from_header.strip())

    angle_match = re.match(r'^(.*?)\s*<([^>]+)>\s*$', from_header)
    if angle_match:
        display_name = angle_match.group(1).strip().strip('"').strip("'")
        email_addr = angle_match.group(2).strip().lower()
    else:
        display_name = ""
        email_addr = from_header.strip().lower()

    domain = email_addr.split("@")[-1] if "@" in email_addr else ""
    return display_name, email_addr, domain


def get_new_message_refs(service, after_unix: int, max_results: int = 200) -> List[Dict]:
    """Return message stubs (id, threadId) for inbox messages newer than after_unix."""
    date_str = datetime.utcfromtimestamp(after_unix).strftime("%Y/%m/%d")
    query = f"in:inbox after:{date_str} -from:me"

    try:
        response = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        messages = response.get("messages", [])
        logger.debug(f"Gmail query '{query}' returned {len(messages)} message refs")
        return messages
    except HttpError as exc:
        logger.error(f"Gmail list error: {exc}")
        return []


def get_message_meta(service, message_id: str) -> Optional[Dict]:
    """Fetch metadata (From, Subject, Date headers) for a single message."""
    try:
        return (
            service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
    except HttpError as exc:
        logger.error(f"Gmail get message {message_id} error: {exc}")
        return None


def extract_message_info(message: Dict) -> Optional[Dict]:
    """
    Extract sender info from a Gmail message dict.
    Returns None if the From header is missing or malformed.
    """
    headers: Dict[str, str] = {
        h["name"]: h["value"]
        for h in message.get("payload", {}).get("headers", [])
    }

    from_header = headers.get("From", "").strip()
    if not from_header:
        return None

    display_name, email_addr, domain = parse_sender(from_header)

    if not email_addr or "@" not in email_addr:
        return None

    return {
        "message_id": message["id"],
        "thread_id": message.get("threadId", ""),
        "display_name": display_name,
        "email": email_addr,
        "domain": domain,
        "subject": headers.get("Subject", ""),
        "date_header": headers.get("Date", ""),
        "internal_date_ms": int(message.get("internalDate", "0")),
    }
