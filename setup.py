"""
Script di configurazione guidata per Gmail → HubSpot Sync.
Esegui con: python3 setup.py
"""

import json
import os
import sys
from pathlib import Path


def header(text: str) -> None:
    print(f"\n{'═'*60}")
    print(f"  {text}")
    print(f"{'═'*60}")


def step(n: int, text: str) -> None:
    print(f"\n[Passo {n}] {text}")


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"  → {prompt}{suffix}: ").strip()
    return value or default


def main():
    header("Gmail → HubSpot Sync — Setup Guidato")

    env_file = Path(".env")
    existing: dict[str, str] = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip()

    # ── HubSpot Token ──────────────────────────────────────────────────────────
    step(1, "HubSpot Private App Token")
    print("""
  1. Vai su HubSpot → Impostazioni (icona ⚙️ in alto a destra)
  2. Sinistra: Integrazioni → App private
  3. Crea app → nome "Gmail Sync"
  4. Scopes necessari:  crm.objects.contacts.read
                        crm.objects.contacts.write
                        crm.objects.notes.write
  5. Crea app → copia il token (inizia con pat-...)
    """)
    token = ask("Incolla il token HubSpot", existing.get("HUBSPOT_TOKEN", ""))

    # ── Intervallo polling ─────────────────────────────────────────────────────
    step(2, "Intervallo di polling")
    print("  Ogni quanti secondi controllare Gmail? (minimo consigliato: 30)")
    interval = ask("Intervallo in secondi", existing.get("POLL_INTERVAL", "60"))

    # ── Batch size ─────────────────────────────────────────────────────────────
    step(3, "Email per ciclo")
    batch = ask("Quante email elaborare per ciclo (max 20)", existing.get("GMAIL_BATCH_SIZE", "20"))

    # ── Domini da ignorare ─────────────────────────────────────────────────────
    step(4, "Domini da ignorare")
    print("  Email da questi domini saranno saltate (newsletter, notifiche automatiche).")
    default_ignored = "noreply.com,mailchimp.com,constantcontact.com,sendgrid.net"
    ignored = ask("Domini separati da virgola", existing.get("IGNORED_DOMAINS", default_ignored))

    # ── Salva .env ─────────────────────────────────────────────────────────────
    env_content = f"""# Gmail → HubSpot Sync — Configurazione
# Generato da setup.py

HUBSPOT_TOKEN={token}
POLL_INTERVAL={interval}
GMAIL_BATCH_SIZE={batch}
IGNORED_DOMAINS={ignored}
STATE_FILE=sync_state.json
"""
    env_file.write_text(env_content)
    print("\n  ✅ File .env salvato.")

    # ── Google credentials ─────────────────────────────────────────────────────
    step(5, "Credenziali Google OAuth2")
    if not Path("credentials.json").exists():
        print("""
  Le credenziali Google OAuth2 non sono presenti.
  Segui questi passaggi:

  1. Vai su https://console.cloud.google.com/
  2. Crea un progetto (o selezionane uno esistente)
  3. Attiva l'API Gmail: API & Servizi → Libreria → "Gmail API" → Abilita
  4. Crea credenziali OAuth:
     API & Servizi → Credenziali → Crea credenziali → ID client OAuth 2.0
     Tipo applicazione: "App desktop"
  5. Scarica il file JSON → rinominalo "credentials.json"
  6. Mettilo nella stessa cartella di questo script

  Poi riesegui: python3 gmail_hubspot_sync.py
        """)
    else:
        print("\n  ✅ credentials.json trovato.")

    # ── Installa dipendenze ────────────────────────────────────────────────────
    step(6, "Installazione dipendenze")
    install = ask("Installare le dipendenze Python ora? (s/n)", "s")
    if install.lower() in ("s", "y", "si", "yes"):
        os.system(f"{sys.executable} -m pip install -r requirements.txt -q")
        print("  ✅ Dipendenze installate.")

    # ── Riepilogo ──────────────────────────────────────────────────────────────
    header("Setup completato!")
    print("""
  Per avviare la sincronizzazione:

      python3 gmail_hubspot_sync.py

  Al primo avvio, si aprirà il browser per autorizzare l'accesso a Gmail.
  Le credenziali verranno salvate in token.json per i successivi avvii.

  Output per ogni email processata:
      ✅ [Creato   ]  contatto@email.com   ID: 12345678
      🔄 [Aggiornato]  altro@email.com     ID: 87654321
      ⏭️  [Ignorato  ]  noreply@spam.com    mittente non valido

  Per fermarlo: Ctrl+C
    """)


if __name__ == "__main__":
    main()
