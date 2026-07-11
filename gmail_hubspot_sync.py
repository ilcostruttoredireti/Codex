"""
Gmail → HubSpot Contact Sync
Monitors inbound Gmail messages and upserts senders as HubSpot contacts.

Designed to run inside a Claude Code session that has both the Gmail MCP
and HubSpot MCP servers connected.  The actual API calls are made by
Claude using those tools; this file documents the logic, provides the
filtering rules, and stores the run history in sync_log.json.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

LOG_FILE = Path(__file__).with_name("sync_log.json")

# ---------------------------------------------------------------------------
# Sender filtering
# ---------------------------------------------------------------------------

AUTOMATED_PATTERNS = re.compile(
    r"(no-?reply|noreply|notifications?|pinbot|confirm@|"
    r"messaging-digest|do-?not-?reply|mailer-daemon|"
    r"bounce|postmaster|feedback@|alert@)",
    re.IGNORECASE,
)

AUTOMATED_DOMAINS = {
    "youtube.com",
    "linkedin.com",
    "discord.com",
    "mailchimp.com",
    "vercel.com",
    "openai.com",
    "bybit.com",
    "pinterest.com",
    "canva.com",
    "facebook.com",
    "twitter.com",
    "instagram.com",
    "tiktok.com",
}


def is_automated(sender_email: str) -> bool:
    """Return True when the sender is a system/automated address."""
    local, _, domain = sender_email.partition("@")
    if domain.lower() in AUTOMATED_DOMAINS:
        return True
    if AUTOMATED_PATTERNS.search(local):
        return True
    return False


# ---------------------------------------------------------------------------
# Name / company helpers
# ---------------------------------------------------------------------------

def domain_to_company(domain: str) -> str:
    """Convert a domain into a readable company name heuristic."""
    base = domain.rsplit(".", 2)[0]          # strip TLD(s)
    # camelCase / PascalCase → insert spaces
    base = re.sub(r"([a-z])([A-Z])", r"\1 \2", base)
    return base.replace("-", " ").replace("_", " ").title()


def parse_sender(raw_sender: str) -> dict:
    """
    Accept either 'Name <email>' or plain 'email' and return
    {"email": ..., "firstname": ..., "lastname": ..., "company": ...}.
    """
    m = re.match(r'^"?([^"<]+)"?\s*<([^>]+)>', raw_sender.strip())
    if m:
        display, email = m.group(1).strip(), m.group(2).strip().lower()
        parts = display.split()
        firstname = parts[0] if parts else ""
        lastname = " ".join(parts[1:]) if len(parts) > 1 else ""
    else:
        email = raw_sender.strip().lower()
        firstname = ""
        lastname = ""

    _, _, domain = email.partition("@")
    company = domain_to_company(domain)

    return {
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "company": company,
        "domain": domain,
    }


# ---------------------------------------------------------------------------
# HubSpot contact builder
# ---------------------------------------------------------------------------

def build_hubspot_properties(sender: dict, source: str = "Gmail") -> dict:
    """Return the HubSpot property dict to create/update a contact."""
    props = {
        "email": sender["email"],
        "company": sender["company"],
        "hs_lead_source": source,
    }
    if sender.get("firstname"):
        props["firstname"] = sender["firstname"]
    if sender.get("lastname"):
        props["lastname"] = sender["lastname"]
    return props


# ---------------------------------------------------------------------------
# Run-log helpers
# ---------------------------------------------------------------------------

def load_log() -> list:
    if LOG_FILE.exists():
        return json.loads(LOG_FILE.read_text())
    return []


def save_log(entries: list) -> None:
    LOG_FILE.write_text(json.dumps(entries, indent=2, ensure_ascii=False))


def append_run(run_entries: list) -> None:
    log = load_log()
    log.extend(run_entries)
    save_log(log)


# ---------------------------------------------------------------------------
# Main sync function (called by the Claude Code session)
# ---------------------------------------------------------------------------

def process_email_batch(raw_threads: list) -> list:
    """
    Given a list of Gmail thread dicts (as returned by mcp__Gmail__search_threads),
    returns a deduplicated list of sender dicts that should be upserted in HubSpot.
    """
    seen: set[str] = set()
    to_process = []

    for thread in raw_threads:
        for msg in thread.get("messages", []):
            sender_email = msg.get("sender", "").lower().strip()
            if not sender_email or sender_email in seen:
                continue
            seen.add(sender_email)

            if is_automated(sender_email):
                continue

            sender = parse_sender(sender_email)
            sender["subject"] = msg.get("subject", "")
            sender["date"] = msg.get("date", "")
            to_process.append(sender)

    return to_process
