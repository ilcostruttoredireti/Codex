# Gmail → HubSpot Contact Sync

Monitora continuamente la casella Gmail e sincronizza automaticamente i mittenti come contatti in HubSpot, evitando duplicati e aggiornando i campi mancanti.

## Funzionamento

```
Gmail (History API) → estrai mittente → cerca in HubSpot → crea / aggiorna
```

Per ogni nuova email:
1. Estrae email, nome, cognome e dominio aziendale del mittente
2. Cerca il contatto in HubSpot per email (chiave unica)
3. Se non esiste → crea nuovo contatto con fonte "Gmail"
4. Se esiste → aggiorna solo i campi vuoti
5. Crea una nota attività "Inbound Gmail" associata al contatto
6. Stampa il report: **Creato / Aggiornato / Ignorato**

## Prerequisiti

- Python 3.11+
- Account Google Cloud con Gmail API abilitata
- Account HubSpot con App Privata (token di accesso)

---

## Setup

### 1. Installa le dipendenze

```bash
pip install -r requirements.txt
```

### 2. Credenziali Gmail

1. Vai su [Google Cloud Console](https://console.cloud.google.com/)
2. Crea un progetto → **API & Services** → **Enable APIs**
3. Abilita **Gmail API**
4. **Credentials** → **Create Credentials** → **OAuth 2.0 Client ID** → tipo *Desktop app*
5. Scarica il JSON e salvalo come `credentials.json` nella cartella del progetto

### 3. Token HubSpot

1. Vai su **HubSpot** → **Impostazioni** → **Integrazioni** → **App private**
2. Crea una nuova app privata con i seguenti scopes:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
   - `crm.objects.notes.write` *(per le attività — opzionale)*
3. Copia il token generato

### 4. Configura l'ambiente

```bash
cp .env.example .env
```

Modifica `.env`:

```env
HUBSPOT_ACCESS_TOKEN=pat-na1-il-tuo-token-qui
POLL_INTERVAL_SECONDS=60
GMAIL_LABEL=INBOX
```

---

## Avvio

```bash
python main.py
```

Alla **prima esecuzione** si aprirà il browser per autorizzare l'accesso Gmail.
Il token viene salvato in `token.json` (aggiunto al `.gitignore`).

### Output esempio

```
──────────────────────────────────────────────────────
  Gmail → HubSpot Contact Sync
  Intervallo polling: 60s | Label: INBOX
──────────────────────────────────────────────────────

[14:23:01] Processate 3 email
  Creati: 2 | Aggiornati: 1 | Ignorati: 0 | Errori: 0

  [Creato    ] mario.rossi@acme-corp.com                ID: 123456789
  [Creato    ] anna@example.it                          ID: 987654321
  [Aggiornato] luca@partner.com                         ID: 111222333
```

---

## Struttura del progetto

```
├── main.py           # Entry point — loop di polling
├── gmail_client.py   # Gmail API: autenticazione + estrazione mittenti
├── hubspot_client.py # HubSpot API: crea/aggiorna contatti + note
├── sync.py           # Logica di sincronizzazione e report
├── state_manager.py  # Persiste l'historyId su disco
├── config.py         # Configurazione da variabili d'ambiente
├── requirements.txt
├── .env.example
└── .gitignore
```

## File sensibili (NON committare)

| File | Contenuto |
|------|-----------|
| `credentials.json` | Client ID Google OAuth2 |
| `token.json` | Token di accesso Gmail |
| `.env` | Token HubSpot e configurazione |
| `.gmail_state.json` | Stato interno (historyId) |

Tutti sono già nel `.gitignore`.

## Filtri automatici

Il monitor ignora automaticamente:
- Indirizzi `noreply`, `no-reply`, `donotreply`, `bounce`
- Domìni di notifica (`noreply.github.com`, `accounts.google.com`, ecc.)
- `mailer-daemon`

Per aggiungere altri filtri, modifica `IGNORED_DOMAINS` e `IGNORED_EMAILS` in `config.py`.
