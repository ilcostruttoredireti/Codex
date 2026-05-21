# Gmail → HubSpot Contact Sync — Setup

## Prerequisiti

- Python 3.11+
- Account Google con Gmail abilitato
- Account HubSpot con accesso alle API (Private App)

---

## 1. Google Cloud — Credenziali Gmail

1. Vai su [console.cloud.google.com](https://console.cloud.google.com)
2. Crea un progetto (o selezionane uno esistente)
3. **API & Services → Enable APIs** → abilita **Gmail API**
4. **API & Services → Credentials → Create Credentials → OAuth 2.0 Client ID**
   - Application type: **Desktop app**
5. Scarica il file JSON → rinominalo `gmail_credentials.json` e copialo nella cartella del progetto
6. In **OAuth consent screen** aggiungi il tuo indirizzo Gmail come utente di test

---

## 2. HubSpot — Private App Token

1. Vai su **Settings → Integrations → Private Apps → Create a private app**
2. Assegna gli scope:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
   - `crm.objects.notes.write` *(per le activity timeline)*
3. Copia il token generato

---

## 3. Configurazione

```bash
cp .env.example .env
# Modifica .env con i tuoi valori reali
```

---

## 4. Installazione dipendenze

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## 5. Prima esecuzione (autorizzazione Gmail)

```bash
source .venv/bin/activate
export $(cat .env | xargs)       # carica variabili d'ambiente

# Primo avvio: apre il browser per il consenso OAuth
python gmail_hubspot_sync.py --once
```

Dopo il primo accesso viene salvato `gmail_token.json` — le esecuzioni successive non richiedono il browser.

---

## 6. Utilizzo

### Singolo ciclo (test)
```bash
python gmail_hubspot_sync.py --once
```

### Monitoraggio continuo (ogni 60 secondi)
```bash
python gmail_hubspot_sync.py
```

### Intervallo personalizzato (ogni 5 minuti)
```bash
python gmail_hubspot_sync.py --interval 300
```

### Senza activity timeline
```bash
python gmail_hubspot_sync.py --no-activity
```

---

## 7. Esecuzione come servizio (Linux/systemd)

```ini
# /etc/systemd/system/gmail-hubspot-sync.service
[Unit]
Description=Gmail → HubSpot Contact Sync
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/Codex
EnvironmentFile=/path/to/Codex/.env
ExecStart=/path/to/Codex/.venv/bin/python gmail_hubspot_sync.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable gmail-hubspot-sync
sudo systemctl start gmail-hubspot-sync
sudo journalctl -fu gmail-hubspot-sync
```

---

## Output per ogni email processata

```
────────────────────────────────────────────────────────────
STATO        EMAIL                               HUBSPOT ID
────────────────────────────────────────────────────────────
Creato       mario.rossi@acmecorp.com            12345678
Aggiornato   anna.bianchi@startup.io             87654321
Ignorato     newsletter@noreply.com
────────────────────────────────────────────────────────────
  Creati: 1  |  Aggiornati: 1  |  Ignorati: 1  |  Errori: 0
────────────────────────────────────────────────────────────
```

---

## File di stato

- `sync_state.json` — tiene traccia degli ID messaggi già processati (evita duplicati al riavvio)
- `sync.log` — log completo di tutte le operazioni

---

## Logica anti-duplicati

Il contatto viene identificato **univocamente dall'indirizzo email**.
- Se il contatto **non esiste** → viene **creato** con tutti i campi disponibili
- Se il contatto **esiste già** → vengono **aggiornati solo i campi vuoti** (non sovrascrive dati esistenti)
- Se non c'è nulla da aggiornare → **Ignorato**
