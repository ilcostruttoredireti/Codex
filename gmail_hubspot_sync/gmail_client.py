"""
Gmail API client – handles OAuth flow, message fetching and label management.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Generator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import config
from utils import company_from_domain, extract_sender, parse_name, should_ignore


@dataclass
class SenderInfo:
    email: str
    first_name: str
    last_name: str
    full_name: str
    domain: str
    company: str  # derived from domain when no explicit company name is available
    message_id: str
    thread_id: str
    subject: str


def _get_credentials() -> Credentials:
    creds: Credentials | None = None

    if os.path.exists(config.GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(
            config.GMAIL_TOKEN_FILE, config.GMAIL_SCOPES
        )

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                config.GMAIL_CREDENTIALS_FILE, config.GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(config.GMAIL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return creds




class GmailClient:
    def __init__(self) -> None:
        creds = _get_credentials()
        self._service = build("gmail", "v1", credentials=creds)
        self._label_id: str | None = None  # cache for PROCESSED_LABEL id

    # ── label management ──────────────────────────────────────────────────────

    def _get_or_create_label(self) -> str:
        if self._label_id:
            return self._label_id

        labels = self._service.users().labels().list(userId="me").execute()
        for lbl in labels.get("labels", []):
            if lbl["name"] == config.PROCESSED_LABEL:
                self._label_id = lbl["id"]
                return self._label_id

        # create it
        body = {
            "name": config.PROCESSED_LABEL,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }
        created = self._service.users().labels().create(userId="me", body=body).execute()
        self._label_id = created["id"]
        return self._label_id

    def mark_processed(self, message_id: str) -> None:
        label_id = self._get_or_create_label()
        self._service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [label_id]},
        ).execute()

    # ── state (history ID) ────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        if os.path.exists(config.STATE_FILE):
            with open(config.STATE_FILE) as f:
                return json.load(f)
        return {}

    def _save_state(self, state: dict) -> None:
        with open(config.STATE_FILE, "w") as f:
            json.dump(state, f)

    # ── message fetching ──────────────────────────────────────────────────────

    def fetch_new_messages(self) -> Generator[SenderInfo, None, None]:
        """
        Yield SenderInfo for every new inbox message not yet labelled as
        processed. Uses the Gmail history API when a previous history ID exists,
        falls back to a full inbox scan on first run.
        """
        state = self._load_state()
        processed_ids: set[str] = set(state.get("processed_ids", []))
        last_history_id: str | None = state.get("history_id")

        processed_label_id = self._get_or_create_label()
        new_history_id: str | None = None

        if last_history_id:
            message_ids = self._history_message_ids(last_history_id, new_ids=[])
        else:
            message_ids = self._full_inbox_message_ids(processed_label_id)

        for msg_id in message_ids:
            if msg_id in processed_ids:
                continue

            sender = self._parse_message(msg_id)
            if sender is None:
                continue

            if new_history_id is None:
                # capture the latest history ID from the first message
                msg_meta = (
                    self._service.users()
                    .messages()
                    .get(userId="me", id=msg_id, format="minimal")
                    .execute()
                )
                new_history_id = msg_meta.get("historyId")

            yield sender

            processed_ids.add(msg_id)

        # persist state
        state["processed_ids"] = list(processed_ids)
        if new_history_id:
            state["history_id"] = new_history_id
        self._save_state(state)

    def _full_inbox_message_ids(self, processed_label_id: str) -> list[str]:
        """Return all inbox message IDs not yet carrying the processed label."""
        ids: list[str] = []
        page_token = None
        while True:
            kwargs: dict = {
                "userId": "me",
                "labelIds": ["INBOX"],
                # exclude already-processed messages
                "q": f"-label:{config.PROCESSED_LABEL}",
                "maxResults": 100,
            }
            if page_token:
                kwargs["pageToken"] = page_token

            resp = self._service.users().messages().list(**kwargs).execute()
            ids.extend(m["id"] for m in resp.get("messages", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return ids

    def _history_message_ids(self, start_history_id: str, new_ids: list) -> list[str]:
        """Use the history API to find messages added since last run."""
        ids: list[str] = []
        page_token = None
        while True:
            kwargs: dict = {
                "userId": "me",
                "startHistoryId": start_history_id,
                "historyTypes": ["messageAdded"],
                "labelId": "INBOX",
                "maxResults": 100,
            }
            if page_token:
                kwargs["pageToken"] = page_token
            try:
                resp = self._service.users().history().list(**kwargs).execute()
            except Exception:
                # historyId expired → fall back to full scan
                return self._full_inbox_message_ids("")
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    ids.append(added["message"]["id"])
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return ids

    def _parse_message(self, message_id: str) -> SenderInfo | None:
        try:
            msg = (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="metadata",
                     metadataHeaders=["From", "Subject"])
                .execute()
            )
        except Exception:
            return None

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        from_header = headers.get("From", "")
        subject = headers.get("Subject", "(no subject)")

        display_name, email = extract_sender(from_header)
        if not email or "@" not in email:
            return None

        if should_ignore(email):
            return None

        _, _, domain = email.partition("@")
        first_name, last_name = parse_name(display_name)
        company = company_from_domain(domain)

        return SenderInfo(
            email=email,
            first_name=first_name,
            last_name=last_name,
            full_name=display_name or email,
            domain=domain,
            company=company,
            message_id=message_id,
            thread_id=msg.get("threadId", ""),
            subject=subject,
        )
