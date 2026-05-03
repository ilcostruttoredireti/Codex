import logging
import os
import re
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str):
        self._credentials_file = credentials_file
        self._token_file = token_file
        self.service = self._authenticate()

    def _authenticate(self):
        creds: Optional[Credentials] = None
        if os.path.exists(self._token_file):
            creds = Credentials.from_authorized_user_file(self._token_file, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(self._token_file, "w") as f:
                f.write(creds.to_json())
        return build("gmail", "v1", credentials=creds)

    def get_current_history_id(self) -> str:
        profile = self.service.users().getProfile(userId="me").execute()
        return str(profile["historyId"])

    def get_new_inbox_messages(
        self, history_id: str
    ) -> tuple[list[dict], str]:
        """Poll for new INBOX messages since `history_id`.

        Returns a tuple of (message_details_list, new_history_id).
        Raises HttpError with status 410 when history_id is stale.
        """
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

        new_history_id = str(response.get("historyId", history_id))
        message_ids: list[str] = []
        seen: set[str] = set()

        for record in response.get("history", []):
            for added in record.get("messagesAdded", []):
                msg = added["message"]
                mid = msg["id"]
                if mid not in seen and "INBOX" in msg.get("labelIds", []):
                    seen.add(mid)
                    message_ids.append(mid)

        details: list[dict] = []
        for mid in message_ids:
            try:
                detail = (
                    self.service.users()
                    .messages()
                    .get(
                        userId="me",
                        messageId=mid,
                        format="metadata",
                        metadataHeaders=["From", "Subject", "Date"],
                    )
                    .execute()
                )
                details.append(detail)
            except HttpError as exc:
                logger.warning(f"Cannot fetch message {mid}: {exc}")

        return details, new_history_id

    @staticmethod
    def parse_sender(message: dict) -> dict:
        headers = {
            h["name"]: h["value"]
            for h in message.get("payload", {}).get("headers", [])
        }
        from_header = headers.get("From", "").strip()
        subject = headers.get("Subject", "")
        date = headers.get("Date", "")

        # "Display Name <email>" or bare "email"
        match = re.match(r'^"?([^"<]+?)"?\s*<([^>]+)>\s*$', from_header)
        if match:
            name = match.group(1).strip()
            email = match.group(2).strip().lower()
        else:
            name = ""
            email = from_header.lower().strip()

        # Derive first/last from display name
        parts = name.split(" ", 1) if name else []
        first_name = parts[0] if parts else ""
        last_name = parts[1] if len(parts) > 1 else ""

        # Fallback: guess names from local part of email (john.doe@...)
        if not name and "@" in email:
            local = email.split("@")[0]
            local_parts = re.split(r"[._\-]", local)
            if len(local_parts) >= 2:
                first_name = local_parts[0].capitalize()
                last_name = " ".join(p.capitalize() for p in local_parts[1:])
            else:
                first_name = local_parts[0].capitalize()

        domain = email.split("@")[1] if "@" in email else ""

        return {
            "email": email,
            "name": name,
            "first_name": first_name,
            "last_name": last_name,
            "domain": domain,
            "subject": subject,
            "date": date,
            "message_id": message["id"],
        }
