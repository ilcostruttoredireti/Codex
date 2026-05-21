"""
Lettura email da Gmail con tracciamento degli ID già processati.
"""

import json
import os
import re
from dataclasses import dataclass
from typing import Optional

from googleapiclient.discovery import Resource


STATE_FILE = "processed_ids.json"


@dataclass
class SenderInfo:
    email: str
    firstname: str
    lastname: str
    domain: str
    company: str
    thread_id: str
    message_id: str
    subject: str


def _load_processed() -> set:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return set(json.load(f))
    return set()


def _save_processed(ids: set) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(list(ids), f)


def _parse_from_header(from_header: str) -> tuple[str, str, str]:
    """Ritorna (display_name, email, domain) dal campo From."""
    match = re.match(r'^"?([^"<]+?)"?\s*<([^>]+)>$', from_header.strip())
    if match:
        display_name = match.group(1).strip()
        email = match.group(2).strip().lower()
    else:
        email = from_header.strip().lower()
        display_name = ""

    domain = email.split("@")[1] if "@" in email else ""
    return display_name, email, domain


def _split_name(display_name: str) -> tuple[str, str]:
    parts = display_name.strip().split(" ", 1)
    firstname = parts[0] if parts else ""
    lastname = parts[1] if len(parts) > 1 else ""
    return firstname, lastname


PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "icloud.com", "me.com", "protonmail.com",
    "libero.it", "virgilio.it", "tiscali.it", "tin.it",
}


def _company_from_domain(domain: str) -> str:
    if not domain or domain in PERSONAL_DOMAINS:
        return ""
    # "acme-corp.com" → "Acme Corp"
    name = domain.split(".")[0]
    return name.replace("-", " ").replace("_", " ").title()


def fetch_new_senders(service: Resource,
                      owner_email: str,
                      label: str = "INBOX") -> list[SenderInfo]:
    """Recupera i mittenti delle email non ancora processate."""
    processed = _load_processed()
    new_senders: list[SenderInfo] = []

    result = service.users().messages().list(
        userId="me",
        labelIds=[label],
        maxResults=50,
        q=f"-from:{owner_email}",
    ).execute()

    messages = result.get("messages", [])

    for msg_ref in messages:
        msg_id = msg_ref["id"]
        if msg_id in processed:
            continue

        msg = service.users().messages().get(
            userId="me",
            id=msg_id,
            format="metadata",
            metadataHeaders=["From", "Subject"],
        ).execute()

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        from_header = headers.get("From", "")
        subject = headers.get("Subject", "(nessun oggetto)")
        thread_id = msg.get("threadId", "")

        if not from_header:
            processed.add(msg_id)
            continue

        display_name, email, domain = _parse_from_header(from_header)
        firstname, lastname = _split_name(display_name)
        company = _company_from_domain(domain)

        new_senders.append(SenderInfo(
            email=email,
            firstname=firstname,
            lastname=lastname,
            domain=domain,
            company=company,
            thread_id=thread_id,
            message_id=msg_id,
            subject=subject,
        ))
        processed.add(msg_id)

    _save_processed(processed)
    return new_senders
