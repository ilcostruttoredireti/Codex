"""Gmail API client: authentication, message listing, sender extraction."""

import email.utils
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import Config

log = logging.getLogger(__name__)

_NOREPLY_PATTERN = re.compile(
    r"(no[-_]?reply|noreply|do[-_]?not[-_]?reply|mailer-daemon|postmaster|bounce|"
    r"notification|automated|auto[-_]?generated)",
    re.IGNORECASE,
)


@dataclass
class SenderInfo:
    raw_from: str
    email: str
    firstname: str
    lastname: str
    display_name: str
    domain: str
    company: str       # derived from domain
    message_id: str
    thread_id: str
    subject: str
    received_at: str   # RFC-2822 date header value


class GmailClient:
    def __init__(self) -> None:
        self._service = self._authenticate()
        self._synced_label_id: Optional[str] = None

    # ── Auth ─────────────────────────────────────────────────────────────────

    def _authenticate(self):
        creds: Optional[Credentials] = None
        token_path = Path(Config.GMAIL_TOKEN_FILE)

        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), Config.GMAIL_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    Config.GMAIL_CREDENTIALS_FILE, Config.GMAIL_SCOPES
                )
                creds = flow.run_local_server(port=0)
            with token_path.open("w") as fh:
                fh.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    # ── Label management ──────────────────────────────────────────────────────

    def _ensure_synced_label(self) -> str:
        if self._synced_label_id:
            return self._synced_label_id

        result = self._service.users().labels().list(userId="me").execute()
        for label in result.get("labels", []):
            if label["name"] == Config.GMAIL_SYNCED_LABEL:
                self._synced_label_id = label["id"]
                return self._synced_label_id

        # Create it if missing
        created = (
            self._service.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": Config.GMAIL_SYNCED_LABEL,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            .execute()
        )
        self._synced_label_id = created["id"]
        log.info("Created Gmail label '%s' (id=%s)", Config.GMAIL_SYNCED_LABEL, self._synced_label_id)
        return self._synced_label_id

    def apply_synced_label(self, message_id: str) -> None:
        try:
            label_id = self._ensure_synced_label()
            self._service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"addLabelIds": [label_id]},
            ).execute()
        except HttpError as exc:
            log.warning("Could not apply label to message %s: %s", message_id, exc)

    # ── Message fetching ──────────────────────────────────────────────────────

    def _list_message_ids(self, query: str, max_results: int = 500) -> list[str]:
        ids: list[str] = []
        page_token: Optional[str] = None

        while True:
            kwargs: dict = {"userId": "me", "q": query, "maxResults": min(max_results - len(ids), 100)}
            if page_token:
                kwargs["pageToken"] = page_token

            try:
                response = self._service.users().messages().list(**kwargs).execute()
            except HttpError as exc:
                log.error("Gmail list error: %s", exc)
                break

            for msg in response.get("messages", []):
                ids.append(msg["id"])

            page_token = response.get("nextPageToken")
            if not page_token or len(ids) >= max_results:
                break

        return ids

    def _get_header(self, headers: list[dict], name: str) -> str:
        for h in headers:
            if h["name"].lower() == name.lower():
                return h["value"]
        return ""

    def _fetch_sender_info(self, message_id: str) -> Optional[SenderInfo]:
        try:
            msg = (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="metadata",
                     metadataHeaders=["From", "Subject", "Date"])
                .execute()
            )
        except HttpError as exc:
            log.warning("Could not fetch message %s: %s", message_id, exc)
            return None

        headers = msg.get("payload", {}).get("headers", [])
        raw_from = self._get_header(headers, "From")
        subject = self._get_header(headers, "Subject")
        date_header = self._get_header(headers, "Date")

        if not raw_from:
            return None

        display_name, addr = email.utils.parseaddr(raw_from)
        addr = addr.strip().lower()
        if not addr or "@" not in addr:
            return None

        domain = addr.split("@", 1)[1]
        firstname, lastname = _split_name(display_name)
        company = _company_from_domain(domain)

        return SenderInfo(
            raw_from=raw_from,
            email=addr,
            firstname=firstname,
            lastname=lastname,
            display_name=display_name,
            domain=domain,
            company=company,
            message_id=message_id,
            thread_id=msg.get("threadId", ""),
            subject=subject,
            received_at=date_header,
        )

    # ── Public iterator ───────────────────────────────────────────────────────

    def new_senders(self, after_timestamp: Optional[str] = None) -> Iterator[SenderInfo]:
        """
        Yield SenderInfo for every inbox message not yet labelled as synced,
        optionally restricted to messages newer than `after_timestamp`
        (an epoch-second string, e.g. "1718000000").
        """
        query_parts = ["in:inbox", f"-label:{Config.GMAIL_SYNCED_LABEL}"]
        if after_timestamp:
            query_parts.append(f"after:{after_timestamp}")

        query = " ".join(query_parts)
        log.debug("Gmail query: %s", query)

        ids = self._list_message_ids(query)
        log.info("Found %d un-synced inbox messages.", len(ids))

        for mid in ids:
            info = self._fetch_sender_info(mid)
            if info is None:
                continue
            if _should_skip(info):
                log.debug("Skipping %s (%s)", info.email, "noreply or free domain")
                continue
            yield info


# ── Helpers ───────────────────────────────────────────────────────────────────

def _split_name(display_name: str) -> tuple[str, str]:
    """Return (firstname, lastname) from a display name string."""
    parts = display_name.strip().split(None, 2)
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _company_from_domain(domain: str) -> str:
    """
    Best-effort: strip TLD and capitalise.
    company.co.uk → Company, mail.bigcorp.com → Bigcorp
    """
    # Remove well-known subdomains
    parts = domain.lower().split(".")
    # Drop the TLD(s); keep the last meaningful segment before TLDs
    tlds = {"com", "net", "org", "io", "co", "uk", "de", "fr", "it", "eu", "gov", "edu"}
    # Walk right-to-left, skip TLDs
    significant = [p for p in parts if p not in tlds]
    if not significant:
        return domain
    return significant[-1].capitalize()


def _should_skip(info: SenderInfo) -> bool:
    if info.domain in Config.SKIP_FREE_DOMAINS:
        return True
    if Config.SKIP_NOREPLY and _NOREPLY_PATTERN.search(info.email):
        return True
    return False
