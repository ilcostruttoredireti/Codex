import logging
import os
import pickle
import re
from typing import Optional

from models import SenderInfo

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.labels",
]

PROCESSED_LABEL = "HubSpot-Synced"

_SKIP_PREFIXES = (
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "bounces",
    "notifications", "newsletter", "unsubscribe",
)

_CONSUMER_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "yahoo.fr",
    "hotmail.com", "hotmail.it", "hotmail.fr", "outlook.com", "outlook.it",
    "live.com", "live.it", "icloud.com", "me.com", "mac.com",
    "aol.com", "protonmail.com", "proton.me", "libero.it", "virgilio.it",
    "tiscali.it", "alice.it", "email.it", "tin.it",
})


class GmailClient:
    def __init__(self, credentials_file: str = "credentials.json", token_file: str = "token.pickle"):
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._service = None
        self._processed_label_id: Optional[str] = None

    def connect(self) -> None:
        # Lazy import to avoid C-extension issues at module load time in test envs
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        creds = self._load_or_refresh_credentials(Request, InstalledAppFlow)
        self._service = build("gmail", "v1", credentials=creds)
        self._processed_label_id = self._ensure_label(PROCESSED_LABEL)
        logger.info("Gmail client connected")

    def _load_or_refresh_credentials(self, Request, InstalledAppFlow):
        creds = None
        if os.path.exists(self._token_file):
            with open(self._token_file, "rb") as f:
                creds = pickle.load(f)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(self._credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(self._token_file, "wb") as f:
                pickle.dump(creds, f)

        return creds

    def _ensure_label(self, name: str) -> str:
        labels = self._service.users().labels().list(userId="me").execute()
        for label in labels.get("labels", []):
            if label["name"] == name:
                return label["id"]

        new_label = self._service.users().labels().create(
            userId="me",
            body={"name": name, "labelListVisibility": "labelHide", "messageListVisibility": "hide"},
        ).execute()
        logger.info("Created Gmail label: %s", name)
        return new_label["id"]

    def fetch_unsynced_messages(self, max_results: int = 50) -> list[dict]:
        query = f"in:inbox -label:{PROCESSED_LABEL}"
        result = self._service.users().messages().list(
            userId="me", q=query, maxResults=max_results
        ).execute()
        return result.get("messages", [])

    def get_message_headers(self, msg_id: str) -> Optional[dict[str, str]]:
        try:
            msg = self._service.users().messages().get(
                userId="me",
                id=msg_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            return headers
        except Exception:
            logger.exception("Failed to fetch message %s", msg_id)
            return None

    def mark_as_synced(self, msg_id: str) -> None:
        try:
            self._service.users().messages().modify(
                userId="me",
                id=msg_id,
                body={"addLabelIds": [self._processed_label_id]},
            ).execute()
        except Exception:
            logger.warning("Could not label message %s as synced", msg_id)

    @staticmethod
    def parse_sender(from_header: str) -> Optional[SenderInfo]:
        from_header = from_header.strip()

        match = re.match(r'^"?([^"<]*)"?\s*<([^>]+)>$', from_header)
        if match:
            full_name = match.group(1).strip().strip('"')
            email = match.group(2).strip().lower()
        elif re.match(r'^[^\s@]+@[^\s@]+$', from_header):
            full_name = ""
            email = from_header.lower()
        else:
            logger.debug("Unparseable From header: %s", from_header)
            return None

        if "@" not in email:
            return None

        local, domain = email.split("@", 1)

        if any(local.startswith(p) for p in _SKIP_PREFIXES):
            logger.debug("Skipping system sender: %s", email)
            return None

        name_parts = full_name.split() if full_name else []
        first_name = name_parts[0] if name_parts else None
        last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else None

        company = None
        if domain not in _CONSUMER_DOMAINS:
            company = domain.split(".")[0].capitalize()

        return SenderInfo(
            email=email,
            domain=domain,
            first_name=first_name,
            last_name=last_name,
            company=company,
        )
