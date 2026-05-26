# Gmail → HubSpot Contact Sync

Monitora automaticamente la casella Gmail in arrivo ed estrae i mittenti,
sincronizzandoli come contatti in HubSpot (crea nuovi o aggiorna quelli esistenti).

## Funzionalità

| Feature | Descrizione |
|---|---|
| 📩 Parsing email dirette | Estrae nome + email dal campo `From:` |
| 📨 Parsing email inoltrate | Analizza il corpo per trovare il mittente originale (`Da:` / `From:`) |
| 🔍 Deduplicazione | Usa l'indirizzo email come chiave unica |
| ✅ Crea contatti | Se il mittente non esiste in HubSpot |
| 🔄 Aggiorna contatti | Compila solo i campi vuoti (non sovrascrive dati esistenti) |
| ⏭️ Ignora duplicati | Salta mittenti già processati nella stessa sessione |
| 🏢 Azienda dal dominio | Ricava automaticamente il nome azienda dal dominio email |
| 🔄 Loop continuo | Polling configurabile (default ogni 5 minuti) |

## Campi compilati in HubSpot

- `email` — indirizzo email (chiave univoca)
- `firstname` — nome (estratto dal display name)
- `lastname` — cognome (estratto dal display name)
- `company` — azienda (dal dominio email se non è provider generico)

## Installazione

```bash
# 1. Clona il repository
git clone <repo-url>
cd Codex

# 2. Installa le dipendenze
pip install -r requirements.txt

# 3. Copia e configura il file .env
cp .env.example .env
# → Modifica .env con il tuo HUBSPOT_ACCESS_TOKEN e le impostazioni Gmail

# 4. Configura Gmail OAuth
#    a. Vai su https://console.cloud.google.com
#    b. Crea un progetto (o usa uno esistente)
#    c. Abilita "Gmail API"
#    d. Crea credenziali → OAuth 2.0 → Desktop app
#    e. Scarica il file JSON e rinominalo in credentials.json
#    f. Copialo nella directory del progetto
```

## Configurazione HubSpot

1. Vai su **Impostazioni → Integrazioni → App private**
2. Crea una nuova app privata
3. Assegna gli scope: `crm.objects.contacts.read` e `crm.objects.contacts.write`
4. Copia il token nel file `.env`

## Utilizzo

### Modalità loop continuo (raccomandato)

```bash
python gmail_hubspot_sync.py
```

Controlla la casella ogni `POLL_INTERVAL_SECONDS` secondi (default: 5 minuti).
Al primo avvio analizza le ultime 7 giorni.

### Singola esecuzione (es: da cron)

```bash
# Ultima ora
python gmail_hubspot_sync.py --once

# Ultime 48 ore
python gmail_hubspot_sync.py --once --since=2880
```

### Esempio di cron job (ogni 10 minuti)

```cron
*/10 * * * * cd /path/to/Codex && python gmail_hubspot_sync.py --once >> sync.log 2>&1
```

## Output per ogni email processata

```
[STATO     ] email@esempio.com                              → ID: 123456789
```

| Stato | Significato |
|---|---|
| ✅ Creato | Nuovo contatto creato in HubSpot |
| 🔄 Aggiornato | Contatto esistente aggiornato con campi mancanti |
| ⏭️ Ignorato | Già processato, nella lista SKIP, o contatto già completo |

## Log

Tutti gli eventi vengono scritti sia su console che su file `gmail_hubspot_sync.log`.

## Struttura del progetto

```
Codex/
├── gmail_hubspot_sync.py   # Script principale
├── requirements.txt         # Dipendenze Python
├── .env.example             # Template configurazione
├── .env                     # Configurazione locale (non committare!)
├── credentials.json         # OAuth Google (non committare!)
└── README.md                # Questa documentazione
```

## Sicurezza

> ⚠️ **Non committare mai** `.env`, `credentials.json` o `token.json` nel repository.
> Il file `.gitignore` li esclude automaticamente.
