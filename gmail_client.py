"""Gmail API client — handles auth and email fetching."""

import os
import base64
import email as email_lib
from email.header import decode_header
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _decode_header_value(value: str) -> str:
    parts = decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


def build_gmail_service(credentials_file: str, token_file: str):
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as fh:
            fh.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def fetch_messages(service, query: str, max_results: int = 100) -> list[dict]:
    """Return a list of message stubs {id, threadId} matching *query*."""
    result = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=max_results)
        .execute()
    )
    return result.get("messages", [])


def parse_sender(service, message_id: str) -> Optional[dict]:
    """
    Fetch a single message and extract sender metadata.

    Returns a dict with keys: email, name, domain, subject, message_id
    or None if the From header is missing / sender is a no-reply address.
    """
    msg = (
        service.users()
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

    # Parse "Display Name <addr@domain.com>" or bare address
    parsed = email_lib.utils.parseaddr(raw_from)
    display_name = _decode_header_value(parsed[0]).strip()
    address = parsed[1].strip().lower()

    if not address or address.startswith("no-reply") or address.startswith("noreply"):
        return None

    domain = address.split("@")[-1] if "@" in address else ""

    # Split display name into first / last name
    name_parts = display_name.split(" ", 1) if display_name else []
    first_name = name_parts[0] if name_parts else ""
    last_name = name_parts[1] if len(name_parts) > 1 else ""

    return {
        "email": address,
        "name": display_name,
        "first_name": first_name,
        "last_name": last_name,
        "domain": domain,
        "subject": subject,
        "message_id": message_id,
        "internal_date": msg.get("internalDate", "0"),
    }
