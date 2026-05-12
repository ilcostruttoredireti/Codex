# Gmail → HubSpot Contact Sync — Setup Guide

## Prerequisiti

- Python 3.10+
- Account Google con Gmail
- Account HubSpot con accesso Developer

---

## 1. Installa le dipendenze

```bash
pip install -r requirements.txt
```

---

## 2. Configura HubSpot

1. Vai su **HubSpot → Impostazioni → Integrazioni → App Private**
2. Crea una nuova app privata con gli scope:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
   - `crm.schemas.contacts.read`
   - `crm.objects.notes.write` *(per le attività timeline — opzionale)*
3. Copia il **token di accesso** generato

---

## 3. Configura Gmail (Google Cloud)

1. Vai su [Google Cloud Console](https://console.cloud.google.com/)
2. Crea un progetto o selezionane uno esistente
3. Abilita l'**API Gmail**
4. Crea credenziali OAuth 2.0 di tipo **"App desktop"**
5. Scarica il file JSON e salvalo come `credentials.json` nella root del progetto

---

## 4. Crea il file `.env`

```bash
cp .env.example .env
```

Modifica `.env`:

```dotenv
HUBSPOT_ACCESS_TOKEN=pat-eu1-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
GMAIL_CREDENTIALS_FILE=credentials.json
POLL_INTERVAL_SECONDS=60
MAX_EMAILS_PER_POLL=50
```

---

## 5. Prima autenticazione Gmail

Al primo avvio apre il browser per il consenso OAuth2:

```bash
python main.py --auth
```

Il token viene salvato in `token.json` e riutilizzato automaticamente.

---

## 6. Avvio

### Ciclo singolo (test)

```bash
python main.py --once
```

### Monitoraggio continuo

```bash
python main.py
```

Premi `Ctrl+C` per fermare.

---

## Output per ogni email processata

```
Stato: Creato | Email: mario.rossi@example.com | ID HubSpot: 12345678
Stato: Aggiornato | Email: giulia.bianchi@agency.it | ID HubSpot: 87654321
Stato: Ignorato | Email: no-reply@newsletter.com | Motivo: Mittente automatico
```

---

## Struttura del progetto

```
.
├── main.py                          # Entry point CLI
├── requirements.txt
├── .env.example
├── credentials.json                 # (non committare — aggiunto a .gitignore)
├── token.json                       # (generato automaticamente)
└── gmail_hubspot_sync/
    ├── __init__.py
    ├── config.py                    # Configurazione da variabili d'ambiente
    ├── gmail_client.py              # Autenticazione e polling Gmail
    ├── hubspot_client.py            # Ricerca, creazione e aggiornamento contatti
    └── sync.py                      # Logica di orchestrazione
```

---

## Logica anti-duplicati

- L'email del mittente viene usata come chiave univoca nella ricerca HubSpot.
- Se il contatto esiste, vengono aggiornati **solo i campi vuoti** — i dati esistenti non vengono sovrascritti.
- I mittenti automatici (`noreply`, `postmaster`, `newsletter`, ecc.) vengono ignorati prima di qualsiasi chiamata API.
- I messaggi già processati ricevono la label Gmail `hubspot-synced` per evitare la ri-elaborazione.

---

## Sicurezza

- **Non committare** `credentials.json` e `token.json` — sono già in `.gitignore`.
- Usa variabili d'ambiente o un secret manager in produzione.
- Il token HubSpot va trattato come una password.
