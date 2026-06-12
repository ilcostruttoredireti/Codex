"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail, extracts senders, creates/updates HubSpot contacts.
"""

import os
import re
import json
import time
import base64
import logging
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import hubspot
from hubspot.crm.contacts import SimplePublicObjectInput, ApiException
from hubspot.crm.contacts.api import basic_api, search_api
from hubspot.crm.contacts.models import PublicObjectSearchRequest, Filter, FilterGroup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_TOKEN_FILE = Path("gmail_token.json")
GMAIL_CREDENTIALS_FILE = Path("gmail_credentials.json")

HUBSPOT_ACCESS_TOKEN = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")

STATE_FILE = Path("sync_state.json")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL", "120"))

# Domains we own / want to skip as contacts
SKIP_DOMAINS = {"gmail.com", "googlemail.com"}
OWN_EMAILS: set[str] = set(
    e.strip().lower()
    for e in os.environ.get("OWN_EMAILS", "").split(",")
    if e.strip()
)

# ─── Gmail helpers ────────────────────────────────────────────────────────────


def gmail_service():
    creds = None
    if GMAIL_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_FILE), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(GMAIL_CREDENTIALS_FILE), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        GMAIL_TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def get_header(headers: list[dict], name: str) -> str:
    name_lower = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_lower:
            return h.get("value", "")
    return ""


def parse_name_email(raw: str) -> tuple[str, str]:
    """Return (display_name, email_address) from a raw From header."""
    name, addr = parseaddr(raw)
    return name.strip(), addr.strip().lower()


def domain_from_email(email: str) -> str:
    parts = email.split("@")
    return parts[1] if len(parts) == 2 else ""


def company_from_domain(domain: str) -> str:
    """Best-effort company name from domain (strips TLD and common prefixes)."""
    if not domain:
        return ""
    root = domain.split(".")[0]
    return root.replace("-", " ").replace("_", " ").title()


def fetch_new_messages(service, since_history_id: str | None) -> list[dict]:
    """
    If we have a history ID, use history.list for efficiency.
    Otherwise fall back to a search for inbox messages in the last 7 days.
    """
    messages = []
    if since_history_id:
        try:
            resp = (
                service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=since_history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg = added.get("message", {})
                    if "INBOX" in msg.get("labelIds", []):
                        messages.append(msg)
        except Exception as exc:
            log.warning("History fetch failed (%s); falling back to search", exc)
            since_history_id = None

    if not since_history_id:
        result = (
            service.users()
            .messages()
            .list(userId="me", q="in:inbox -from:me newer_than:7d", maxResults=50)
            .execute()
        )
        messages = result.get("messages", [])

    return messages


def get_message_detail(service, msg_id: str) -> dict | None:
    try:
        return (
            service.users()
            .messages()
            .get(userId="me", id=msg_id, format="metadata",
                 metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )
    except Exception as exc:
        log.warning("Could not fetch message %s: %s", msg_id, exc)
        return None


# ─── HubSpot helpers ─────────────────────────────────────────────────────────


def hubspot_client():
    return hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)


def find_contact_by_email(client, email: str) -> dict | None:
    """Return existing HubSpot contact dict or None."""
    search_req = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(
                filters=[
                    Filter(
                        property_name="email",
                        operator="EQ",
                        value=email,
                    )
                ]
            )
        ],
        properties=["email", "firstname", "lastname", "company", "hs_lead_status"],
        limit=1,
    )
    try:
        resp = client.crm.contacts.search_api.do_search(
            public_object_search_request=search_req
        )
        results = resp.results
        return results[0].to_dict() if results else None
    except ApiException as exc:
        log.error("HubSpot search failed for %s: %s", email, exc)
        return None


def build_contact_properties(
    email: str,
    name: str,
    domain: str,
    existing: dict | None,
) -> dict:
    """
    Merge new data with existing, fill only missing/empty fields.
    Returns the properties dict to set (empty dict = nothing to update).
    """
    props: dict[str, str] = {}
    existing_props = (existing or {}).get("properties", {})

    # Always set source if not already
    if not existing_props.get("hs_analytics_source_data_1"):
        props["hs_analytics_source_data_1"] = "Gmail"

    # Parse name into firstname / lastname
    first, last = "", ""
    if name:
        parts = name.split(None, 1)
        first = parts[0]
        last = parts[1] if len(parts) > 1 else ""

    if first and not existing_props.get("firstname"):
        props["firstname"] = first
    if last and not existing_props.get("lastname"):
        props["lastname"] = last

    # Company from domain
    if domain and domain not in SKIP_DOMAINS:
        company = company_from_domain(domain)
        if company and not existing_props.get("company"):
            props["company"] = company

    # Custom source field
    if not existing_props.get("lead_source"):
        props["lead_source"] = "Gmail"

    return props


# ─── Sync state ───────────────────────────────────────────────────────────────


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"history_id": None, "processed_ids": []}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ─── Core sync logic ──────────────────────────────────────────────────────────


def process_message(client, msg_detail: dict) -> dict:
    """
    Process one Gmail message. Returns a result dict:
      { status, email, contact_id }
    """
    headers = msg_detail.get("payload", {}).get("headers", [])
    raw_from = get_header(headers, "From")
    subject = get_header(headers, "Subject")

    if not raw_from:
        return {"status": "Ignorato", "email": "", "contact_id": None, "reason": "no From header"}

    name, email = parse_name_email(raw_from)

    if not email or "@" not in email:
        return {"status": "Ignorato", "email": email, "contact_id": None, "reason": "invalid email"}

    if email in OWN_EMAILS:
        return {"status": "Ignorato", "email": email, "contact_id": None, "reason": "own address"}

    domain = domain_from_email(email)

    existing = find_contact_by_email(client, email)

    if existing:
        contact_id = existing.get("id")
        props = build_contact_properties(email, name, domain, existing)

        if not props:
            log.info("  [=] Ignorato (nessun aggiornamento) — %s (ID: %s)", email, contact_id)
            return {"status": "Ignorato", "email": email, "contact_id": contact_id,
                    "reason": "no fields to update"}

        try:
            client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(properties=props),
            )
            log.info("  [U] Aggiornato — %s (ID: %s) props=%s", email, contact_id, list(props))
            return {"status": "Aggiornato", "email": email, "contact_id": contact_id}
        except ApiException as exc:
            log.error("  [!] Update failed for %s: %s", email, exc)
            return {"status": "Errore", "email": email, "contact_id": contact_id}

    else:
        create_props = {"email": email}
        extra = build_contact_properties(email, name, domain, None)
        create_props.update(extra)

        try:
            created = client.crm.contacts.basic_api.create(
                simple_public_object_input=SimplePublicObjectInput(properties=create_props)
            )
            contact_id = created.id
            log.info("  [+] Creato — %s (ID: %s)", email, contact_id)
            return {"status": "Creato", "email": email, "contact_id": contact_id}
        except ApiException as exc:
            # 409 = duplicate (contact exists with this email but wasn't found in search)
            if exc.status == 409:
                log.warning("  [=] Duplicato intercettato via 409 — %s", email)
                return {"status": "Ignorato", "email": email, "contact_id": None,
                        "reason": "duplicate 409"}
            log.error("  [!] Create failed for %s: %s", email, exc)
            return {"status": "Errore", "email": email, "contact_id": None}


def run_sync_cycle(gmail_svc, hs_client, state: dict) -> dict:
    """Run one poll cycle. Returns updated state."""
    log.info("=== Avvio ciclo di sincronizzazione ===")

    messages = fetch_new_messages(gmail_svc, state.get("history_id"))
    processed_ids: list[str] = state.get("processed_ids", [])
    results = []

    # Track latest historyId across this batch
    latest_history_id = state.get("history_id")

    for msg_stub in messages:
        msg_id = msg_stub.get("id") or msg_stub.get("message", {}).get("id")
        if not msg_id or msg_id in processed_ids:
            continue

        detail = get_message_detail(gmail_svc, msg_id)
        if not detail:
            continue

        # Update latest history ID
        h_id = detail.get("historyId")
        if h_id and (not latest_history_id or int(h_id) > int(latest_history_id)):
            latest_history_id = h_id

        result = process_message(hs_client, detail)
        result["message_id"] = msg_id
        results.append(result)
        processed_ids.append(msg_id)

    # Keep only last 5000 IDs to avoid unbounded growth
    state["processed_ids"] = processed_ids[-5000:]
    state["history_id"] = latest_history_id
    state["last_run"] = datetime.now(timezone.utc).isoformat()

    _print_summary(results)
    return state


def _print_summary(results: list[dict]):
    if not results:
        log.info("Nessuna email nuova da processare.")
        return

    created = [r for r in results if r["status"] == "Creato"]
    updated = [r for r in results if r["status"] == "Aggiornato"]
    ignored = [r for r in results if r["status"] == "Ignorato"]
    errors  = [r for r in results if r["status"] == "Errore"]

    log.info("─" * 60)
    log.info("RIEPILOGO: %d email processate", len(results))
    log.info("  Creati:    %d", len(created))
    log.info("  Aggiornati:%d", len(updated))
    log.info("  Ignorati:  %d", len(ignored))
    log.info("  Errori:    %d", len(errors))
    log.info("─" * 60)

    print("\n{:<12} {:<40} {}".format("Stato", "Email contatto", "ID HubSpot"))
    print("-" * 70)
    for r in results:
        print("{:<12} {:<40} {}".format(
            r["status"], r.get("email", ""), r.get("contact_id") or "—"
        ))


# ─── Entry point ─────────────────────────────────────────────────────────────


def main():
    if not HUBSPOT_ACCESS_TOKEN:
        raise SystemExit(
            "Imposta la variabile d'ambiente HUBSPOT_ACCESS_TOKEN prima di avviare."
        )

    log.info("Inizializzazione servizi Gmail e HubSpot…")
    gmail_svc = gmail_service()
    hs_client = hubspot_client()

    state = load_state()
    log.info("Stato caricato. History ID: %s", state.get("history_id"))

    log.info("Avvio monitoraggio continuo (intervallo: %ds). Ctrl+C per fermare.", POLL_INTERVAL_SECONDS)

    while True:
        try:
            state = run_sync_cycle(gmail_svc, hs_client, state)
            save_state(state)
        except KeyboardInterrupt:
            log.info("Interruzione manuale. Salvataggio stato…")
            save_state(state)
            break
        except Exception as exc:
            log.exception("Errore nel ciclo di sync: %s", exc)

        log.info("Prossimo ciclo tra %d secondi…", POLL_INTERVAL_SECONDS)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
