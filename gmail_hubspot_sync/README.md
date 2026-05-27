# Gmail → HubSpot Contact Sync

Monitora continuamente le email in arrivo su Gmail ed estrae automaticamente i mittenti, sincronizzandoli come contatti in HubSpot.

## Funzionamento

```
Gmail (History API)
     │
     ▼  nuovi messaggi INBOX
┌────────────────────┐
│  Estrai mittente   │  →  email, nome, dominio aziendale
└────────────────────┘
     │
     ▼
┌────────────────────┐
│  Cerca in HubSpot  │  →  ricerca per email (chiave unica)
└────────────────────┘
     │
     ├── trovato → aggiorna campi mancanti (AGGIORNATO)
     │                oppure lascia invariato  (IGNORATO)
     │
     └── non trovato → crea contatto + nota (CREATO)
```

### Campi HubSpot compilati

| Campo HubSpot    | Fonte                                      |
|------------------|--------------------------------------------|
| `email`          | Header `From:` del messaggio               |
| `firstname`      | Display name (prima parola)                |
| `lastname`       | Display name (parole successive)           |
| `company`        | Snippet / nome azienda estratto dal dominio|
| `message`        | Tag fisso: `"Inbound Gmail"`               |
| Nota associata   | Oggetto email + dominio + tag              |

### Email ignorate automaticamente

- `noreply@*`, `no-reply@*`
- `mailer-daemon@*`
- `analytics-noreply@google.com`
- `postmaster@*`
- Mittenti interni (configurabili tramite `SKIP_PATTERNS`)

---

## Setup

### 1. Google Cloud — OAuth credentials

1. Vai su [console.cloud.google.com](https://console.cloud.google.com)
2. Crea un progetto (o selezionane uno esistente)
3. Attiva **Gmail API**
4. Crea credenziali → **ID client OAuth 2.0** → tipo *Desktop app*
5. Scarica il JSON e salvalo come `credentials.json`

### 2. HubSpot — Private App token

1. In HubSpot: *Impostazioni → Integrazioni → App private*
2. Crea una nuova App privata con i permessi:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
   - `crm.objects.notes.write`
3. Copia il token (`pat-…`) e inseriscilo in `.env`

### 3. Installazione dipendenze

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 4. Configurazione

```bash
cp .env.example .env
# Modifica .env con le tue credenziali
```

### 5. Primo avvio

```bash
python sync.py
```

Al primo avvio si aprirà il browser per l'autenticazione OAuth Gmail.  
Il token viene salvato in `token.json` per i successivi avvii.  
Il primo ciclo imposta l'`historyId` senza processare email (baseline).  
Dal secondo ciclo in poi vengono processate solo le nuove email.

---

## Output per ogni email processata

```
┌─────────────────────────────────────────────────────────────────┐
│ Sync completata — 2026-05-27 16:30:00                           │
├────────────┬─────────────────────────────────┬───────────────────┤
│  Stato     │  Email contatto                 │  ID HubSpot       │
├────────────┼─────────────────────────────────┼───────────────────┤
│ CREATO     │ info@gliamicidellebici.it        │ 123456789012      │
│ AGGIORNATO │ mpedicchio@ogs.it               │ 987654321098      │
│ IGNORATO   │ giorginimichela@gmail.com        │ 759072659643      │
└────────────┴─────────────────────────────────┴───────────────────┘
```

| Stato       | Significato                                              |
|-------------|----------------------------------------------------------|
| `CREATO`    | Nuovo contatto creato in HubSpot                         |
| `AGGIORNATO`| Contatto esistente aggiornato con campi mancanti         |
| `IGNORATO`  | Contatto già completo, nessuna modifica necessaria       |

---

## Avvio come servizio (Linux systemd)

Crea `/etc/systemd/system/gmail-hubspot-sync.service`:

```ini
[Unit]
Description=Gmail HubSpot Contact Sync
After=network.target

[Service]
Type=simple
User=tuo_utente
WorkingDirectory=/path/to/gmail_hubspot_sync
EnvironmentFile=/path/to/gmail_hubspot_sync/.env
ExecStart=/path/to/.venv/bin/python sync.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable gmail-hubspot-sync
sudo systemctl start gmail-hubspot-sync
sudo systemctl status gmail-hubspot-sync
```

## Variabili d'ambiente

| Variabile          | Default          | Descrizione                         |
|--------------------|------------------|-------------------------------------|
| `HUBSPOT_API_KEY`  | *(obbligatorio)* | Token Private App HubSpot           |
| `GMAIL_CREDS_FILE` | `credentials.json` | Credenziali OAuth Google           |
| `GMAIL_TOKEN_FILE` | `token.json`     | Token OAuth salvato                 |
| `POLL_INTERVAL_SEC`| `300`            | Secondi tra un ciclo e l'altro      |
| `STATE_FILE`       | `sync_state.json`| File di stato (historyId Gmail)     |
