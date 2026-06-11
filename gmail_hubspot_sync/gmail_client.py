"""Gmail API wrapper — lists inbox messages and extracts sender info."""
import os
import pickle
from datetime import datetime, timedelta, timezone
from typing import Iterator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from . import config
from .contact_parser import Contact, extract_forwarded_contacts, parse_sender


def _get_credentials() -> Credentials:
    creds = None
    if os.path.exists(config.GMAIL_TOKEN_FILE):
        with open(config.GMAIL_TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                config.GMAIL_CREDENTIALS_FILE, config.GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(config.GMAIL_TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)
    return creds


def _build_service():
    return build("gmail", "v1", credentials=_get_credentials())


def _is_system_sender(email: str) -> bool:
    email_lower = email.lower()
    if email_lower in config.SKIP_SENDERS or email_lower in config.OWN_EMAILS:
        return True
    system_prefixes = ("mailer-daemon", "no-reply@", "noreply@", "postmaster@")
    return any(email_lower.startswith(p) for p in system_prefixes)


def iter_inbox_contacts(days_back: int = None) -> Iterator[tuple[Contact, str]]:
    """Yield (Contact, message_id) for each unique sender found in inbox."""
    if days_back is None:
        days_back = config.DAYS_LOOKBACK

    service = _build_service()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    after_ts = int(cutoff.timestamp())
    query = f"in:inbox after:{after_ts} -from:me"

    seen_emails: set[str] = set()
    page_token = None

    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 50}
        if page_token:
            kwargs["pageToken"] = page_token

        result = service.users().messages().list(**kwargs).execute()
        messages = result.get("messages", [])

        for msg_ref in messages:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=msg_ref["id"], format="metadata",
                     metadataHeaders=["From", "Subject"])
                .execute()
            )
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            from_header = headers.get("From", "")
            snippet = msg.get("snippet", "")
            label_ids = msg.get("labelIds", [])

            if "INBOX" not in label_ids:
                continue

            contact = parse_sender(from_header)
            if contact and not _is_system_sender(contact.email):
                if contact.email not in seen_emails:
                    seen_emails.add(contact.email)
                    yield contact, msg_ref["id"]

            # also try to extract forwarded senders from snippet
            for fwd_contact in extract_forwarded_contacts(snippet):
                if not _is_system_sender(fwd_contact.email) and fwd_contact.email not in seen_emails:
                    seen_emails.add(fwd_contact.email)
                    yield fwd_contact, msg_ref["id"]

        page_token = result.get("nextPageToken")
        if not page_token:
            break
