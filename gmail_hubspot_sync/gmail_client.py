import os
import re
from email.header import decode_header
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


def get_gmail_service(credentials_file: str, token_file: str, scopes: list):
    """Authenticate and return a Gmail API service object."""
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, scopes)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, scopes)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _decode_header(value: str) -> str:
    """Decode RFC 2047 MIME-encoded header words."""
    if not value:
        return ""
    parts = []
    for raw, charset in decode_header(value):
        if isinstance(raw, bytes):
            parts.append(raw.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(raw)
    return "".join(parts)


def parse_sender(from_header: str) -> dict:
    """
    Parse a 'From' header into structured components.
    Handles both 'Name <email>' and bare 'email' formats.
    """
    from_header = _decode_header(from_header).strip()

    match = re.match(r'^"?([^"<]*)"?\s*<([^>]+)>\s*$', from_header)
    if match:
        name = match.group(1).strip().strip("'\"")
        email_addr = match.group(2).strip().lower()
    else:
        name = ""
        email_addr = from_header.lower()

    name_parts = name.split(None, 1) if name else []
    firstname = name_parts[0] if name_parts else ""
    lastname = name_parts[1] if len(name_parts) > 1 else ""

    domain = email_addr.split("@")[1] if "@" in email_addr else ""

    return {
        "email": email_addr,
        "name": name,
        "firstname": firstname,
        "lastname": lastname,
        "domain": domain,
    }


def fetch_inbox_message_ids(service, since_epoch: int = None, max_results: int = 200) -> list:
    """
    Return a list of message-ID dicts from INBOX.
    Excludes messages sent by the authenticated user and drafts.
    """
    query = "in:inbox -from:me -in:draft"
    if since_epoch:
        query += f" after:{since_epoch}"

    ids = []
    page_token = None

    while True:
        kwargs: dict = {
            "userId": "me",
            "q": query,
            "maxResults": min(max_results - len(ids), 100),
        }
        if page_token:
            kwargs["pageToken"] = page_token

        response = service.users().messages().list(**kwargs).execute()
        ids.extend(response.get("messages", []))

        page_token = response.get("nextPageToken")
        if not page_token or len(ids) >= max_results:
            break

    return ids


def get_message_meta(service, message_id: str) -> Optional[dict]:
    """
    Fetch From / Subject / Date headers for a single message.
    Returns None if the message cannot be retrieved.
    """
    try:
        msg = service.users().messages().get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()

        headers = {
            h["name"]: h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        from_header = headers.get("From", "")
        if not from_header:
            return None

        sender = parse_sender(from_header)
        sender["subject"] = headers.get("Subject", "")
        sender["date"] = headers.get("Date", "")
        sender["message_id"] = message_id
        return sender

    except Exception as exc:
        print(f"    [gmail] Error reading message {message_id}: {exc}")
        return None
