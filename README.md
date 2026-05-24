# 📬 Gmail → HubSpot Contact Sync

Monitora continuamente la casella Gmail in arrivo ed esegue la sincronizzazione automatica dei mittenti come contatti in HubSpot.

---

## ✨ Funzionalità

| Feature | Dettaglio |
|---|---|
| 📥 Monitoraggio Gmail | Polling continuo sulla inbox (`in:inbox`) |
| 🔍 Estrazione mittente | Email, nome, cognome, dominio, azienda |
| 🔗 Sincronizzazione HubSpot | Crea nuovi contatti o aggiorna quelli esistenti |
| 🚫 Anti-duplicati | Usa l'email come chiave univoca |
| 🔄 Aggiornamento selettivo | Sovrascrive solo i campi vuoti nel contatto esistente |
| 🏷️ Label Gmail | Applica `HubSpot-Synced` ai messaggi già processati |
| 📋 Timeline HubSpot | Registra l'email come attività sul contatto (via Engagements API) |
| 🏢 Rilevamento azienda | Ricava il nome azienda dal dominio email (domini personali esclusi) |
| 📊 Riepilogo run | Creati / Aggiornati / Ignorati / Errori per ogni passata |

---

## 🗂️ Struttura del progetto

```
gmail_hubspot_sync/
├── main.py             # Entry point + scheduler
├── sync.py             # Orchestratore sincronizzazione
├── gmail_client.py     # Gmail API wrapper (OAuth2)
├── hubspot_client.py   # HubSpot API wrapper
├── models.py           # Dataclass condivisi
├── config.py           # Configurazione da variabili d'ambiente
├── logger.py           # Logger coloured + file rotation
├── requirements.txt    # Dipendenze Python
├── run.sh              # Script avvio con virtualenv
├── .env.example        # Template variabili d'ambiente
└── tests/
    ├── test_gmail_client.py
    ├── test_hubspot_client.py
    └── test_sync.py
```

---

## 🚀 Setup rapido

### 1 · Prerequisiti

- Python 3.11+
- Account Google con Gmail
- Account HubSpot con accesso alla API

### 2 · Google Cloud Console

1. Vai su [console.cloud.google.com](https://console.cloud.google.com)
2. Crea (o seleziona) un progetto
3. Abilita la **Gmail API**: *APIs & Services → Enable APIs → Gmail API*
4. Crea credenziali OAuth 2.0: *Credentials → Create Credentials → OAuth client ID → Desktop app*
5. Scarica il file JSON e salvalo come `credentials.json` nella cartella `gmail_hubspot_sync/`

### 3 · HubSpot Private App

1. Vai su *HubSpot → Settings → Integrations → Private Apps*
2. Crea una nuova app con gli scope:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
   - `crm.schemas.contacts.read`
   - `engagements.write` *(opzionale – per le attività timeline)*
3. Copia il token generato

### 4 · Configurazione

```bash
cd gmail_hubspot_sync
cp .env.example .env
# Modifica .env con i tuoi valori
```

Variabili obbligatorie in `.env`:

```dotenv
HUBSPOT_ACCESS_TOKEN=pat-na1-xxxx...
GMAIL_CREDENTIALS_PATH=credentials.json
```

### 5 · Installazione dipendenze

```bash
cd gmail_hubspot_sync
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Oppure usa lo script wrapper:

```bash
chmod +x run.sh
./run.sh
```

---

## ▶️ Utilizzo

### Esecuzione singola

```bash
python main.py
```

### Loop continuo (ogni 60 secondi)

```bash
python main.py --loop
```

### Loop con intervallo personalizzato (es. ogni 5 minuti)

```bash
python main.py --loop --interval 300
```

### Senza attività timeline

```bash
python main.py --loop --no-activity
```

### Prima esecuzione

Al primo avvio verrà aperto il browser per il login Google OAuth2.  
Il token viene salvato in `token.json` per le esecuzioni successive.

---

## 📤 Output per ogni email processata

```
2024-01-15 10:32:01 [INFO    ] Processing message from: mario@acme.it  subject: 'Richiesta informazioni'
2024-01-15 10:32:02 [INFO    ]   ✅ CREATO   mario@acme.it                           id=12345
2024-01-15 10:32:03 [INFO    ]   🔄 AGGIORNATO luigi@bigcorp.com                     id=67890
2024-01-15 10:32:03 [INFO    ]   ⏭  IGNORATO  anna@gmail.com                         id=11111 (nessun campo da aggiornare)
```

### Riepilogo

```
── Riepilogo sincronizzazione ──────────────────────────────────
  Messaggi elaborati : 12
  ✅ Creati          : 3
  🔄 Aggiornati      : 5
  ⏭  Ignorati        : 4
  ❌ Errori          : 0
────────────────────────────────────────────────────────────────
```

---

## 🔧 Configurazione avanzata

| Variabile | Default | Descrizione |
|---|---|---|
| `POLL_INTERVAL_SECONDS` | `60` | Intervallo polling in secondi |
| `GMAIL_PROCESSED_LABEL` | `HubSpot-Synced` | Label Gmail applicata ai messaggi elaborati |
| `SYNC_SINCE_DATE` | *(vuoto)* | Elabora solo email dopo questa data (ISO 8601) |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_FILE` | `sync.log` | Percorso file di log (rotazione automatica 5 MB) |

---

## 🧪 Test

```bash
cd gmail_hubspot_sync
python -m pytest tests/ -v
```

---

## 🏗️ Architettura

```
main.py (scheduler)
  └── GmailHubSpotSyncer.run_once()          [sync.py]
        ├── GmailClient.fetch_new_messages()  [gmail_client.py]
        │     └── Gmail API (OAuth2)
        ├── HubSpotClient.find_contact()      [hubspot_client.py]
        │     └── HubSpot Search API
        ├── HubSpotClient.create_contact()
        │     └── HubSpot Contacts API
        ├── HubSpotClient.update_contact()
        │     └── HubSpot Contacts API
        ├── HubSpotClient.create_email_activity()
        │     └── HubSpot Engagements API
        └── GmailClient.mark_as_processed()   [gmail_client.py]
              └── Gmail Labels API
```

---

## ⚠️ Note di sicurezza

- **Non committare mai** `credentials.json` e `token.json` (già in `.gitignore`)
- Usa HubSpot **Private App** (non API key deprecata)
- Il token OAuth Gmail viene rinnovato automaticamente

---

## 📄 Licenza

MIT
