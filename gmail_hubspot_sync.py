"""
Gmail → HubSpot Contact Sync
=============================
Fetches recent inbound Gmail threads, extracts sender metadata,
and creates or updates HubSpot contacts — deduplicating on email address.

State tracking: processed Gmail thread IDs are stored in processed_threads.txt
so re-runs never double-import.

Fields populated on HubSpot contacts:
  email, firstname, lastname, company (from domain)
  hs_analytics_source = "EMAIL_MARKETING"   (enumeration — only valid option for email)

Timeline activity: a NOTE is created on each contact with body:
  "Fonte contatto: Gmail\nTag: Inbound Gmail\nEmail ricevuta il YYYY-MM-DD"

Notes on read-only HubSpot properties (discovered 2026-07-09):
  - leadsource: does not exist as a writable property
  - hs_analytics_source_data_1: read-only; cannot be set via API
  - hs_object_source_label: read-only enumeration
"""

import re
import os
import json
from datetime import datetime, timezone

# ── helpers ──────────────────────────────────────────────────────────────────

STATE_FILE = os.path.join(os.path.dirname(__file__), "processed_threads.txt")

SKIP_DOMAINS = {
    # well-known bulk/transactional senders that are not real contacts
    "noreply", "no-reply", "donotreply", "mailer-daemon",
}

SKIP_LOCAL_PARTS = {
    "nobody", "noreply", "no-reply", "donotreply",
    "mailer-daemon", "bounce", "postmaster", "abuse",
}


def load_processed_ids() -> set:
    if not os.path.exists(STATE_FILE):
        return set()
    with open(STATE_FILE) as f:
        return {line.strip() for line in f if line.strip()}


def save_processed_id(thread_id: str):
    with open(STATE_FILE, "a") as f:
        f.write(thread_id + "\n")


def parse_sender(raw_sender: str) -> dict:
    """
    Parse a raw From header into {email, firstname, lastname, company}.
    Handles both 'Name <email>' and bare 'email' formats.
    """
    raw_sender = raw_sender.strip()
    match = re.match(r'^"?(.+?)"?\s*<([^>]+)>$', raw_sender)
    if match:
        display_name = match.group(1).strip()
        email = match.group(2).strip().lower()
    else:
        display_name = ""
        email = raw_sender.lower()

    local, _, domain = email.partition("@")
    root_domain = domain.split(".")[-2] if domain.count(".") >= 1 else domain

    # derive company from domain (capitalise first letter, strip TLD noise)
    company = root_domain.replace("-", " ").replace("_", " ").title()

    # derive names
    firstname = lastname = ""
    if display_name:
        parts = display_name.split()
        firstname = parts[0] if parts else ""
        lastname = " ".join(parts[1:]) if len(parts) > 1 else ""
    else:
        # fall back to local part of the email
        name_guess = local.replace(".", " ").replace("-", " ").replace("_", " ").title()
        parts = name_guess.split()
        firstname = parts[0] if parts else local.title()
        lastname = " ".join(parts[1:]) if len(parts) > 1 else ""

    return {
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "company": company,
        "domain": domain,
    }


def is_skippable(sender_info: dict) -> bool:
    local = sender_info["email"].split("@")[0]
    return local in SKIP_LOCAL_PARTS


# ── main sync logic (called by Claude Code's MCP tools) ─────────────────────

def describe_sync_plan(threads: list, existing_contacts: dict) -> list:
    """
    Given a list of Gmail thread dicts and a mapping of email→HubSpot contact,
    return a list of actions: {'action': 'CREATE'|'UPDATE'|'SKIP', ...}
    """
    actions = []
    seen_emails = set()
    processed_ids = load_processed_ids()

    for thread in threads:
        thread_id = thread["id"]
        if thread_id in processed_ids:
            actions.append({"action": "SKIP", "reason": "already processed", "thread_id": thread_id})
            continue

        messages = thread.get("messages", [])
        if not messages:
            continue

        raw_sender = messages[0].get("sender", "")
        if not raw_sender:
            continue

        info = parse_sender(raw_sender)
        email = info["email"]

        if is_skippable(info):
            actions.append({"action": "SKIP", "reason": "automated/bulk sender", "email": email, "thread_id": thread_id})
            continue

        if email in seen_emails:
            actions.append({"action": "SKIP", "reason": "duplicate in batch", "email": email, "thread_id": thread_id})
            continue
        seen_emails.add(email)

        if email in existing_contacts:
            contact = existing_contacts[email]
            missing = {}
            props = contact.get("properties", {})
            if not props.get("leadsource"):
                missing["leadsource"] = "Gmail"
            # fill genuinely blank fields
            for field in ("firstname", "lastname", "company"):
                if not props.get(field) and info.get(field):
                    missing[field] = info[field]

            actions.append({
                "action": "UPDATE" if missing else "SKIP",
                "reason": "contact exists" + ("" if missing else ", nothing to update"),
                "email": email,
                "hubspot_id": contact["id"],
                "fields_to_update": missing,
                "thread_id": thread_id,
            })
        else:
            actions.append({
                "action": "CREATE",
                "email": email,
                "firstname": info["firstname"],
                "lastname": info["lastname"],
                "company": info["company"],
                "leadsource": "Gmail",
                "thread_id": thread_id,
            })

    return actions


if __name__ == "__main__":
    # Stand-alone smoke test — prints parse results for a sample input
    samples = [
        "Riccardo Belli <riccardo@martes-ai.com>",
        "info@bdmassociati.it",
        "events@send.zapier.com",
        "nobody@e.feedspot.com",
        'Adobe Creative Cloud <mail@mail.adobe.com>',
    ]
    for s in samples:
        info = parse_sender(s)
        skip = is_skippable(info)
        print(json.dumps({**info, "skip": skip}, ensure_ascii=False))
