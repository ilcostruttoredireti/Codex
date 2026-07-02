#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitora le email in arrivo su Gmail ed estrae/sincronizza i contatti in HubSpot.
"""

import re
import json
import time
import logging
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Optional

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ── Configurazione ─────────────────────────────────────────────────────────────
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
HUBSPOT_API_BASE = "https://api.hubapi.com"

# Prefissi email da ignorare (mittenti automatici/noreply)
SKIP_PREFIXES = (
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "notifications", "notification", "notifica",
    "invoicing", "payments", "mailer", "nobody",
    "alerts", "alert",
)

# hs_analytics_source valori consentiti da HubSpot:
# ORGANIC_SEARCH, PAID_SEARCH, EMAIL_MARKETING, SOCIAL_MEDIA, REFERRALS,
# OTHER_CAMPAIGNS, DIRECT_TRAFFIC, OFFLINE, PAID_SOCIAL, AI_REFERRALS
CONTACT_SOURCE = "EMAIL_MARKETING"  # Gmail = canale email inbound


# ── Helpers ────────────────────────────────────────────────────────────────────

def is_automated_sender(email: str) -> bool:
    local = email.split("@")[0].lower().replace("+", "")
    return any(local.startswith(p) or local == p for p in SKIP_PREFIXES)


def extract_company_from_domain(email: str) -> str:
    domain = email.split("@")[-1]
    # Rimuove sottodomini comuni (mail., news., m., etc.)
    parts = domain.split(".")
    if len(parts) > 2 and parts[0] in ("mail", "news", "m", "info", "e", "hello"):
        domain = ".".join(parts[1:])
    # Rimuove TLD per il nome azienda
    name = ".".join(parts[:-1]) if len(parts) > 1 else parts[0]
    return name.replace("-", " ").replace("_", " ").title()


def parse_sender(raw_sender: str) -> tuple[str, str, str]:
    """Restituisce (email, firstname, lastname)."""
    name, email = parseaddr(raw_sender)
    email = email.lower().strip()

    firstname, lastname = "", ""
    if name:
        parts = name.strip().split(" ", 1)
        firstname = parts[0]
        lastname = parts[1] if len(parts) > 1 else ""

    return email, firstname, lastname


# ── Gmail ──────────────────────────────────────────────────────────────────────

class GmailClient:
    def __init__(self, access_token: str):
        self.headers = {"Authorization": f"Bearer {access_token}"}

    def list_inbox_threads(self, newer_than_days: int = 1, max_results: int = 100) -> list[dict]:
        params = {
            "q": f"in:inbox newer_than:{newer_than_days}d -from:me",
            "maxResults": max_results,
        }
        resp = httpx.get(f"{GMAIL_API_BASE}/users/me/threads", headers=self.headers, params=params)
        resp.raise_for_status()
        return resp.json().get("threads", [])

    def get_thread_sender(self, thread_id: str) -> Optional[str]:
        resp = httpx.get(
            f"{GMAIL_API_BASE}/users/me/threads/{thread_id}",
            headers=self.headers,
            params={"format": "metadata", "metadataHeaders": "From"},
        )
        resp.raise_for_status()
        data = resp.json()
        messages = data.get("messages", [])
        if not messages:
            return None
        headers = messages[0].get("payload", {}).get("headers", [])
        for h in headers:
            if h.get("name", "").lower() == "from":
                return h["value"]
        return None


# ── HubSpot ───────────────────────────────────────────────────────────────────

class HubSpotClient:
    def __init__(self, api_key: str):
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def search_contact_by_email(self, email: str) -> Optional[dict]:
        payload = {
            "filterGroups": [{"filters": [
                {"propertyName": "email", "operator": "EQ", "value": email}
            ]}],
            "properties": ["email", "firstname", "lastname", "company", "hs_analytics_source"],
            "limit": 1,
        }
        resp = httpx.post(
            f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/search",
            headers=self.headers,
            json=payload,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0] if results else None

    def create_contact(self, email: str, firstname: str, lastname: str, company: str) -> dict:
        payload = {"properties": {
            "email": email,
            "firstname": firstname or email.split("@")[0].title(),
            "lastname": lastname,
            "company": company,
            "hs_analytics_source": CONTACT_SOURCE,
            "hs_lead_status": "NEW",
        }}
        resp = httpx.post(
            f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts",
            headers=self.headers,
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()

    def update_contact(self, contact_id: str, updates: dict) -> dict:
        resp = httpx.patch(
            f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/{contact_id}",
            headers=self.headers,
            json={"properties": updates},
        )
        resp.raise_for_status()
        return resp.json()


# ── Core sync ─────────────────────────────────────────────────────────────────

def sync_email_contacts(gmail_token: str, hubspot_key: str, newer_than_days: int = 1):
    gmail = GmailClient(gmail_token)
    hs = HubSpotClient(hubspot_key)

    threads = gmail.list_inbox_threads(newer_than_days=newer_than_days)
    log.info("Trovati %d thread Gmail", len(threads))

    seen_emails: set[str] = set()
    results = []

    for thread in threads:
        raw_sender = gmail.get_thread_sender(thread["id"])
        if not raw_sender:
            continue

        email, firstname, lastname = parse_sender(raw_sender)

        if not email or email in seen_emails:
            continue
        seen_emails.add(email)

        if is_automated_sender(email):
            log.debug("Saltato (automatico): %s", email)
            continue

        company = extract_company_from_domain(email)
        existing = hs.search_contact_by_email(email)

        if existing:
            contact_id = existing["id"]
            props = existing.get("properties", {})
            updates: dict[str, str] = {}

            if not props.get("hs_analytics_source"):
                updates["hs_analytics_source"] = CONTACT_SOURCE
            if not props.get("company") and company:
                updates["company"] = company
            if not props.get("firstname") and firstname:
                updates["firstname"] = firstname
            if not props.get("lastname") and lastname:
                updates["lastname"] = lastname

            if updates:
                hs.update_contact(contact_id, updates)
                status = "Aggiornato"
            else:
                status = "Ignorato"
        else:
            created = hs.create_contact(email, firstname, lastname, company)
            contact_id = created["id"]
            status = "Creato"

        entry = {"stato": status, "email": email, "hubspot_id": contact_id}
        results.append(entry)
        log.info("[%s] %s → ID %s", status, email, contact_id)

        time.sleep(0.1)  # rate-limiting cortesia

    return results


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os

    gmail_token = os.environ.get("GMAIL_ACCESS_TOKEN", "")
    hubspot_key = os.environ.get("HUBSPOT_API_KEY", "")

    if not gmail_token or not hubspot_key:
        raise SystemExit(
            "Imposta GMAIL_ACCESS_TOKEN e HUBSPOT_API_KEY come variabili d'ambiente."
        )

    days = int(os.environ.get("LOOKBACK_DAYS", "1"))
    output = sync_email_contacts(gmail_token, hubspot_key, newer_than_days=days)

    print("\n=== RISULTATI SINCRONIZZAZIONE ===")
    print(f"{'STATO':<12} {'EMAIL':<45} {'ID HUBSPOT'}")
    print("-" * 75)
    for r in output:
        print(f"{r['stato']:<12} {r['email']:<45} {r['hubspot_id']}")

    totals = {}
    for r in output:
        totals[r["stato"]] = totals.get(r["stato"], 0) + 1
    print(f"\nRiepilogo: {totals}")
