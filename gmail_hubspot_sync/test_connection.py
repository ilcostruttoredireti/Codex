"""
Script di verifica rapida: controlla Gmail e HubSpot senza modificare nulla.
Esegui prima di avviare main.py per assicurarti che tutto sia configurato.

    python test_connection.py
"""

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

def check_gmail():
    from gmail_monitor import get_gmail_service
    creds = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    token = os.getenv("GMAIL_TOKEN_FILE", "token.json")
    try:
        svc = get_gmail_service(creds, token)
        profile = svc.users().getProfile(userId="me").execute()
        print(f"  Gmail OK — account: {profile['emailAddress']}")
        print(f"            messaggi totali: {profile.get('messagesTotal', '?')}")
        return True
    except Exception as e:
        print(f"  Gmail ERRORE: {e}")
        return False

def check_hubspot():
    from hubspot_sync import get_hubspot_client
    token = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
    if not token:
        print("  HubSpot ERRORE: HUBSPOT_ACCESS_TOKEN mancante nel .env")
        return False
    try:
        client = get_hubspot_client(token)
        result = client.crm.contacts.basic_api.get_page(limit=1)
        total = getattr(result, "total", "?")
        print(f"  HubSpot OK — contatti totali: {total}")
        return True
    except Exception as e:
        print(f"  HubSpot ERRORE: {e}")
        return False

if __name__ == "__main__":
    print("\n=== Test connessioni ===\n")
    g = check_gmail()
    h = check_hubspot()
    print()
    if g and h:
        print("Tutto OK — puoi avviare main.py")
    else:
        print("Correggi gli errori sopra prima di avviare main.py")
    print()
