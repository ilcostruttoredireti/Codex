"""
Main orchestrator: continuously polls Gmail and syncs contacts to HubSpot.
Run with:  python -m gmail_hubspot_sync.main
"""
import logging
import time
from collections import defaultdict

from .config import (
    POLL_INTERVAL_SECONDS,
    LOOKBACK_DAYS,
    SKIP_DOMAINS,
    OWN_EMAILS,
)
from .gmail_reader import build_service, fetch_recent_messages, extract_header
from .contact_parser import parse_sender, should_skip
from .hubspot_sync import sync_contact

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def run_once() -> list[dict]:
    """
    Perform a single pass: read Gmail inbox → parse senders → sync to HubSpot.
    Returns a list of result dicts per processed email.
    """
    service = build_service()
    results: list[dict] = []
    seen_emails: set[str] = set()

    for message in fetch_recent_messages(service, days=LOOKBACK_DAYS):
        from_header = extract_header(message, "From")
        snippet = message.get("snippet", "")
        subject = extract_header(message, "Subject")

        contact = parse_sender(from_header, snippet)
        if contact is None:
            continue

        email = contact.email
        if should_skip(email, SKIP_DOMAINS, OWN_EMAILS):
            logger.debug("Skipped (system/own): %s", email)
            continue

        if email in seen_emails:
            logger.debug("Skipped (duplicate in this run): %s", email)
            continue
        seen_emails.add(email)

        logger.info("Processing sender: %s (%s)", email, subject[:60])
        result = sync_contact(contact)
        result["subject"] = subject
        results.append(result)

        _print_result(result)

    return results


def _print_result(r: dict) -> None:
    status_icon = {"created": "✅", "updated": "🔄", "ignored": "⏭️", "error": "❌"}.get(
        r["status"], "?"
    )
    print(
        f"{status_icon} [{r['status'].upper():8}] {r['email']:50s}  "
        f"HubSpot ID: {r.get('hubspot_id', 'N/A')}"
    )


def main() -> None:
    logger.info(
        "Gmail→HubSpot sync started. Poll every %ds, lookback %dd.",
        POLL_INTERVAL_SECONDS,
        LOOKBACK_DAYS,
    )
    while True:
        logger.info("--- Starting sync pass ---")
        try:
            results = run_once()
            counts: dict = defaultdict(int)
            for r in results:
                counts[r["status"]] += 1
            logger.info(
                "Pass complete. Created: %d | Updated: %d | Ignored: %d | Errors: %d",
                counts["created"],
                counts["updated"],
                counts["ignored"],
                counts["error"],
            )
        except Exception:
            logger.exception("Error during sync pass")

        logger.info("Waiting %ds before next pass...", POLL_INTERVAL_SECONDS)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
