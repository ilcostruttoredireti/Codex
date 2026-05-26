"""
Configurazione centralizzata dal file .env / variabili d'ambiente.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Gmail ────────────────────────────────────────────────────────────────────
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE       = os.getenv("GMAIL_TOKEN_FILE", "token.json")
GMAIL_SCOPES           = ["https://www.googleapis.com/auth/gmail.readonly"]

# ── HubSpot ──────────────────────────────────────────────────────────────────
HUBSPOT_API_KEY        = os.getenv("HUBSPOT_API_KEY", "")
HUBSPOT_BASE_URL       = "https://api.hubapi.com"

# ── Monitor ──────────────────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS  = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))   # ogni 60s
STATE_FILE             = os.getenv("STATE_FILE", "state.json")

# Etichetta sorgente contatto
CONTACT_SOURCE_LABEL   = "Gmail"
CONTACT_TAG            = "Inbound Gmail"

# Domini da ignorare (provider email comuni → non aziendali)
IGNORED_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "icloud.com", "me.com", "aol.com",
    "libero.it", "virgilio.it", "tiscali.it", "alice.it",
    "msn.com", "protonmail.com", "fastmail.com",
}
