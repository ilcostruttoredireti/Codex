"""
Gmail → HubSpot Contact Sync
-----------------------------
Monitors Gmail inbox for new emails and syncs sender contacts to HubSpot.

Tracking mechanism:
  - Gmail label "HubSpot Synced" (Label_32) marks emails already processed.
  - Next cron run queries: in:inbox -label:HubSpot-Synced newer_than:1d

HubSpot logic:
  - Email is the unique key.
  - Existing contact → update only blank fields.
  - New contact    → create with source "Gmail" and tag "Inbound Gmail".

Sender filtering (skip list):
  - no-reply / noreply / donotreply patterns
  - nobody / mailer-daemon / postmaster / bounce
  - newsletter / notification / unsubscribe
  - Amazon order/payment confirmations
  - Any address whose local-part is purely system-generated
"""

import re
import sys


# ── Skip heuristics ─────────────────────────────────────────────────────────

SKIP_PATTERNS = [
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "notification",
    "newsletter", "unsubscribe", "nobody", "auto@",
    "conferma-spedizione", "conferma-ordine", "payments-update",
]

FREE_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "icloud.com", "me.com", "mac.com",
    "aol.com", "protonmail.com", "libero.it", "alice.it",
    "virgilio.it", "tin.it", "tiscali.it",
}


def should_skip(email: str) -> bool:
    low = email.lower()
    return any(p in low for p in SKIP_PATTERNS)


def company_from_domain(email: str) -> str:
    """Return a best-guess company name from the email domain."""
    if "@" not in email:
        return ""
    domain = email.split("@")[1].lower()
    if domain in FREE_DOMAINS:
        return ""
    root = domain.split(".")[0]
    return root.replace("-", " ").replace("_", " ").title()


def parse_name(display: str):
    """Split a display name into (first, last)."""
    parts = display.strip().split(None, 1)
    first = parts[0] if parts else ""
    last = parts[1] if len(parts) > 1 else ""
    return first, last


# ── Entry point (for CI / standalone execution) ──────────────────────────────

if __name__ == "__main__":
    print(
        "Run via Claude Code MCP cron (Gmail + HubSpot MCP tools).\n"
        "For standalone use wire up google-auth + hubspot-api-client SDKs.",
        file=sys.stderr,
    )
    sys.exit(0)
