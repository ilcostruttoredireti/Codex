"""Verifica rapida della configurazione prima di avviare il sync."""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

errors = []
warnings = []

creds_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
if not Path(creds_file).exists():
    errors.append(
        f"GOOGLE_CREDENTIALS_FILE non trovato: '{creds_file}'\n"
        "  → Scarica le credenziali OAuth da Google Cloud Console e "
        "salvale come credentials.json"
    )

hubspot_token = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
if not hubspot_token:
    errors.append(
        "HUBSPOT_ACCESS_TOKEN non configurato\n"
        "  → Crea una Private App su HubSpot > Impostazioni > App Private\n"
        "     Scope richiesti: crm.objects.contacts.read, crm.objects.contacts.write"
    )
elif hubspot_token == "your_hubspot_private_app_token":
    errors.append("HUBSPOT_ACCESS_TOKEN è ancora il valore di esempio. Aggiornalo nel file .env")

poll_interval = os.getenv("POLL_INTERVAL", "60")
if not poll_interval.isdigit() or int(poll_interval) < 10:
    warnings.append(f"POLL_INTERVAL={poll_interval} è molto basso. Consigliato ≥ 30s.")

print("── Verifica configurazione ──────────────────────────────────────")
if errors:
    for e in errors:
        print(f"  [ERRORE] {e}")
    print()
    print("Correggi gli errori sopra, poi riavvia.")
    sys.exit(1)

if warnings:
    for w in warnings:
        print(f"  [AVVISO] {w}")

print("  [OK] Configurazione valida.")
print()
print("Prossimo passo: python gmail_hubspot_sync.py")
print("  Al primo avvio si aprirà il browser per autorizzare l'accesso Gmail.")
print("────────────────────────────────────────────────────────────────")
