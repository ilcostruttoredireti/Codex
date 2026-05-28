"""Core sync loop: Gmail inbox → HubSpot contacts."""

import logging
import time
from typing import Optional

from .config import Config
from .gmail_client import GmailClient, SenderInfo
from .hubspot_client import HubSpotClient, SyncResult
from .state import SyncState, load_state, save_state

log = logging.getLogger(__name__)


def _should_skip(info: SenderInfo, config: Config) -> Optional[str]:
    if info.email in config.skip_addresses:
        return "own address"
    if info.domain in config.skip_domains:
        return f"skip domain ({info.domain})"
    if info.email.startswith("mailer-daemon"):
        return "mailer-daemon"
    if info.email.startswith("noreply") or info.email.startswith("no-reply"):
        return "no-reply address"
    return None


def _log_result(result: SyncResult):
    icon = {"created": "✅", "updated": "🔄", "skipped": "⏭"}.get(result.status, "?")
    extra = f" — {result.reason}" if result.reason else ""
    log.info(
        "%s  %-10s  %-40s  ID: %s%s",
        icon, result.status.upper(), result.email,
        result.contact_id or "—", extra,
    )


def run_once(gmail: GmailClient, hubspot: HubSpotClient,
             state: SyncState, config: Config) -> int:
    """Process all new messages since state.last_history_id. Returns count processed."""
    if not state.last_history_id:
        state.last_history_id = gmail.get_history_id()
        log.info("Initialised history ID: %s", state.last_history_id)
        return 0

    processed = 0
    seen_emails: set[str] = set()   # deduplicate senders within this run

    for msg_stub in gmail.new_messages_since(state.last_history_id):
        msg_id = msg_stub["id"]
        if state.is_processed(msg_id):
            continue

        info = gmail.get_sender_info(msg_id)
        if info is None:
            state.mark_processed(msg_id)
            continue

        skip_reason = _should_skip(info, config)
        if skip_reason:
            log.debug("Skip %s: %s", info.email, skip_reason)
            state.mark_processed(msg_id)
            continue

        if info.email in seen_emails:
            state.mark_processed(msg_id)
            continue
        seen_emails.add(info.email)

        result = hubspot.sync_sender(
            email=info.email,
            first_name=info.first_name,
            last_name=info.last_name,
            domain=info.domain,
        )
        _log_result(result)
        state.mark_processed(msg_id)
        state.last_history_id = info.history_id or state.last_history_id
        processed += 1

    return processed


def run_loop(config: Optional[Config] = None):
    """Continuous monitor loop. Blocks until interrupted."""
    if config is None:
        config = Config.from_env()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    gmail = GmailClient(config.gmail_credentials_file, config.gmail_token_file)
    hubspot = HubSpotClient(config.hubspot_token)
    state = load_state(config.state_file)

    log.info("Gmail → HubSpot sync started (poll every %ds)", config.poll_interval)

    try:
        while True:
            try:
                n = run_once(gmail, hubspot, state, config)
                if n:
                    log.info("Round complete: %d sender(s) processed", n)
                save_state(state, config.state_file)
            except Exception as exc:
                log.error("Error during sync round: %s", exc, exc_info=True)
            time.sleep(config.poll_interval)
    except KeyboardInterrupt:
        log.info("Stopped.")
        save_state(state, config.state_file)
