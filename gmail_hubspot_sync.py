#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Monitors Gmail inbox continuously and syncs sender contacts to HubSpot.
For each inbound email: creates a new HubSpot contact or updates an existing
one, logs a timeline activity note, and labels the Gmail thread "Inbound Gmail".

Usage:
    export HUBSPOT_ACCESS_TOKEN=pat-na1-xxxx
    python gmail_hubspot_sync.py

Gmail setup (first run):
    Place credentials.json (OAuth client) in the working directory.
    The script opens a browser to complete OAuth the first time.

Environment variables:
    HUBSPOT_ACCESS_TOKEN  – HubSpot private-app token (required)
    POLL_INTERVAL         – seconds between inbox polls (default: 60)
    GMAIL_LABEL           – label applied to processed threads (default: "Inbound Gmail")
    LOG_LEVEL             – DEBUG | INFO | WARNING (default: INFO)
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── Config ─────────────────────────────────────────────────────────────────

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]
HUBSPOT_BASE = "https://api.hubapi.com"
TOKEN_FILE = "token.json"
CREDENTIALS_FILE = "credentials.json"
STATE_FILE = "sync_state.json"

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
GMAIL_LABEL = os.getenv("GMAIL_LABEL", "Inbound Gmail")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Local-part patterns that indicate automated/system senders
_NOREPLY_RE = re.compile(
    r"(noreply|no-reply|no_reply|donotreply|do-not-reply|mailer-daemon|postmaster|bounce)",
    re.IGNORECASE,
)
# Domains that produce only system mail
_IGNORED_DOMAINS = frozenset(
    ["bounce.gmail.com", "mailer.google.com", "mail.protection.outlook.com"]
)


# ── State ──────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"processed_message_ids": []}


