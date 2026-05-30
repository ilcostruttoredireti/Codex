# Gmail → HubSpot Contact Sync — Setup

## 1. Credenziali Gmail (OAuth 2.0)

1. Vai su [Google Cloud Console](https://console.cloud.google.com/)
2. Crea un progetto → Abilita **Gmail API**
3. Credenziali → **OAuth 2.0 Desktop app** → Scarica `credentials.json`
4. Posiziona `credentials.json` nella stessa cartella dello script
5. Al primo avvio si aprirà il browser per l'autorizzazione; il token viene salvato in `token.pickle`

## 2. HubSpot Private App Token

1. Vai su [HubSpot Private Apps](https://app.hubspot.com/private-apps)
2. Crea una nuova app con questi scopes:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
   - `crm.objects.notes.write`
3. Copia il token generato

## 3. Configurazione variabili d'ambiente

```bash
cp .env.example .env
# Modifica .env con il tuo token HubSpot e la tua email
```

Oppure esporta direttamente:

```bash
export HUBSPOT_ACCESS_TOKEN="pat-eu1-..."
export MY_EMAIL="tua@email.it"
```

## 4. Installazione dipendenze

```bash
pip install -r requirements.txt
```

## 5. Avvio

```bash
python gmail_hubspot_sync.py
```

Il processo gira in loop continuo (default: ogni 60 secondi).
Per fermarlo: `Ctrl+C` — lo stato dei messaggi processati viene salvato in `processed_messages.json`.

## Output per ogni email

| Stato | Significato |
|-------|-------------|
| `Creato` | Nuovo contatto aggiunto a HubSpot |
| `Aggiornato` | Contatto esistente aggiornato con campi mancanti |
| `Invariato` | Contatto già completo, nessuna modifica |
| `Ignorato` | Mittente di sistema (noreply, notifiche, ecc.) |

## Logica duplicati

- La chiave univoca è l'**email del mittente**
- Se esiste già un contatto con quella email → aggiorna solo i campi vuoti
- La fonte contatto viene impostata su **"Gmail"** (campo `leadsource`)
- Ogni email elaborata genera una **nota** associata al contatto in HubSpot con tag `Inbound Gmail`

## Esecuzione come servizio (systemd)

```ini
# /etc/systemd/system/gmail-hubspot-sync.service
[Unit]
Description=Gmail HubSpot Contact Sync
After=network.target

[Service]
WorkingDirectory=/path/to/Codex
EnvironmentFile=/path/to/Codex/.env
ExecStart=/usr/bin/python3 /path/to/Codex/gmail_hubspot_sync.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable gmail-hubspot-sync
sudo systemctl start gmail-hubspot-sync
```
