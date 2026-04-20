import os
import logging
from email.header import decode_header as _mime_decode

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import config

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class HistoryExpiredError(Exception):
    """Raised when the stored Gmail historyId is too old to resume from."""


def get_gmail_service():
    """Authenticate and return a Gmail API service object."""
    creds = None

    if os.path.exists(config.GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(config.GMAIL_TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                config.GMAIL_CREDENTIALS_FILE, SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(config.GMAIL_TOKEN_FILE, "w") as fh:
            fh.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_initial_state(service):
    """
    Return (current_history_id, list_of_recent_inbox_message_ids).
    Called on first run to anchor the history cursor.
    """
    profile = service.users().getProfile(userId="me").execute()
    history_id = profile["historyId"]

    result = service.users().messages().list(
        userId="me",
        labelIds=["INBOX"],
        maxResults=config.INITIAL_EMAILS_MAX,
    ).execute()

    msg_ids = [m["id"] for m in result.get("messages", [])]
    return history_id, msg_ids


def get_new_messages(service, start_history_id):
    """
    Return (list_of_new_inbox_msg_ids, new_history_id) for messages
    added since start_history_id.
    Raises HistoryExpiredError if the history ID is too old (Gmail 404).
    """
    try:
        response = service.users().history().list(
            userId="me",
            startHistoryId=start_history_id,
            historyTypes=["messageAdded"],
            labelId="INBOX",
        ).execute()
    except Exception as exc:
        error_str = str(exc)
        if "404" in error_str or "invalid" in error_str.lower():
            raise HistoryExpiredError(error_str)
        raise

    new_history_id = response.get("historyId", start_history_id)
    msg_ids = []

    for record in response.get("history", []):
        for added in record.get("messagesAdded", []):
            msg = added.get("message", {})
            labels = msg.get("labelIds", [])
            if "INBOX" in labels and "SPAM" not in labels and "TRASH" not in labels:
                msg_ids.append(msg["id"])

    return msg_ids, new_history_id


def get_message_details(service, msg_id):
    """
    Fetch metadata for a single message.
    Returns a dict with sender info, or None if the message cannot be parsed.
    """
    try:
        msg = service.users().messages().get(
            userId="me",
            id=msg_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
    except Exception as exc:
        logger.warning("Could not fetch message %s: %s", msg_id, exc)
        return None

    headers = {
        h["name"]: h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }

    from_value = headers.get("From", "").strip()
    if not from_value:
        return None

    name, email_addr = _parse_from_header(from_value)
    if not email_addr or "@" not in email_addr:
        return None

    domain = email_addr.split("@")[1]

    return {
        "gmail_id": msg_id,
        "email": email_addr,
        "name": name,
        "domain": domain,
        "subject": headers.get("Subject", "(nessun oggetto)"),
        "date": headers.get("Date", ""),
        "internal_date_ms": int(msg.get("internalDate", 0)),
    }


def _decode_mime(value):
    if not value:
        return ""
    parts = []
    for chunk, charset in _mime_decode(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(str(chunk))
    return "".join(parts)


def _parse_from_header(raw):
    """Parse 'Display Name <addr@domain>' or bare 'addr@domain'."""
    raw = _decode_mime(raw).strip()
    if "<" in raw and ">" in raw:
        name = raw[: raw.rindex("<")].strip().strip('"').strip("'")
        addr = raw[raw.rindex("<") + 1 : raw.rindex(">")].strip()
    else:
        name = ""
        addr = raw
    return (name or None), addr.lower()
