import json
import logging
import time
from pathlib import Path
from typing import Iterator

from .config import Config
from .gmail_client import GmailClient
from .hubspot_client import HubSpotClient
from .models import ContactInfo, SyncResult, SyncStatus

log = logging.getLogger(__name__)


def _load_state(state_file: str) -> dict:
    p = Path(state_file)
    if p.exists():
        return json.loads(p.read_text())
    return {}


def _save_state(state_file: str, state: dict) -> None:
    Path(state_file).write_text(json.dumps(state, indent=2))


def run_once(cfg: Config) -> list[SyncResult]:
    """Fetch new Gmail senders and sync them to HubSpot. Returns results for this run."""
    state = _load_state(cfg.state_file)
    after_ts = state.get("last_run_timestamp")

    gmail = GmailClient(cfg)
    hubspot = HubSpotClient(cfg)

    results: list[SyncResult] = []

    log.info("Fetching Gmail senders (after_ts=%s) …", after_ts)
    for contact in gmail.fetch_new_senders(after_timestamp=after_ts):
        result = hubspot.upsert_contact(contact)
        results.append(result)
        _log_result(result)

    # Persist timestamp so the next run only processes newer messages
    import time as _time
    state["last_run_timestamp"] = int(_time.time())
    _save_state(cfg.state_file, state)

    return results


def run_loop(cfg: Config) -> Iterator[list[SyncResult]]:
    """Continuously poll Gmail and sync contacts. Yields results from each cycle."""
    log.info(
        "Starting Gmail→HubSpot sync loop (poll every %ds) …",
        cfg.poll_interval_seconds,
    )
    while True:
        try:
            results = run_once(cfg)
            yield results
            _print_summary(results)
        except Exception as e:
            log.error("Sync cycle error: %s", e, exc_info=True)
        time.sleep(cfg.poll_interval_seconds)


def _log_result(result: SyncResult) -> None:
    icon = {"Creato": "✚", "Aggiornato": "↑", "Ignorato": "–"}.get(result.status, "?")
    log.info(
        "%s [%s] %s  (ID: %s)",
        icon,
        result.status,
        result.email,
        result.hubspot_contact_id or "n/a",
    )


def _print_summary(results: list[SyncResult]) -> None:
    created = sum(1 for r in results if r.status == SyncStatus.CREATED)
    updated = sum(1 for r in results if r.status == SyncStatus.UPDATED)
    ignored = sum(1 for r in results if r.status == SyncStatus.IGNORED)
    log.info(
        "Cycle complete — Creati: %d | Aggiornati: %d | Ignorati: %d",
        created,
        updated,
        ignored,
    )
