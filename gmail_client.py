import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE = "gmail_token.json"
LAST_HISTORY_FILE = "last_history_id.txt"


class GmailClient:
    def __init__(self, credentials_file: str):
        self.credentials_file = credentials_file
        self.service = self._authenticate()

    def _authenticate(self):
        creds = None
        if Path(TOKEN_FILE).exists():
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(TOKEN_FILE, "w") as f:
                f.write(creds.to_json())

        logger.info("Gmail authentication successful")
        return build("gmail", "v1", credentials=creds)

    def get_user_email(self) -> str:
        profile = self.service.users().getProfile(userId="me").execute()
        return profile.get("emailAddress", "")

    def _get_last_history_id(self) -> str | None:
        p = Path(LAST_HISTORY_FILE)
        return p.read_text().strip() or None if p.exists() else None

    def _save_history_id(self, history_id: str):
        Path(LAST_HISTORY_FILE).write_text(str(history_id))

    def get_new_messages(self, max_results: int = 50) -> list[dict]:
        last_id = self._get_last_history_id()
        if last_id:
            return self._messages_from_history(last_id, max_results)
        return self._initial_messages(max_results)

    def _initial_messages(self, max_results: int) -> list[dict]:
        logger.info("First run — fetching recent INBOX messages")
        result = (
            self.service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=max_results)
            .execute()
        )
        messages = result.get("messages", [])
        full_msgs = [m for m in (self._fetch(msg["id"]) for msg in messages) if m]

        profile = self.service.users().getProfile(userId="me").execute()
        if h := profile.get("historyId"):
            self._save_history_id(h)

        return full_msgs

    def _messages_from_history(self, start_id: str, max_results: int) -> list[dict]:
        try:
            history = (
                self.service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=start_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )

            new_ids: set[str] = set()
            for record in history.get("history", []):
                for added in record.get("messagesAdded", []):
                    new_ids.add(added["message"]["id"])

            if h := history.get("historyId"):
                self._save_history_id(h)

            return [
                m
                for m in (self._fetch(mid) for mid in list(new_ids)[:max_results])
                if m
            ]

        except Exception as exc:
            logger.warning("History fetch failed (%s) — resetting and retrying", exc)
            Path(LAST_HISTORY_FILE).unlink(missing_ok=True)
            return self._initial_messages(max_results)

    def _fetch(self, message_id: str) -> dict | None:
        try:
            return (
                self.service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
                .execute()
            )
        except Exception as exc:
            logger.error("Could not fetch message %s: %s", message_id, exc)
            return None

    @staticmethod
    def extract_sender_info(message: dict) -> dict | None:
        headers = {
            h["name"]: h["value"]
            for h in message.get("payload", {}).get("headers", [])
        }
        from_header = headers.get("From", "").strip()
        if not from_header:
            return None

        sender_email, sender_name = GmailClient._parse_from(from_header)
        if not sender_email or "@" not in sender_email:
            return None

        return {
            "email": sender_email.lower().strip(),
            "name": sender_name.strip(),
            "domain": sender_email.split("@")[-1].lower(),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
            "message_id": message.get("id", ""),
        }

    @staticmethod
    def _parse_from(header: str) -> tuple[str, str]:
        if "<" in header and ">" in header:
            name = header[: header.rfind("<")].strip().strip('"').strip("'")
            addr = header[header.rfind("<") + 1 : header.rfind(">")].strip()
            return addr, name
        return header.strip(), ""
