"""
One-time OAuth2 setup for Gmail access.

Run this once on a machine with a browser to generate token.json.
After that, gmail_hubspot_sync.py can refresh the token automatically.

Steps:
1. Go to https://console.cloud.google.com/apis/credentials
2. Create OAuth 2.0 Client ID (Desktop app)
3. Download the JSON and save as credentials.json in this directory
4. Run: python setup_gmail_oauth.py
"""

from google_auth_oauthlib.flow import InstalledAppFlow
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

def main():
    if not Path("credentials.json").exists():
        print("ERRORE: credentials.json non trovato.")
        print("Scaricalo da Google Cloud Console → APIs & Services → Credentials")
        return

    flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
    creds = flow.run_local_server(port=0)
    Path("token.json").write_text(creds.to_json())
    print("token.json creato con successo.")

if __name__ == "__main__":
    main()
