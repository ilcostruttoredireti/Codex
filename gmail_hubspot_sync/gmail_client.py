import logging
import os
import re
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import GMAIL_SCOPES, PROCESSED_LABEL, IGNORED_SENDER_PREFIXES, IGNORED_DOMAINS
from .models import ContactInfo

logger = logging.getLogger(__name__)

# "Display Name <email@domain.com>" — name is everything before the last <…>
_NAMED_RE = re.compile(r'^(?P<name>.+?)\s*<(?P<email>[^>@\s]+@[^>@\s]+)>\s*$')
# Plain "email@domain.com"
_PLAIN_RE = re.compile(r'^(?P<email>[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})\s*$')


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str):
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._service = None
        self._processed_label_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        creds = None
        if os.path.exists(self._token_file):
            creds = Credentials.from_authorized_user_file(self._token_file, GMAIL_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._credentials_file, GMAIL_SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(self._token_file, "w") as fh:
                fh.write(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)
        logger.info("Gmail authenticated successfully")

    # ------------------------------------------------------------------
    # Label management
    # ------------------------------------------------------------------

    def ensure_processed_label(self) -> str:
        """Return the label ID for PROCESSED_LABEL, creating it if needed."""
        if self._processed_label_id:
            return self._processed_label_id

        labels = self._service.users().labels().list(userId="me").execute().get("labels", [])
        for label in labels:
            if label["name"] == PROCESSED_LABEL:
                self._processed_label_id = label["id"]
                return self._processed_label_id

        new_label = (
            self._service.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": PROCESSED_LABEL,
                    "labelListVisibility": "labelHide",
                    "messageListVisibility": "hide",
                },
            )
            .execute()
        )
        self._processed_label_id = new_label["id"]
        logger.info("Created Gmail label: %s (id=%s)", PROCESSED_LABEL, self._processed_label_id)
        return self._processed_label_id

    def mark_as_processed(self, message_id: str) -> None:
        label_id = self.ensure_processed_label()
        self._service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [label_id]},
        ).execute()

    # ------------------------------------------------------------------
    # Message fetching
    # ------------------------------------------------------------------

    def get_unprocessed_inbox_messages(self, max_results: int = 50) -> list[dict]:
        """Return messages from INBOX that don't have the processed label."""
        label_id = self.ensure_processed_label()
        query = f"in:inbox -label:{PROCESSED_LABEL} -from:me"
        try:
            response = (
                self._service.users()
                .messages()
                .list(userId="me", q=query, maxResults=max_results)
                .execute()
            )
            return response.get("messages", [])
        except HttpError as exc:
            logger.error("Failed to list Gmail messages: %s", exc)
            return []

    def get_message_sender(self, message_id: str) -> Optional[dict]:
        """Return {'from': str, 'message_id': str} for a message, or None on error."""
        try:
            msg = (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="metadata", metadataHeaders=["From"])
                .execute()
            )
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            from_header = headers.get("From", "")
            return {"from": from_header, "message_id": message_id}
        except HttpError as exc:
            logger.warning("Could not fetch message %s: %s", message_id, exc)
            return None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def parse_from_header(from_header: str) -> Optional[ContactInfo]:
        """Parse a From: header into a ContactInfo, or return None to skip."""
        from_header = from_header.strip()

        named = _NAMED_RE.match(from_header)
        plain = _PLAIN_RE.match(from_header)

        if named:
            raw_email = named.group("email").lower().strip()
            raw_name = named.group("name").strip().strip('"').strip("'")
        elif plain:
            raw_email = plain.group("email").lower().strip()
            raw_name = ""
        else:
            logger.debug("Cannot parse From header: %r", from_header)
            return None

        local, _, domain = raw_email.partition("@")

        # Skip ignored senders
        if domain in IGNORED_DOMAINS:
            return None
        if any(local.startswith(pfx) for pfx in IGNORED_SENDER_PREFIXES):
            return None

        # Parse name into first / last
        first_name: Optional[str] = None
        last_name: Optional[str] = None
        if raw_name and raw_name.lower() != raw_email:
            parts = raw_name.split()
            first_name = parts[0].capitalize() if parts else None
            last_name = " ".join(p.capitalize() for p in parts[1:]) if len(parts) > 1 else None

        # Derive company from domain (strip common TLDs, www, etc.)
        company = _domain_to_company(domain)

        return ContactInfo(
            email=raw_email,
            first_name=first_name,
            last_name=last_name,
            company=company,
            domain=domain,
        )


def _domain_to_company(domain: str) -> Optional[str]:
    """Best-effort: turn 'acme.com' into 'Acme'."""
    # Strip subdomains like mail., smtp., etc.
    parts = domain.split(".")
    # Remove generic public email providers
    public_providers = {
        "gmail", "yahoo", "hotmail", "outlook", "icloud",
        "protonmail", "aol", "live", "msn", "googlemail",
    }
    core = parts[-2] if len(parts) >= 2 else parts[0]
    if core.lower() in public_providers:
        return None
    return core.capitalize()
