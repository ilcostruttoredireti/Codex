# 📬 Gmail → HubSpot Contact Sync

Monitora automaticamente la inbox Gmail, estrae i mittenti e li sincronizza come contatti in HubSpot — evitando duplicati e aggiornando i dati esistenti.

---

## Funzionalità

| Feature | Dettaglio |
|---|---|
| 🔍 Monitoraggio Gmail | Polling continuo della INBOX ogni N secondi |
| 👤 Estrazione mittente | Email, nome, cognome, dominio, azienda |
| 🔄 Deduplicazione | Usa l'email come chiave univoca in HubSpot |
| ✅ Creazione contatto | Se non esiste → crea con tutti i campi disponibili |
| 🔄 Aggiornamento | Se esiste → aggiorna solo i campi vuoti |
| 🏷️ Fonte contatto | Imposta `leadsource = "Gmail"` |
| 📝 Tag | Aggiunge nota `"Inbound Gmail"` al contatto |
| 📅 Timeline activity | Crea una nota con i dettagli dell'email ricevuta |
| 🧠 Stato persistente | File JSON locale → riprende dal punto di stop |
| 🖨️ Output colorato | `Creato / Aggiornato / Ignorato` per ogni email |

---

## Prerequisiti

- Python 3.11+
- Account Google con accesso Gmail
- Account HubSpot con chiave API / Private App Token

---

## Setup

### 1. Installa le dipendenze

```bash
cd gmail_hubspot_sync
pip install -r requirements.txt
```

### 2. Configura HubSpot

1. Accedi a HubSpot → **Settings → Integrations → Private Apps**
2. Crea una nuova app con questi scope:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
   - `crm.objects.notes.write`
3. Copia il token generato

### 3. Configura Gmail OAuth2

1. Vai su [Google Cloud Console](https://console.cloud.google.com/)
2. Crea un progetto → abilita **Gmail API**
3. Crea credenziali **OAuth 2.0 → Desktop App**
4. Scarica il JSON e rinominalo `credentials.json`
5. Copialo nella cartella `gmail_hubspot_sync/`

### 4. Crea il file `.env`

```bash
cp .env.example .env
# Modifica .env con i tuoi valori reali
```

```env
HUBSPOT_API_KEY=pat-eu1-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
GMAIL_CREDENTIALS_FILE=credentials.json
POLL_INTERVAL_SECONDS=60
```

### 5. Prima esecuzione (autorizzazione Gmail)

```bash
python main.py --once
```

Al primo avvio si aprirà il browser per autorizzare l'accesso Gmail.  
Il token viene salvato in `token.json` e riutilizzato automaticamente.

---

## Utilizzo

### Loop continuo (monitoraggio in tempo reale)
```bash
python main.py
```

### Scansione singola
```bash
python main.py --once
```

### Backfill delle ultime N email
```bash
python main.py --backfill 200
```

### Simulazione (nessuna scrittura su HubSpot)
```bash
python main.py --dry-run
```

### Reset stato (riprocessa tutto)
```bash
python main.py --reset-state --once
```

---

## Output di esempio

```
📬 3 nuovi messaggi (14:32:05)
  ✅ Creato        mario.rossi@acme.it      ID: 12345678
  🔄 Aggiornato    luca@startup.io          ID: 87654321
  ⏭️  Ignorato      spam@gmail.com           ID: —  [dominio personale ignorato]

┌─────────────┬────┐
│ Stato       │ N° │
├─────────────┼────┤
│ Creato      │  1 │
│ Aggiornato  │  1 │
│ Ignorato    │  1 │
└─────────────┴────┘
```

---

## Architettura

```
gmail_hubspot_sync/
├── main.py            # Entry point, loop di monitoraggio
├── gmail_monitor.py   # Gmail API: autenticazione OAuth2 + fetch messaggi
├── hubspot_sync.py    # HubSpot API: ricerca / creazione / aggiornamento contatti
├── state_manager.py   # Stato persistente su JSON (ID messaggi processati)
├── config.py          # Configurazione da variabili d'ambiente
├── requirements.txt
├── .env.example
└── README.md
```

### Flusso dati

```
Gmail INBOX
    │
    ▼
GmailMonitor.fetch_inbox_messages()
    │  filtra per ID non ancora processati (StateManager)
    ▼
GmailMonitor.get_sender_info()
    │  estrae: email, nome, cognome, dominio, azienda
    ▼
HubSpotSync.sync_sender()
    ├─ find_contact_by_email()  ──→  esiste?
    │       YES: update_contact() → aggiorna campi vuoti
    │       NO:  create_contact() → crea nuovo contatto
    ├─ add_tag_to_contact()     ──→  aggiunge "Inbound Gmail"
    └─ log_email_activity()     ──→  crea nota timeline
    │
    ▼
StateManager.mark_processed()  →  salva ID in state.json
    │
    ▼
Output: Creato / Aggiornato / Ignorato
```

---

## Configurazione avanzata

### Domini ignorati

I mittenti da provider email personali (gmail.com, yahoo.com, libero.it, ecc.) vengono **ignorati automaticamente** per evitare contatti non qualificati.

Per personalizzare la lista, modifica `IGNORED_DOMAINS` in `config.py`.

### Come eseguire come servizio (systemd)

```ini
# /etc/systemd/system/gmail-hubspot-sync.service
[Unit]
Description=Gmail → HubSpot Contact Sync
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/opt/gmail_hubspot_sync
ExecStart=/usr/bin/python3 main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable gmail-hubspot-sync
sudo systemctl start gmail-hubspot-sync
sudo journalctl -u gmail-hubspot-sync -f
```

### Esecuzione con Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["python", "main.py"]
```

```bash
docker build -t gmail-hubspot-sync .
docker run -d \
  -v $(pwd)/credentials.json:/app/credentials.json \
  -v $(pwd)/token.json:/app/token.json \
  -v $(pwd)/state.json:/app/state.json \
  --env-file .env \
  gmail-hubspot-sync
```

---

## Log

Il file `sync.log` viene aggiornato ad ogni scansione con il log dettagliato delle operazioni.

```bash
tail -f sync.log
```
