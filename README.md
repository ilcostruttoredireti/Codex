# Gmail → HubSpot Contact Sync

Sistema automatico che monitora le email in arrivo su Gmail, estrae i dati del mittente e li sincronizza come contatti in HubSpot CRM — evitando duplicati e aggiornando i dati esistenti.

---

## Funzionalità

| Funzione | Dettaglio |
|---|---|
| **Monitoraggio continuo** | Polling Gmail configurabile (default: ogni 60s) |
| **Estrazione mittente** | Email, nome, cognome, dominio aziendale |
| **Anti-duplicati** | L'email è la chiave univoca in HubSpot |
| **Aggiornamento intelligente** | Aggiorna solo i campi vuoti, non sovrascrive |
| **Label Gmail** | Applica `HubSpot-Synced` ai messaggi processati |
| **Timeline HubSpot** | Aggiunge una nota con i dettagli dell'email |
| **Tag contatto** | Imposta `hs_lead_status = "Inbound Gmail"` |
| **Filtraggio domini** | Ignora domìni di bot/newsletter configurabili |
| **Report ciclo** | Log colorato con stato per ogni email |

---

## Architettura

```
gmail_hubspot_sync/
├── main.py              # Entry point (loop / once / dry-run)
├── sync_engine.py       # Orchestratore del ciclo di polling
├── gmail_client.py      # Wrapper API Gmail (OAuth 2.0)
├── hubspot_client.py    # Wrapper API HubSpot v3
├── config.py            # Configurazione da .env
├── logger.py            # Logger colorato + rotante
├── setup_oauth.py       # Setup guidato al primo avvio
├── requirements.txt
├── .env.example
└── tests/
    └── test_gmail_parser.py
```

### Flusso dati

```
Gmail INBOX
    │
    ▼
GmailClient.fetch_new_messages()
    │   — header From/Reply-To parsing
    │   — filtra domìni ignorati
    ▼
SenderInfo(email, nome, dominio, ...)
    │
    ▼
HubSpotClient.sync_sender()
    ├── find_contact_by_email()
    │       ├── [non trovato] → create_contact()    → 🟢 CREATED
    │       └── [trovato]     → update_contact()    → 🔵 UPDATED / ⚪ IGNORED
    └── _add_timeline_note()
    │
    ▼
GmailClient.mark_as_processed()  — label HubSpot-Synced
    │
    ▼
CycleReport → log di riepilogo
```

---

## Requisiti

- Python 3.11+
- Account Google con Gmail API abilitata
- Account HubSpot con un'App Privata configurata

---

## Installazione

```bash
# 1. Clona il repository
git clone <repo-url>
cd gmail_hubspot_sync

# 2. Installa le dipendenze
pip install -r requirements.txt

# 3. Crea il file di configurazione
cp .env.example .env
# → Compila GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, HUBSPOT_ACCESS_TOKEN

# 4. Setup OAuth (una sola volta — apre il browser)
python setup_oauth.py
```

---

## Configurazione

### Gmail — Google Cloud Console

1. Vai su [console.cloud.google.com](https://console.cloud.google.com)
2. Crea un progetto → **API e servizi → Libreria** → abilita **Gmail API**
3. **Credenziali → Crea credenziali → ID client OAuth 2.0**
   - Tipo applicazione: **App desktop**
4. Scarica il JSON e salvalo come `credentials.json`  
   **oppure** imposta `GMAIL_CLIENT_ID` e `GMAIL_CLIENT_SECRET` nel `.env`

### HubSpot — App Privata

1. In HubSpot: **Impostazioni → Integrazioni → App private → Crea app privata**
2. Nome: es. `Gmail Sync`
3. **Scopes necessari:**
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
   - `crm.schemas.contacts.read` *(opzionale)*
4. Copia il **Token di accesso** nel `.env` come `HUBSPOT_ACCESS_TOKEN`

### File .env — tutte le variabili

```dotenv
# Gmail OAuth
GMAIL_CLIENT_ID=...
GMAIL_CLIENT_SECRET=...
GMAIL_TOKEN_FILE=token.json
GMAIL_CREDENTIALS_FILE=credentials.json

# HubSpot
HUBSPOT_ACCESS_TOKEN=...

# Sync
POLL_INTERVAL_SECONDS=60        # Frequenza polling
PROCESSED_LABEL=HubSpot-Synced  # Label Gmail da applicare
IGNORE_DOMAINS=noreply.com,no-reply.com,mailchimp.com,sendgrid.net
CONTACT_SOURCE=Gmail
CONTACT_TAG=Inbound Gmail
LOG_FILE=sync.log
```

---

## Utilizzo

```bash
# Loop continuo (produzione)
python main.py

# Singolo ciclo poi esci
python main.py --once

# Dry-run: mostra cosa farebbe senza modificare nulla
python main.py --dry-run
```

### Esempio output

```
10:23:01 [INFO] 🚀  Avvio sync continua — polling ogni 60s
10:23:05 [INFO] 📬  Trovati 3 nuovi messaggi da processare.
10:23:06 [INFO] ────────────────────────────────────────────────────────────
10:23:06 [INFO] 📊  Ciclo completato in 1.2s — Tot=3  🟢Creati=2  🔵Aggiornati=1  ⚪Ignorati=0
10:23:06 [INFO] 🟢 [CREATED]  marco.verdi@startup.io  id=12345678
10:23:06 [INFO] 🟢 [CREATED]  sara.neri@consulting.com  id=12345679
10:23:06 [INFO] 🔵 [UPDATED]  luca.bianchi@acme.com  id=87654321  (campi aggiornati: firstname, company)
10:23:06 [INFO] ────────────────────────────────────────────────────────────
10:23:06 [INFO] ⏳  Prossimo ciclo tra 60s...
```

---

## Campi HubSpot compilati

| Proprietà HubSpot | Fonte |
|---|---|
| `email` | Header `From` |
| `firstname` | Nome estratto dall'header |
| `lastname` | Cognome estratto dall'header |
| `company` | Dominio email → nome azienda |
| `leadsource` | `"Gmail"` (fisso) |
| `hs_lead_status` | `"Inbound Gmail"` (tag) |

---

## Come produzione (systemd)

```ini
# /etc/systemd/system/gmail-hubspot-sync.service
[Unit]
Description=Gmail → HubSpot Contact Sync
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/opt/gmail-hubspot-sync
ExecStart=/usr/bin/python3 main.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now gmail-hubspot-sync
sudo journalctl -u gmail-hubspot-sync -f
```

---

## Test

```bash
python -m pytest tests/ -v
```

---

## Licenza

MIT
