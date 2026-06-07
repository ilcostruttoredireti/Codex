import os
import re
import logging
from email.utils import parseaddr, parsedate_to_datetime
from datetime import datetime, timezone, timedelta

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Domains that generate automated/noreply mail — skip syncing these
_IGNORED_DOMAINS_DEFAULT = {
    "noreply.com",
    "no-reply.com",
    "notifications.google.com",
    "mailer-daemon.googlemail.com",
    "accounts.google.com",
    "bounce.google.com",
    "mail.gmail.com",
}


class GmailMonitor:
    def __init__(self, credentials_file: str, token_file: str, ignored_domains: set[str] | None = None):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.ignored_domains = (ignored_domains or set()) | _IGNORED_DOMAINS_DEFAULT
        self._service = None

    def _authenticate(self):
        creds = None
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(self.token_file, "w") as token:
                token.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    @property
    def service(self):
        if self._service is None:
            self._service = self._authenticate()
        return self._service

    def get_new_messages(self, since_iso: str | None = None) -> list[dict]:
        """
        Returns inbox messages received after `since_iso` (ISO 8601).
        Falls back to last 24h when no timestamp is provided.
        """
        if since_iso:
            dt = datetime.fromisoformat(since_iso)
            # Gmail uses epoch seconds in the `after:` operator
            epoch = int(dt.timestamp())
            query = f"in:inbox after:{epoch}"
        else:
            yesterday = datetime.now(timezone.utc) - timedelta(hours=24)
            epoch = int(yesterday.timestamp())
            query = f"in:inbox after:{epoch}"

        messages = []
        page_token = None

        while True:
            params = {"userId": "me", "q": query, "maxResults": 100}
            if page_token:
                params["pageToken"] = page_token

            result = self.service.users().messages().list(**params).execute()
            batch = result.get("messages", [])
            messages.extend(batch)

            page_token = result.get("nextPageToken")
            if not page_token:
                break

        return messages

    def get_message_sender(self, message_id: str) -> dict | None:
        """
        Fetches the From header for a message and returns parsed sender info.
        Returns None if the sender should be ignored.
        """
        try:
            msg = (
                self.service.users()
                .messages()
                .get(userId="me", id=message_id, format="metadata", metadataHeaders=["From", "Date"])
                .execute()
            )
        except Exception as exc:
            logger.warning("Failed to fetch message %s: %s", message_id, exc)
            return None

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        raw_from = headers.get("From", "")
        raw_date = headers.get("Date", "")

        name, email_addr = parseaddr(raw_from)
        email_addr = email_addr.lower().strip()

        if not email_addr or "@" not in email_addr:
            return None

        domain = email_addr.split("@", 1)[1]

        if domain in self.ignored_domains:
            logger.debug("Skipping ignored domain: %s", domain)
            return None

        # Resolve first / last name from display name
        first_name, last_name = _split_name(name)

        # Derive company name from domain (strip TLD and common prefixes)
        company = _company_from_domain(domain)

        received_at = None
        if raw_date:
            try:
                received_at = parsedate_to_datetime(raw_date).isoformat()
            except Exception:
                pass

        return {
            "message_id": message_id,
            "email": email_addr,
            "name": name,
            "first_name": first_name,
            "last_name": last_name,
            "domain": domain,
            "company": company,
            "received_at": received_at,
        }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _split_name(display_name: str) -> tuple[str, str]:
    """Split 'First Last' into (first, last). Handles edge cases gracefully."""
    cleaned = display_name.strip().strip('"').strip("'")
    parts = cleaned.split(None, 1)  # split on first whitespace
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


_COMMON_PREFIXES = {"mail", "email", "info", "hello", "contact", "support", "noreply", "no-reply"}


def _company_from_domain(domain: str) -> str:
    """
    Derive a human-readable company name from a domain.
    e.g. 'acme-corp.co.uk' → 'Acme Corp'
    """
    # Remove known TLDs naively: keep only the second-level domain
    parts = domain.lower().split(".")
    # Use the leftmost non-trivial part
    base = parts[0] if parts[0] not in _COMMON_PREFIXES else (parts[1] if len(parts) > 1 else parts[0])
    # Replace hyphens/underscores with spaces and title-case
    return re.sub(r"[-_]", " ", base).title()