def _save_state(state: dict) -> None:
    state["processed_message_ids"] = state["processed_message_ids"][-20_000:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Gmail ──────────────────────────────────────────────────────────────────

def _gmail_service():
    creds: Optional[Credentials] = None
    if Path(TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(CREDENTIALS_FILE).exists():
                raise FileNotFoundError(
                    f"{CREDENTIALS_FILE} not found. "
                    "Download it from Google Cloud Console → APIs & Services → Credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _get_or_create_label(svc, name: str) -> str:
    existing = svc.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in existing:
        if lbl["name"] == name:
            return lbl["id"]
    created = svc.users().labels().create(
        userId="me",
        body={
            "name": name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        },
    ).execute()
    log.info("Gmail label created: '%s' (id=%s)", name, created["id"])
    return created["id"]


def _apply_label_to_thread(svc, thread_id: str, label_id: str) -> None:
    svc.users().threads().modify(
        userId="me",
        id=thread_id,
        body={"addLabelIds": [label_id]},
    ).execute()


def _fetch_new_messages(svc, state: dict, max_results: int = 100) -> list[dict]:
    """
    Return message stubs (id + threadId) for inbound messages not yet processed.
    Uses message-level query so `-from:me` filters correctly even in reply threads.
    """
    resp = (
        svc.users()
        .messages()
        .list(userId="me", q="in:inbox -from:me", maxResults=max_results)
        .execute()
    )
    messages = resp.get("messages", [])
    seen = set(state["processed_message_ids"])
    return [m for m in messages if m["id"] not in seen]


def _parse_from_header(value: str) -> tuple[str, str, str]:
    """Parse From header → (email, full_name, domain)."""
    m = re.match(r'^"?([^"<]*?)"?\s*<([^>]+)>\s*$', value.strip())
    if m:
        name = m.group(1).strip().strip('"')
        email = m.group(2).strip().lower()
    else:
        name = ""
        email = value.strip().lower()
    domain = email.split("@")[-1] if "@" in email else ""
    return email, name, domain


def _get_message_info(svc, msg_stub: dict) -> Optional[dict]:
    """Fetch minimal headers for one message."""
    msg = (
        svc.users()
        .messages()
        .get(
            userId="me",
            id=msg_stub["id"],
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        )
        .execute()
    )
    headers = {
        h["name"]: h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }
    from_raw = headers.get("From", "")
    if not from_raw:
        return None
    email, name, domain = _parse_from_header(from_raw)
    return {
        "message_id": msg_stub["id"],
        "thread_id": msg_stub["threadId"],
        "email": email,
        "name": name,
        "domain": domain,
        "subject": headers.get("Subject", "(no subject)"),
        "date": headers.get("Date", ""),
    }


def _is_ignorable(email: str, domain: str) -> bool:
    if _NOREPLY_RE.search(email):
        return True
    if domain in _IGNORED_DOMAINS:
        return True
    return False


# ── HubSpot ────────────────────────────────────────────────────────────────

class HubSpot:
    def __init__(self, token: str) -> None:
        self._s = requests.Session()
        self._s.headers.update(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )

    def _post(self, path: str, body: dict) -> dict:
        r = self._s.post(f"{HUBSPOT_BASE}{path}", json=body)
        r.raise_for_status()
        return r.json()

    def _patch(self, path: str, body: dict) -> dict:
        r = self._s.patch(f"{HUBSPOT_BASE}{path}", json=body)
        r.raise_for_status()
        return r.json()

    def find_contact(self, email: str) -> Optional[dict]:
        data = self._post(
            "/crm/v3/objects/contacts/search",
            {
                "filterGroups": [
                    {
                        "filters": [
                            {"propertyName": "email", "operator": "EQ", "value": email}
                        ]
                    }
                ],
                "properties": ["email", "firstname", "lastname", "company", "hs_lead_status"],
                "limit": 1,
            },
        )
        results = data.get("results", [])
        return results[0] if results else None

    def create_contact(self, props: dict) -> dict:
        return self._post("/crm/v3/objects/contacts", {"properties": props})

    def update_contact(self, contact_id: str, props: dict) -> dict:
        return self._patch(
            f"/crm/v3/objects/contacts/{contact_id}", {"properties": props}
        )

    def add_note(self, contact_id: str, body: str) -> None:
        ts = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        try:
            self._post(
                "/crm/v3/objects/notes",
                {
                    "properties": {"hs_note_body": body, "hs_timestamp": ts},
                    "associations": [
                        {
                            "to": {"id": contact_id},
                            "types": [
                                {
                                    "associationCategory": "HUBSPOT_DEFINED",
                                    "associationTypeId": 202,
                                }
                            ],
                        }
                    ],
                },
            )
        except requests.HTTPError as exc:
            log.warning("Note creation failed for contact %s: %s", contact_id, exc)


# ── Sync logic ─────────────────────────────────────────────────────────────

def _build_create_props(email: str, name: str, domain: str) -> dict:
    props: dict = {"email": email, "hs_lead_status": "NEW"}
    parts = name.strip().split(None, 1) if name.strip() else []
    if parts:
        props["firstname"] = parts[0]
    if len(parts) > 1:
        props["lastname"] = parts[1]
    if domain and "." in domain:
        props["company"] = domain.split(".")[0].capitalize()
    return props


def _build_update_props(existing_props: dict, name: str, domain: str) -> dict:
    updates: dict = {}
    if not existing_props.get("firstname") and name:
        parts = name.strip().split(None, 1)
        updates["firstname"] = parts[0]
        if len(parts) > 1:
            updates["lastname"] = parts[1]
    if not existing_props.get("company") and domain and "." in domain:
        updates["company"] = domain.split(".")[0].capitalize()
    return updates


def sync_sender(hs: HubSpot, info: dict) -> dict:
    email = info["email"]
    name = info["name"]
    domain = info["domain"]

    if _is_ignorable(email, domain):
        return {"status": "Ignorato", "email": email, "contact_id": None}

    note_body = (
        f"📧 Inbound Gmail\n"
        f"Oggetto: {info['subject']}\n"
        f"Data: {info['date']}\n"
        f"Fonte: Gmail"
    )

    existing = hs.find_contact(email)
    if existing:
        contact_id = str(existing["id"])
        updates = _build_update_props(existing.get("properties", {}), name, domain)
        if updates:
            hs.update_contact(contact_id, updates)
        hs.add_note(contact_id, note_body)
        return {"status": "Aggiornato", "email": email, "contact_id": contact_id}
    else:
        props = _build_create_props(email, name, domain)
        created = hs.create_contact(props)
        contact_id = str(created["id"])
        hs.add_note(contact_id, note_body)
        return {"status": "Creato", "email": email, "contact_id": contact_id}


# ── Output ─────────────────────────────────────────────────────────────────

def _print_report(results: list[dict]) -> None:
    if not results:
        return
    w = max(len(r["email"]) for r in results) + 2
    sep = "─" * (w + 36)
    print(f"\n┌─ Sync Report {sep}┐")
    for r in results:
        cid = str(r["contact_id"]) if r["contact_id"] else "—"
        print(f"│  [{r['status']:10s}]  {r['email']:{w}s}  ID HubSpot: {cid:<18s}│")
    print(f"└{sep}──────────────────┘\n")


# ── Main loop ──────────────────────────────────────────────────────────────

def run_once(svc, hs: HubSpot, label_id: str, state: dict) -> list[dict]:
    new_msgs = _fetch_new_messages(svc, state)
    if not new_msgs:
        log.debug("Nessun nuovo messaggio.")
        return []

    log.info("Trovati %d nuovi messaggi da processare.", len(new_msgs))
    results: list[dict] = []
    seen_emails: set[str] = set()       # deduplicate senders within same poll cycle
    labeled_threads: set[str] = set()   # apply label once per thread

    for stub in new_msgs:
        msg_id = stub["id"]
        try:
            info = _get_message_info(svc, stub)
            if not info:
                state["processed_message_ids"].append(msg_id)
                continue

            email = info["email"]

            # Skip duplicate senders in the same poll cycle
            if email in seen_emails:
                state["processed_message_ids"].append(msg_id)
                continue
            seen_emails.add(email)

            result = sync_sender(hs, info)
            results.append(result)

            if result["status"] != "Ignorato":
                thread_id = info["thread_id"]
                if thread_id not in labeled_threads:
                    _apply_label_to_thread(svc, thread_id, label_id)
                    labeled_threads.add(thread_id)

            state["processed_message_ids"].append(msg_id)
            log.info(
                "[%s] %s → contact_id=%s",
                result["status"],
                result["email"],
                result["contact_id"],
            )
        except Exception as exc:
            log.error("Errore sul messaggio %s: %s", msg_id, exc)

    _save_state(state)
    return results


def main() -> None:
    token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise SystemExit(
            "HUBSPOT_ACCESS_TOKEN non impostato.\n"
            "Esporta la variabile prima di avviare:\n"
            "  export HUBSPOT_ACCESS_TOKEN=pat-na1-xxxx"
        )

    log.info("Autenticazione Gmail in corso…")
    svc = _gmail_service()
    hs = HubSpot(token)
    state = _load_state()

    label_id = _get_or_create_label(svc, GMAIL_LABEL)
    log.info("Label Gmail '%s' pronta (id=%s)", GMAIL_LABEL, label_id)
    log.info("Sync loop avviato — polling ogni %ds", POLL_INTERVAL)

    while True:
        try:
            results = run_once(svc, hs, label_id, state)
            _print_report(results)
        except Exception as exc:
            log.error("Errore nel ciclo di sync: %s", exc)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
