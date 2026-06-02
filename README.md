# Gmail → HubSpot Contact Sync

Monitora le email in arrivo su Gmail ed esegue automaticamente l'upsert dei mittenti come contatti in HubSpot CRM.

---

## Funzionamento

```
Gmail INBOX  →  estrai mittente  →  cerca in HubSpot
                                         │
                                   ┌─────┴──────┐
                                   │            │
                                esiste?       nuovo?
                                   │            │
                             aggiorna campi   crea contatto
                             mancanti         con tutti i campi
                                   │            │
                                   └─────┬──────┘
                                         │
                                  aggiungi nota timeline
                                  "Inbound Gmail"
```

Campi compilati in HubSpot:

| Campo HubSpot     | Fonte                                      |
|-------------------|--------------------------------------------|
| Email             | Indirizzo del mittente                     |
| Nome              | Display name (parte 1)                     |
| Cognome           | Display name (parte 2, se presente)        |
| Azienda           | Inferita dal dominio email                 |
| Lead Source       | `Gmail` (fisso)                            |
| Nota timeline     | Oggetto email, data, tag "Inbound Gmail"   |

---

## Setup

### 1. Credenziali Gmail (OAuth2)

1. Vai su [Google Cloud Console](https://console.cloud.google.com/)
2. Crea un progetto (o usa uno esistente)
3. Abilita **Gmail API** in *APIs & Services → Library*
4. In *APIs & Services → Credentials*, crea un **OAuth 2.0 Client ID** (tipo: Desktop app)
5. Scarica il file JSON e rinominalo `credentials.json`
6. Al primo avvio si aprirà il browser per autorizzare l'accesso — il token viene salvato in `token.json`

### 2. HubSpot Private App

1. In HubSpot: *Settings → Integrations → Private Apps → Create a private app*
2. Scope necessari:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
   - `crm.objects.notes.write`
3. Copia l'**Access Token** (inizia con `pat-`)

### 3. Configurazione

```bash
cp .env.example .env
# Modifica .env con le tue credenziali
```

### 4. Installazione dipendenze

```bash
pip install -r requirements.txt
```

---

## Utilizzo

```bash
# Modalità continua (polling ogni 60 secondi)
python sync.py

# Ciclo singolo ed esci
python sync.py --once

# Reset dello stato (rescansiona dall'inizio)
python sync.py --reset

# Reset + ciclo singolo
python sync.py --reset --once
```

### Output per ogni email processata

```
2025-06-02 10:23:45  INFO     [✓ CREATO ]  mario.rossi@acme.it                HubSpot ID: 12345
2025-06-02 10:23:46  INFO     [↑ AGGIORNATO]  luca.bianchi@beta.com            HubSpot ID: 67890
2025-06-02 10:23:47  INFO     [– IGNORATO]  noreply@notifications.com          HubSpot ID: —
```

---

## Come funziona internamente

- **Primo avvio**: scansiona gli ultimi `INITIAL_SCAN_LIMIT` messaggi in INBOX
- **Avvii successivi**: usa la [Gmail History API](https://developers.google.com/gmail/api/reference/rest/v1/users.history) per recuperare solo i messaggi nuovi dall'ultimo check (efficiente, minimal quota)
- **Stato**: salvato in `sync_state.json` — contiene l'ultimo `historyId` e gli ID dei messaggi già elaborati
- **Deduplicazione**: HubSpot viene interrogato per email prima di ogni create/update
- **No-reply**: mittenti con pattern `noreply`, `mailer-daemon`, `postmaster`, ecc. vengono saltati automaticamente

---

## Variabili d'ambiente

| Variabile              | Default                      | Descrizione                              |
|------------------------|------------------------------|------------------------------------------|
| `GMAIL_CREDENTIALS_FILE` | `credentials.json`         | File OAuth2 da Google Cloud Console      |
| `GMAIL_TOKEN_FILE`     | `token.json`                 | Token OAuth2 salvato dopo il primo login |
| `HUBSPOT_ACCESS_TOKEN` | —                            | Token Private App HubSpot                |
| `INITIAL_SCAN_LIMIT`   | `100`                        | Email da scansionare al primo avvio      |
| `POLL_INTERVAL_SECONDS`| `60`                         | Secondi tra un ciclo e l'altro           |
| `STATE_FILE`           | `sync_state.json`            | File di stato persistente                |
| `GENERIC_DOMAINS`      | `gmail.com,yahoo.com,...`    | Domini per cui non inferire azienda      |
| `SKIP_SENDERS`         | (vuoto)                      | Mittenti da ignorare sempre              |

---

## Esecuzione come servizio (systemd)

```ini
# /etc/systemd/system/gmail-hubspot-sync.service
[Unit]
Description=Gmail HubSpot Contact Sync
After=network.target

[Service]
WorkingDirectory=/path/to/Codex
ExecStart=/usr/bin/python3 sync.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now gmail-hubspot-sync
sudo journalctl -fu gmail-hubspot-sync
```
