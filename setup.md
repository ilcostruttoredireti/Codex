# Gmail → HubSpot Contact Sync — Guida al Setup

## Prerequisiti

- Python 3.11+
- Account Google con Gmail
- Account HubSpot con accesso alle API

---

## 1. Installa le dipendenze

```bash
pip install -r requirements.txt
```

---

## 2. Configura HubSpot

1. Vai su **HubSpot → Impostazioni → Integrazioni → App private**
2. Clicca **Crea app privata**
3. Inserisci nome (es. *Gmail Sync*) e descrizione
4. Nella scheda **Scope**, seleziona:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
   - `crm.objects.notes.write` *(per timeline, opzionale)*
5. Clicca **Crea app** e copia il **Token di accesso**

---

## 3. Configura Gmail OAuth2

1. Vai su [Google Cloud Console](https://console.cloud.google.com/)
2. Crea un progetto (o usa uno esistente)
3. Attiva l'API **Gmail API**: *API e servizi → Libreria → Gmail API*
4. Crea credenziali OAuth2:
   - *API e servizi → Credenziali → Crea credenziali → ID client OAuth*
   - Tipo applicazione: **App desktop**
5. Scarica il file JSON e salvalo come `credentials.json` nella cartella del progetto

---

## 4. Configura le variabili d'ambiente

```bash
cp .env.example .env
```

Modifica `.env`:

```env
HUBSPOT_ACCESS_TOKEN=pat-na1-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
POLL_INTERVAL_SECONDS=60
IGNORE_DOMAINS=noreply.com,no-reply.com,mailer-daemon.org
ADD_TIMELINE_NOTE=true
LOG_LEVEL=INFO
```

---

## 5. Prima esecuzione (autorizzazione Gmail)

```bash
python main.py --once
```

Al primo avvio si aprirà il browser per autorizzare l'accesso Gmail.  
Le credenziali vengono salvate in `token.json` per le esecuzioni successive.

Il primo ciclo **non processerà email storiche** — registra solo il punto di partenza.  
Dal secondo ciclo in poi monitora solo le email nuove.

---

## 6. Avvio del monitoraggio continuo

```bash
python main.py
```

Output per ogni email processata:

```
  ✓ [Creato]      mario.rossi@esempio.it  (ID: 12345678)
  ↺ [Aggiornato]  info@azienda.com        (ID: 87654321)
  – [Ignorato]    noreply@newsletter.com  — dominio/email escluso
```

---

## Opzioni CLI

| Opzione | Descrizione |
|---------|-------------|
| `python main.py` | Loop continuo (default) |
| `python main.py --once` | Esegui un ciclo e termina |
| `python main.py --reset` | Azzera lo stato, poi esce |
| `python main.py --reset --once` | Azzera e rielabora le email recenti |

---

## Come funziona

```
Gmail (INBOX)
     │
     ▼
Gmail History API ──► Nuovi messaggi dall'ultimo sync
     │
     ▼
Parser mittente ──► email, nome, cognome, dominio, azienda
     │
     ▼
HubSpot Search ──► Contatto esistente?
     │
     ├── SÌ ──► Aggiorna campi vuoti
     │
     └── NO ──► Crea nuovo contatto
                    │
                    ▼
             Campi compilati:
             - Email
             - Nome / Cognome
             - Azienda (dal dominio)
             - Fonte: "Gmail"
             + Nota timeline (email ricevuta)
```

---

## Esecuzione automatica (cron)

Per eseguirlo in background con riavvio automatico:

```bash
# Con systemd (Linux)
sudo nano /etc/systemd/system/gmail-hubspot-sync.service
```

```ini
[Unit]
Description=Gmail HubSpot Sync
After=network.target

[Service]
ExecStart=/usr/bin/python3 /path/to/main.py
WorkingDirectory=/path/to/project
Restart=always
RestartSec=10
User=yourusername

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable gmail-hubspot-sync
sudo systemctl start gmail-hubspot-sync
sudo journalctl -u gmail-hubspot-sync -f
```

---

## File generati

| File | Descrizione |
|------|-------------|
| `token.json` | Token OAuth2 Gmail (non committare) |
| `.sync_state.json` | Stato sync: history ID + ID messaggi processati |
| `.env` | Variabili d'ambiente (non committare) |
