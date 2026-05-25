# 📬 Gmail → HubSpot Contact Sync

Monitora automaticamente la tua casella Gmail e sincronizza i mittenti come contatti in HubSpot CRM, evitando duplicati e aggiornando solo i campi mancanti.

---

## ✨ Funzionalità

| Funzionalità | Dettaglio |
|---|---|
| 📥 Monitoraggio continuo | Controlla nuove email ogni N secondi (configurabile) |
| 👤 Estrazione contatto | Email, nome, cognome, dominio aziendale dal mittente |
| 🔍 Deduplicazione | Usa l'email come chiave unica su HubSpot |
| ✅ Crea contatto | Se non esiste, lo crea con sorgente "Gmail" |
| 🔄 Aggiorna contatto | Se esiste, integra solo i campi vuoti |
| 🚫 Filtri smart | Ignora noreply, mailer-daemon, email proprie, domini pubblici |
| 📊 Output strutturato | Stato (Creato/Aggiornato/Ignorato), email, ID HubSpot |

---

## 🚀 Installazione

### 1. Installa le dipendenze

```bash
pip install -r requirements.txt
```

### 2. Configura Gmail (OAuth2)

1. Vai su [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
2. Crea un progetto → abilita **Gmail API**
3. Crea credenziali **OAuth 2.0** (tipo: "App desktop")
4. Scarica il file JSON → salvalo come `credentials.json` nella root del progetto

### 3. Configura HubSpot (Private App)

1. In HubSpot: *Impostazioni → Integrazioni → App private → Crea*
2. Assegna gli scope:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
3. Copia il **token di accesso**

### 4. Crea il file `.env`

```bash
cp .env.example .env
```

Modifica `.env` con i tuoi valori:

```env
GMAIL_CREDENTIALS_FILE=credentials.json
HUBSPOT_ACCESS_TOKEN=pat-na1-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# Opzionale
POLL_INTERVAL_SECONDS=60
LOG_LEVEL=INFO
IGNORE_DOMAINS=newsletter.com,noreply.acme.com
```

---

## ▶️ Utilizzo

```bash
# Monitoraggio continuo (Ctrl+C per fermare)
python main.py

# Singola iterazione (per cron job)
python main.py --once

# Dry-run: legge Gmail ma NON scrive su HubSpot
python main.py --dry-run

# Versione
python main.py --version
```

**Prima esecuzione:** il programma aprirà il browser per il consenso OAuth Gmail.  
Le credenziali vengono salvate in `token.json` per le esecuzioni successive.

---

## 📋 Output esempio

```
✅ [Creato]     | email=mario@acme.com        | id=198432101
🔄 [Aggiornato] | email=giulia@startup.io     | id=198432088 | (campi: company)
⏭️  [Ignorato]   | email=newsletter@example.com | id=198431990 | (dati già presenti)
❌ [Errore]     | email=unknown@domain.net    | (403 Forbidden)
```

---

## 🗂️ Struttura progetto

```
Codex/
├── main.py                      # Entrypoint CLI
├── requirements.txt
├── .env.example                 # Template variabili d'ambiente
├── .gitignore
├── gmail_hubspot_sync/
│   ├── __init__.py
│   ├── config.py                # Configurazione centralizzata
│   ├── gmail_client.py          # Gmail OAuth2 + History API
│   ├── hubspot_client.py        # HubSpot CRM API v3
│   ├── sync.py                  # Orchestratore ciclo di sync
│   └── models.py                # Modelli dati condivisi
└── tests/
    ├── test_gmail_client.py     # Test parsing email
    └── test_hubspot_client.py   # Test logica sync HubSpot
```

---

## 🏗️ Architettura

```
┌─────────────────┐     Gmail History API      ┌─────────────────────┐
│   Gmail API     │ ──── nuovi messaggi ────▶  │   GmailClient       │
│  (OAuth2)       │                             │  get_new_senders()  │
└─────────────────┘                             └────────┬────────────┘
                                                          │ SenderInfo
                                                          ▼
                                                ┌─────────────────────┐
                                                │   GmailHubSpotSync  │
                                                │     sync.py         │
                                                └────────┬────────────┘
                                                          │
                                                          ▼
┌─────────────────┐     HubSpot CRM API v3     ┌─────────────────────┐
│  HubSpot CRM    │ ◀── cerca / crea / aggiorna│   HubSpotClient     │
│  (Private App)  │                             │   sync_sender()     │
└─────────────────┘                             └─────────────────────┘
```

**Flusso per ogni email:**
1. `GmailClient.get_new_senders()` → legge i nuovi messaggi via History API
2. Estrae `From:` header → `SenderInfo` (email, nome, dominio)
3. Filtra noreply, domini di sistema, email propria
4. `HubSpotClient.sync_sender()` → cerca per email
5. Se non trovato → **crea** il contatto
6. Se trovato con campi vuoti → **aggiorna** solo quelli mancanti
7. Se tutti i dati già presenti → **ignora**

---

## ⚙️ Configurazione completa `.env`

| Variabile | Default | Descrizione |
|---|---|---|
| `GMAIL_CREDENTIALS_FILE` | `credentials.json` | Path file OAuth client secret |
| `GMAIL_TOKEN_FILE` | `token.json` | Path token salvato (auto-generato) |
| `GMAIL_USER_ID` | `me` | Account Gmail (`me` = autenticato) |
| `HUBSPOT_ACCESS_TOKEN` | — | **Obbligatorio** — Token Private App |
| `POLL_INTERVAL_SECONDS` | `60` | Secondi tra ogni controllo |
| `STATE_FILE` | `.gmail_sync_state.json` | File stato historyId |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `IGNORE_DOMAINS` | — | Domini da ignorare (separati da virgola) |
| `IGNORE_SELF` | `true` | Ignora email inviate da sé stesso |

---

## 🧪 Test

```bash
pytest tests/ -v
```

I test sono completamente isolati (nessuna chiamata reale a Gmail/HubSpot).

---

## 📝 Note

- **Sicurezza:** i file `credentials.json`, `token.json` e `.env` sono in `.gitignore` — non tracciarli mai nel repository.
- **historyId scaduto:** se il programma non gira per più di 30 giorni, Gmail invalida l'historyId. Il client lo rileva automaticamente e reimposta il baseline.
- **Rate limiting:** la Gmail History API ha limiti generosi (250 unità/utente/secondo); il polling ogni 60s è ampiamente sicuro.
