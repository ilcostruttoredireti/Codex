import json
import logging
import os
from datetime import datetime
from typing import Optional

from config import Config
from contact_extractor import parse_from_header
from gmail_client import GmailClient, HistoryExpiredError
from hubspot_client import HubSpotClient

logger = logging.getLogger(__name__)

_DIVIDER = "-" * 72


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class GmailHubSpotSyncer:
    def __init__(self, config: Config):
        self._config = config
        self._gmail = GmailClient(
            config.gmail_credentials_path, config.gmail_token_path
        )
        self._hubspot = HubSpotClient(config.hubspot_access_token)
        self._state = self._load_state()

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> dict:
        if os.path.exists(self._config.state_file):
            try:
                with open(self._config.state_file) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                logger.warning("Could not read state file — starting fresh")
        return {}

    def _save_state(self) -> None:
        with open(self._config.state_file, "w") as f:
            json.dump(self._state, f, indent=2)

    # ------------------------------------------------------------------
    # History-ID bookkeeping
    # ------------------------------------------------------------------

    def _get_history_id(self) -> str:
        if "history_id" not in self._state:
            profile = self._gmail.get_profile()
            history_id: str = profile["historyId"]
            self._state["history_id"] = history_id
            self._save_state()
            logger.info("Initialized Gmail history at ID %s", history_id)
        return self._state["history_id"]

    def _reset_history_id(self) -> None:
        self._state.pop("history_id", None)
        self._save_state()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_once(self) -> None:
        """Run a single poll cycle: fetch new messages and sync contacts."""
        history_id = self._get_history_id()

        try:
            message_ids, new_history_id = self._gmail.get_new_message_ids(
                history_id
            )
        except HistoryExpiredError:
            logger.warning("Gmail historyId expired — re-initializing")
            self._reset_history_id()
            return

        if not message_ids:
            logger.debug("No new inbox messages since historyId %s", history_id)
            self._state["history_id"] = new_history_id
            self._save_state()
            return

        logger.info("Found %d new message(s) to process", len(message_ids))

        for msg_id in message_ids:
            try:
                self._process_message(msg_id)
            except Exception as exc:
                logger.error(
                    "Error processing message %s: %s", msg_id, exc, exc_info=True
                )

        self._state["history_id"] = new_history_id
        self._save_state()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _process_message(self, message_id: str) -> None:
        headers = self._gmail.get_message_headers(message_id)
        from_header = headers.get("from", "")

        if not from_header:
            logger.debug("Message %s has no From header — skipped", message_id)
            self._print_row("IGNORATO", "(nessun mittente)", None, "nessun From")
            return

        contact = parse_from_header(from_header)
        if contact is None:
            logger.info(
                "Message %s skipped — automated/invalid sender: %r",
                message_id,
                from_header,
            )
            self._print_row("IGNORATO", from_header, None, "no-reply / indirizzo non valido")
            return

        result = self._hubspot.sync_contact(contact, headers)
        self._print_row(
            result.status.upper(),
            result.email,
            result.contact_id,
            result.reason or "",
        )
        self._append_log(result.status, result.email, result.contact_id)

    def _print_row(
        self,
        status: str,
        email: str,
        contact_id: Optional[str],
        note: str = "",
    ) -> None:
        id_str = contact_id or "—"
        note_str = f"  ({note})" if note else ""
        print(
            f"[{_now()}]  {status:<10}  {email:<42}  ID: {id_str}{note_str}"
        )

    def _append_log(
        self, status: str, email: str, contact_id: Optional[str]
    ) -> None:
        log = self._state.setdefault("log", [])
        log.append(
            {
                "ts": datetime.now().isoformat(),
                "status": status,
                "email": email,
                "contact_id": contact_id,
            }
        )
        # Avoid unbounded growth
        self._state["log"] = log[-2000:]
