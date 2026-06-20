"""
Script di autenticazione iniziale Gmail (da eseguire una sola volta localmente).
Genera il file token.json necessario per le esecuzioni successive.
"""
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def main():
    flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
    creds = flow.run_local_server(port=0)
    with open("token.json", "w") as f:
        f.write(creds.to_json())
    print("✓ token.json creato correttamente.")


if __name__ == "__main__":
    main()
