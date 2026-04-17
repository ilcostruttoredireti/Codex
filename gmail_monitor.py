import os
import json
import logging
from pathlib import Path
from email.utils import parseaddr

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE = "token.json"
STATE_FILE = "state.json"

PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "live.com", "me.com", "aol.com", "protonmail.com",
    "libero.it", "tin.it", "virgilio.it", "alice.it", "tiscali.it",
    "yahoo.it", "msn.com", "ymail.com",
}

logger = logging.getLogger(__name__)


class GmailMonitor:
    def __init__(self, credentials_file: str = "credentials.json"):
        if not Path(credentials_file).exists():
            raise FileNotFoundError(
                f"Google credentials file not found: '{credentials_file}'\n"
                "Download it from Google Cloud Console → APIs & Services → Credentials."
            )
        self.credentials_file = credentials_file
        self.service = self._authenticate()
        self.user_email = self._get_user_email()
        self.history_id = self._load_state().get("history_id")

        if not self.history_id:
            self.history_id = self._get_current_history_id()
            self._save_state({"history_id": self.history_id})
            logger.info("Initialized Gmail monitor. Watching for new messages from history ID %s.", self.history_id)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _authenticate(self):
        creds = None
        if Path(TOKEN_FILE).exists():
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(TOKEN_FILE, "w") as fh:
                fh.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    def _get_user_email(self) -> str:
        profile = self.service.users().getProfile(userId="me").execute()
        return profile["emailAddress"]

    def _get_current_history_id(self) -> str:
        profile = self.service.users().getProfile(userId="me").execute()
        return profile["historyId"]

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> dict:
        if Path(STATE_FILE).exists():
            with open(STATE_FILE) as fh:
                return json.load(fh)
        return {}

    def _save_state(self, data: dict):
        state = self._load_state()
        state.update(data)
        with open(STATE_FILE, "w") as fh:
            json.dump(state, fh)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_new_emails(self) -> list[dict]:
        """Return a list of sender dicts for each new inbox message."""
        try:
            response = (
                self.service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=self.history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
        except HttpError as exc:
            # History ID expired (> 7 days old) — reset to current
            if exc.resp.status == 404:
                logger.warning("History ID expired, resetting to current position.")
                self.history_id = self._get_current_history_id()
                self._save_state({"history_id": self.history_id})
                return []
            raise

        senders = []
        for record in response.get("history", []):
            for added in record.get("messagesAdded", []):
                msg = added.get("message", {})
                labels = msg.get("labelIds", [])
                # Only inbox messages, skip sent
                if "INBOX" in labels and "SENT" not in labels:
                    info = self._extract_sender(msg["id"])
                    if info and info["email"] != self.user_email:
                        senders.append(info)

        if "historyId" in response:
            self.history_id = response["historyId"]
            self._save_state({"history_id": self.history_id})

        return senders

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extract_sender(self, message_id: str) -> dict | None:
        try:
            msg = (
                self.service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject"],
                )
                .execute()
            )
        except HttpError as exc:
            logger.error("Could not fetch message %s: %s", message_id, exc)
            return None

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        raw_from = headers.get("From", "")
        name, email = parseaddr(raw_from)

        if not email or "@" not in email:
            return None

        email = email.lower().strip()
        domain = email.split("@")[1]
        first_name, last_name = self._split_name(name.strip().strip('"'))
        company = self._domain_to_company(domain)

        return {
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "company": company,
            "domain": domain,
            "subject": headers.get("Subject", ""),
            "message_id": message_id,
        }

    @staticmethod
    def _split_name(full_name: str) -> tuple[str, str]:
        if not full_name:
            return "", ""
        parts = full_name.split(" ", 1)
        return parts[0], parts[1] if len(parts) > 1 else ""

    @staticmethod
    def _domain_to_company(domain: str) -> str:
        if domain in PERSONAL_DOMAINS:
            return ""
        # "acme.com" → "Acme"
        return domain.split(".")[0].capitalize()
