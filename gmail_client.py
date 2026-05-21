import os
import re
import json
import logging
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
STATE_FILE = ".gmail_state.json"


class GmailClient:
    def __init__(self, credentials_file: str = "credentials.json", token_file: str = "token.json"):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = self._authenticate()
        self._state = self._load_state()

    # -------------------------------------------------------------------------
    # Auth
    # -------------------------------------------------------------------------

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
            with open(self.token_file, "w") as f:
                f.write(creds.to_json())
        return build("gmail", "v1", credentials=creds)

    # -------------------------------------------------------------------------
    # State persistence (historyId)
    # -------------------------------------------------------------------------

    def _load_state(self) -> dict:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                return json.load(f)
        return {}

    def _save_state(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self._state, f)

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def get_new_messages(self) -> list[dict]:
        """Return parsed sender info for every new INBOX message since last run."""
        history_id = self._state.get("history_id")

        if not history_id:
            # First run: record current historyId without fetching old mail
            profile = self.service.users().getProfile(userId="me").execute()
            self._state["history_id"] = str(profile["historyId"])
            self._save_state()
            logger.info("Prima esecuzione: history_id salvato. Le nuove email saranno rilevate al prossimo ciclo.")
            return []

        try:
            response = (
                self.service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
        except HttpError as e:
            if e.resp.status == 404:
                # historyId expired — reset
                logger.warning("historyId scaduto, reset dello stato.")
                profile = self.service.users().getProfile(userId="me").execute()
                self._state["history_id"] = str(profile["historyId"])
                self._save_state()
                return []
            raise

        new_history_id = response.get("historyId", history_id)
        self._state["history_id"] = str(new_history_id)
        self._save_state()

        messages = []
        for record in response.get("history", []):
            for added in record.get("messagesAdded", []):
                msg_stub = added["message"]
                label_ids = msg_stub.get("labelIds", [])
                if "INBOX" not in label_ids:
                    continue
                parsed = self._fetch_and_parse(msg_stub["id"])
                if parsed:
                    messages.append(parsed)

        return messages

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _fetch_and_parse(self, msg_id: str) -> Optional[dict]:
        try:
            msg = self.service.users().messages().get(userId="me", id=msg_id, format="metadata",
                                                       metadataHeaders=["From", "Subject", "Date"]).execute()
        except HttpError as e:
            logger.error("Impossibile scaricare messaggio %s: %s", msg_id, e)
            return None

        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        from_header = headers.get("From", "")
        name, email_addr = self._parse_from(from_header)

        if not email_addr:
            logger.debug("Nessuna email valida in From: %s", from_header)
            return None

        return {
            "id": msg_id,
            "email": email_addr.lower(),
            "name": name,
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
        }

    @staticmethod
    def _parse_from(from_header: str) -> tuple[str, str]:
        """Parse 'Name <email>' or bare 'email' → (name, email)."""
        from_header = from_header.strip()
        # "Display Name" <email@domain.com>
        m = re.match(r'^"?([^"<]*?)"?\s*<([^>]+)>\s*$', from_header)
        if m:
            return m.group(1).strip(), m.group(2).strip()
        # Bare address
        if re.match(r'^[\w.+%-]+@[\w.-]+\.\w+$', from_header):
            return "", from_header
        return "", ""
