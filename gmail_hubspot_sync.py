"""
Gmail → HubSpot Contact Sync
Monitors inbox emails and upserts senders as HubSpot contacts.
Run on a schedule (e.g. daily via cron or GitHub Actions).

Dependencies: google-api-python-client, google-auth, hubspot-api-client
"""

import re
import os
import json
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.api import BasicApi, SearchApi
from hubspot.crm.contacts.models import PublicObjectSearchRequest, Filter, FilterGroup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Senders to always skip ──────────────────────────────────────────────────
SKIP_PREFIXES = {
    "no-reply", "noreply", "notify-noreply", "mailer", "nobody",
    "conferma-", "conferma", "notifica", "notifications", "notification",
    "do-not-reply", "donotreply", "bounce", "postmaster", "daemon",
    "auto-reply", "autoreply",
}
SKIP_DOMAINS = {
    "google.com", "gmail.com", "youtube.com", "amazon.it", "amazon.com",
    "ebay.com", "revolut.com", "academia-mail.com",
}


def _should_skip(email: str) -> bool:
    email = email.lower()
    local, domain = email.split("@", 1)
    if domain in SKIP_DOMAINS:
        return True
    for prefix in SKIP_PREFIXES:
        if local.startswith(prefix):
            return True
    return False


def _parse_sender(raw: str) -> dict | None:
    """Return {email, firstname, lastname, company} or None if should skip."""
    display_name, email = parseaddr(raw)
    if not email or "@" not in email:
        return None
    email = email.lower().strip()
    if _should_skip(email):
        return None

    domain = email.split("@")[1]
    company = _company_from_domain(domain)

    # Try to extract name parts from display name or email local part
    firstname, lastname = _parse_name(display_name, email.split("@")[0])

    return {
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "company": company,
        "domain": domain,
    }


def _company_from_domain(domain: str) -> str:
    """Best-effort company name from domain (strips TLD, capitalises)."""
    parts = domain.replace(".it", "").replace(".eu", "").replace(".com", "") \
                  .replace(".org", "").replace(".net", "").replace(".io", "") \
                  .replace(".sh", "").replace(".ai", "").replace(".es", "")
    name = parts.split(".")[0]
    return " ".join(w.capitalize() for w in re.split(r"[-_]", name))


def _parse_name(display: str, local: str) -> tuple[str, str]:
    display = display.strip()
    if display:
        parts = display.split()
        if len(parts) >= 2:
            return parts[0], " ".join(parts[1:])
        return parts[0], ""
    # Fall back to dot-separated local part (e.g. si.quan → Si, Quan)
    if "." in local:
        parts = local.split(".", 1)
        return parts[0].capitalize(), parts[1].capitalize()
    return local.capitalize(), ""


# ── Gmail ──────────────────────────────────────────────────────────────────

def get_gmail_service(token_path: str = "token.json"):
    creds = Credentials.from_authorized_user_file(token_path)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)


def fetch_inbox_senders(service, hours_back: int = 24) -> list[dict]:
    """Return unique parsed sender dicts from the last `hours_back` hours."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).strftime("%Y/%m/%d")
    query = f"in:inbox -from:me after:{since}"

    results = service.users().messages().list(
        userId="me", q=query, maxResults=500
    ).execute()
    messages = results.get("messages", [])

    seen: set[str] = set()
    senders: list[dict] = []

    for msg_ref in messages:
        msg = service.users().messages().get(
            userId="me", id=msg_ref["id"], format="metadata",
            metadataHeaders=["From"]
        ).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        raw_from = headers.get("From", "")
        parsed = _parse_sender(raw_from)
        if parsed and parsed["email"] not in seen:
            seen.add(parsed["email"])
            senders.append(parsed)

    return senders


# ── HubSpot ────────────────────────────────────────────────────────────────

def get_hubspot_client(api_key: str):
    return hubspot.Client.create(access_token=api_key)


def find_contact_by_email(client, email: str) -> dict | None:
    search_request = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(filters=[Filter(
                property_name="email",
                operator="EQ",
                value=email,
            )])
        ],
        properties=["email", "firstname", "lastname", "company", "hs_analytics_source"],
        limit=1,
    )
    response = client.crm.contacts.search_api.do_search(
        public_object_search_request=search_request
    )
    return response.results[0] if response.results else None


def upsert_contact(client, sender: dict) -> dict:
    """
    Returns {"status": "Creato"|"Aggiornato"|"Ignorato", "email": ..., "id": ...}
    """
    email = sender["email"]
    existing = find_contact_by_email(client, email)

    props = {
        "email": email,
        "hs_analytics_source": "OTHER_CAMPAIGNS",
    }
    if sender.get("firstname"):
        props["firstname"] = sender["firstname"]
    if sender.get("lastname"):
        props["lastname"] = sender["lastname"]
    if sender.get("company"):
        props["company"] = sender["company"]

    if existing is None:
        obj = SimplePublicObjectInputForCreate(properties=props)
        created = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=obj
        )
        # Add Inbound Gmail note
        client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties={
                    "hs_note_body": (
                        f"Contatto acquisito da Gmail (Inbound Gmail). "
                        f"Mittente: {email}. "
                        f"Fonte: email in arrivo del {datetime.now(timezone.utc).date()}."
                    ),
                    "hs_timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                }
            )
        )
        return {"status": "Creato", "email": email, "id": created.id}

    # Update only missing fields
    existing_props = existing.properties
    update_props = {}
    for field in ("firstname", "lastname", "company"):
        if not existing_props.get(field) and props.get(field):
            update_props[field] = props[field]

    if update_props:
        client.crm.contacts.basic_api.update(
            contact_id=existing.id,
            simple_public_object_input=SimplePublicObjectInputForCreate(
                properties=update_props
            ),
        )
        return {"status": "Aggiornato", "email": email, "id": existing.id}

    return {"status": "Ignorato", "email": email, "id": existing.id}


# ── Main ───────────────────────────────────────────────────────────────────

def run_sync(hours_back: int = 24):
    gmail_token = os.environ.get("GMAIL_TOKEN_PATH", "token.json")
    hubspot_key = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")

    gmail = get_gmail_service(gmail_token)
    hs = get_hubspot_client(hubspot_key)

    log.info("Fetching Gmail inbox senders (last %dh)…", hours_back)
    senders = fetch_inbox_senders(gmail, hours_back=hours_back)
    log.info("Found %d unique senders to process.", len(senders))

    results = []
    for sender in senders:
        try:
            result = upsert_contact(hs, sender)
            results.append(result)
            log.info("[%s] %s (ID: %s)", result["status"], result["email"], result["id"])
        except ApiException as e:
            log.error("HubSpot error for %s: %s", sender["email"], e)
            results.append({"status": "Errore", "email": sender["email"], "id": None})

    summary = {
        "Creato": sum(1 for r in results if r["status"] == "Creato"),
        "Aggiornato": sum(1 for r in results if r["status"] == "Aggiornato"),
        "Ignorato": sum(1 for r in results if r["status"] == "Ignorato"),
        "Errore": sum(1 for r in results if r["status"] == "Errore"),
    }
    log.info("Sync completo: %s", summary)
    print(json.dumps({"summary": summary, "results": results}, indent=2, ensure_ascii=False))
    return results


if __name__ == "__main__":
    run_sync()
