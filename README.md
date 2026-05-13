# Gmail → HubSpot Contact Sync

Monitora continuamente la casella Gmail e sincronizza automaticamente i mittenti come contatti HubSpot.

## Funzionamento

```
Email in arrivo → estrai mittente → cerca in HubSpot → Crea / Aggiorna / Ignora
```

Per ogni email processata viene stampato:
```
[Creato    ] email=mario@acme.it  hubspot_id=12345678
[Aggiornato] email=sara@firma.com hubspot_id=87654321
[Ignorato  ] email=noreply@...    hubspot_id=...
```

## Requisiti

- Python 3.11+
- Progetto Google Cloud con Gmail API abilitata
- HubSpot Private App con scope `crm.objects.contacts.write`

## Setup

### 1. Installa le dipendenze

```bash
pip install -r requirements.txt
```

### 2. Credenziali Gmail

1. Vai su [Google Cloud Console](https://console.cloud.google.com/)
2. Crea un progetto → abilita **Gmail API**
3. Crea credenziali **OAuth 2.0 (Desktop app)** → scarica `credentials.json`
4. Metti `credentials.json` nella root del progetto

### 3. Token HubSpot

1. In HubSpot: **Impostazioni → Integrazioni → App private**
2. Crea un'app privata con scope:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
   - `crm.objects.notes.write`
3. Copia il token generato

### 4. Configura .env

```bash
cp .env.example .env
# Modifica .env con i tuoi valori
```

```env
GMAIL_CREDENTIALS_FILE=credentials.json
GMAIL_TOKEN_FILE=token.json
HUBSPOT_API_KEY=pat-na1-xxxxxxxxxxxx
POLL_INTERVAL_SECONDS=60
STATE_FILE=sync_state.json
```

### 5. Prima esecuzione (autorizzazione OAuth)

Al primo avvio si apre il browser per autorizzare l'accesso Gmail. Il token viene salvato in `token.json` e riutilizzato nelle esecuzioni successive.

```bash
python main.py
```

## Logica di deduplicazione

| Caso | Azione |
|------|--------|
| Contatto non esiste | Creato con tutti i campi disponibili |
| Contatto esiste, campi vuoti | Aggiornato solo i campi mancanti |
| Contatto esiste, dati completi | Ignorato (nessuna modifica) |

- La chiave univoca è sempre l'**indirizzo email**
- I mittenti automatici (`noreply`, `no-reply`, ecc.) vengono ignorati
- La **fonte contatto** è sempre impostata a `"Gmail"`

## Campi HubSpot compilati

| Campo HubSpot | Fonte |
|---------------|-------|
| `email` | Header `From` |
| `firstname` | Nome visualizzato nell'header `From` |
| `lastname` | Cognome dal nome visualizzato |
| `company` | Dominio email (es. `acme.it` → `Acme`) |
| `leadsource` | Fisso: `"Gmail"` |

La nota sull'attività registra: oggetto email, data, mittente.

## Stato persistente

Il file `sync_state.json` salva l'ultimo `historyId` Gmail processato.  
In caso di riavvio, il sync riprende da dove si era fermato senza riprocessare email già viste.

## Esecuzione in background (Linux/macOS)

```bash
nohup python main.py >> sync.log 2>&1 &
```

Oppure usa `systemd` o un container Docker per un'esecuzione continua in produzione.
