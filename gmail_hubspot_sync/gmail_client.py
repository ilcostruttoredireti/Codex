"""Gmail API client for reading incoming emails."""

import logging
import re
from email.header import decode_header
from pathlib import Path
from typing import Generator, List, Optional

from .config import GmailConfig
from .models import ContactInfo

# Google API libraries are imported lazily inside GmailClient._build_service()
# so that the rest of the package (models, state, hubspot_client, etc.) can be
# imported and tested without a working native cryptography build.

logger = logging.getLogger(__name__)


def _decode_mime_words(s: str) -> str:
    """Decode MIME encoded-word syntax (RFC 2047)."""
    if not s:
        return ""
    parts = decode_header(s)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def _parse_from_header(from_header: str) -> tuple[Optional[str], Optional[str]]:
    """
    Parse a From header into (name, email).

    Examples:
        'John Doe <john@example.com>' → ('John Doe', 'john@example.com')
        'john@example.com'           → (None, 'john@example.com')
    """
    from_header = _decode_mime_words(from_header).strip()

    # Pattern: "Name <email>"
    match = re.match(r'^"?(.+?)"?\s*<([^>]+)>$', from_header)
    if match:
        name = match.group(1).strip().strip('"')
        addr = match.group(2).strip().lower()
        return (name if name else None, addr)

    # Plain email address
    if "@" in from_header:
        return (None, from_header.lower())

    return (None, None)


class GmailClient:
    """Authenticates with Gmail API and yields new incoming emails."""

    def __init__(self, config: GmailConfig):
        self.config = config
        self._service = None

    # ------------------------------------------------------------------ auth

    def _get_credentials(self):
        """Load or refresh OAuth2 credentials (lazy Google auth import)."""
        # Lazy imports so the module can be imported without cryptography C extensions
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        creds = None
        token_path = Path(self.config.token_file)
        creds_path = Path(self.config.credentials_file)

        if token_path.exists():
            creds = Credentials.from_authorized_user_file(
                str(token_path), self.config.scopes
            )

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not creds_path.exists():
                    raise FileNotFoundError(
                        f"File credenziali Google non trovato: {creds_path}\n"
                        "Scarica 'credentials.json' dalla Google Cloud Console."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(creds_path), self.config.scopes
                )
                creds = flow.run_local_server(port=0)

            # Save credentials for future use
            token_path.write_text(creds.to_json())

        return creds

    def _build_service(self):
        """Build and cache the Gmail API service (lazy Google API import)."""
        if self._service is None:
            from googleapiclient.discovery import build
            creds = self._get_credentials()
            self._service = build("gmail", "v1", credentials=creds)
        return self._service

    # ---------------------------------------------------------------- fetch

    def get_new_messages(
        self, processed_ids: set, label: str = "INBOX"
    ) -> Generator[dict, None, None]:
        """
        Yield raw Gmail message dicts that haven't been processed yet.

        Args:
            processed_ids: Set of already-processed message IDs.
            label: Gmail label to query (default: INBOX).
        """
        service = self._build_service()
        query = "is:unread" if label == "INBOX" else ""

        try:
            from googleapiclient.errors import HttpError
        except ImportError:
            HttpError = Exception  # fallback for environments without the library

        try:
            results = (
                service.users()
                .messages()
                .list(
                    userId="me",
                    labelIds=[label] if label != "all" else [],
                    q=query,
                    maxResults=self.config.max_results,
                )
                .execute()
            )
        except HttpError as e:
            logger.error("Errore Gmail API (list): %s", e)
            return

        messages = results.get("messages", [])
        logger.info("Gmail: trovati %d messaggi da processare", len(messages))

        for msg_ref in messages:
            msg_id = msg_ref["id"]
            if msg_id in processed_ids:
                continue

            try:
                full_msg = (
                    service.users()
                    .messages()
                    .get(userId="me", id=msg_id, format="metadata",
                         metadataHeaders=["From", "Subject", "Date"])
                    .execute()
                )
                full_msg["_parsed_id"] = msg_id
                yield full_msg
            except HttpError as e:
                logger.error("Errore Gmail API (get %s): %s", msg_id, e)

    # ----------------------------------------------------------- parse

    @staticmethod
    def extract_contact(message: dict) -> Optional[ContactInfo]:
        """Extract ContactInfo from a Gmail message dict."""
        headers = {
            h["name"]: h["value"]
            for h in message.get("payload", {}).get("headers", [])
        }
        from_header = headers.get("From", "")
        if not from_header:
            return None

        name, email_addr = _parse_from_header(from_header)
        if not email_addr or "@" not in email_addr:
            return None

        contact = ContactInfo(email=email_addr, full_name=name)
        contact.split_name()
        return contact

    @staticmethod
    def get_subject(message: dict) -> Optional[str]:
        """Return the Subject header from a Gmail message dict."""
        headers = {
            h["name"]: h["value"]
            for h in message.get("payload", {}).get("headers", [])
        }
        return headers.get("Subject")
