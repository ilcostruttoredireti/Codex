import logging
import os
import re
from email.header import decode_header
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from . import config
from .state import SyncState

logger = logging.getLogger(__name__)


def get_gmail_service():
    """Authenticate and return a Gmail API service object."""
    creds: Optional[Credentials] = None

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
        with open(config.GMAIL_TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _decode_header_value(value: str) -> str:
    parts = decode_header(value)
    decoded: list[str] = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded).strip()


def parse_sender(from_header: str) -> dict:
    """
    Parse a 'From' header into structured sender fields.

    Handles formats:
      - "First Last <user@domain.com>"
      - '"First Last" <user@domain.com>'
      - "user@domain.com"
    """
    from_header = _decode_header_value(from_header)

    match = re.match(r'^"?([^"<]+?)"?\s*<([^>]+)>$', from_header.strip())
    if match:
        full_name = match.group(1).strip()
        email_addr = match.group(2).strip().lower()
    else:
        full_name = ""
        email_addr = from_header.strip().lower()

    # Normalise email
    email_addr = re.sub(r"\s+", "", email_addr)

    domain = email_addr.split("@")[-1] if "@" in email_addr else ""

    # Split name into first / last
    name_parts = full_name.split() if full_name else []
    firstname = name_parts[0] if name_parts else ""
    lastname = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    # Derive company from domain for business addresses
    company = ""
    if domain and domain not in config.PERSONAL_EMAIL_DOMAINS:
        # Strip TLD(s) and capitalise: "acme.co.uk" → "Acme"
        company = domain.split(".")[0].capitalize()

    return {
        "email": email_addr,
        "full_name": full_name,
        "firstname": firstname,
        "lastname": lastname,
        "domain": domain,
        "company": company,
    }


def _is_ignored_sender(email_addr: str) -> bool:
    return any(pattern in email_addr for pattern in config.IGNORED_SENDER_PATTERNS)


def fetch_message_sender(service, message_id: str) -> Optional[dict]:
    """Retrieve a Gmail message's sender metadata."""
    try:
        msg = service.users().messages().get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()

        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        from_header = headers.get("From", "")
        if not from_header:
            return None

        sender = parse_sender(from_header)
        sender["subject"] = headers.get("Subject", "")
        sender["date"] = headers.get("Date", "")
        sender["message_id"] = message_id
        return sender

    except Exception as exc:
        logger.warning("Impossibile leggere messaggio %s: %s", message_id, exc)
        return None


def get_new_inbox_message_ids(service, state: SyncState) -> list[str]:
    """
    Return IDs of inbox messages received since the last recorded history_id.

    On the very first call the current history_id is seeded and an empty list
    is returned, so only emails arriving *after* the script starts are synced.
    """
    try:
        profile = service.users().getProfile(userId="me").execute()
        current_history_id: str = profile["historyId"]

        if not state.history_id:
            logger.info("Prima esecuzione: seed history_id=%s", current_history_id)
            state.history_id = current_history_id
            return []

        message_ids: list[str] = []
        page_token: Optional[str] = None

        try:
            while True:
                kwargs: dict = {
                    "userId": "me",
                    "startHistoryId": state.history_id,
                    "historyTypes": ["messageAdded"],
                }
                if page_token:
                    kwargs["pageToken"] = page_token

                response = service.users().history().list(**kwargs).execute()

                for record in response.get("history", []):
                    for added in record.get("messagesAdded", []):
                        msg = added.get("message", {})
                        if "INBOX" in msg.get("labelIds", []):
                            message_ids.append(msg["id"])

                page_token = response.get("nextPageToken")
                if not page_token:
                    break

        except Exception as exc:
            # historyId can expire if the gap is > 30 days
            logger.warning("History ID scaduto, re-seed: %s", exc)
            state.history_id = current_history_id
            return []

        state.history_id = current_history_id
        logger.debug("Trovati %d nuovi messaggi in inbox", len(message_ids))
        return message_ids

    except Exception as exc:
        logger.error("Errore nel recupero della history Gmail: %s", exc)
        return []
