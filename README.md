# 📧 Gmail → HubSpot Contact Sync

Monitora automaticamente tutte le email in arrivo su Gmail, estrae i dati dei mittenti e li sincronizza in HubSpot, evitando duplicati e aggiornando i record esistenti.

---

## ✨ Funzionalità

| Feature | Dettaglio |
|---|---|
| 🔍 **Monitoraggio continuo** | Controlla la casella ogni N secondi (default: 60s) |
| 🤖 **Filtro automatico** | Ignora noreply, newsletter, sistemi automatici |
| 📨 **Email forwarded** | Estrae il mittente originale dalle email girate |
| 🔑 **Chiave unica** | Usa l'email come identificatore per evitare duplicati |
| ✏️ **Smart update** | Aggiorna solo i campi vuoti/mancanti in HubSpot |
| 🏷️ **Source tracking** | Imposta `hs_lead_source = "Gmail"` su ogni contatto |
| 📊 **Report live** | Mostra CREATO / AGGIORNATO / IGNORATO per ogni email |

---

## 🚀 Setup

### 1. Prerequisiti

```bash
pip install -r requirements.txt
```

### 2. Google Cloud Console (Gmail OAuth)

1. Vai su [console.cloud.google.com](https://console.cloud.google.com)
2. Crea un progetto → Abilita **Gmail API**
3. Vai su **APIs & Services → Credentials**
4. Crea **OAuth 2.0 Client ID** tipo "Desktop App"
5. Scarica il file JSON e rinominalo in `credentials.json`

### 3. HubSpot Private App Token

1. HubSpot → **Impostazioni → Integrazioni → App Private**
2. Crea app con scope:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
3. Copia il token generato

### 4. Configura le variabili

```bash
cp .env.example .env
# Modifica .env con il tuo HUBSPOT_ACCESS_TOKEN
```

---

## ▶️ Utilizzo

### Monitoraggio continuo (loop)

```bash
python gmail_hubspot_sync.py --hubspot-token "pat-na1-xxx..."
```

Il primo avvio apre il browser per l'autorizzazione Gmail. Il token viene salvato in `token.json` per gli avvii successivi.

### Opzioni

```
--hubspot-token TOKEN   Token HubSpot (o var HUBSPOT_ACCESS_TOKEN)
--interval SECONDI      Intervallo polling (default: 60)
--once                  Esegui una volta sola (per cron/debug)
```

### Esecuzione singola (debug / cron)

```bash
HUBSPOT_ACCESS_TOKEN="pat-na1-xxx" python gmail_hubspot_sync.py --once
```

Output JSON:
```json
[
  { "email": "nome@example.com", "status": "CREATO",     "hubspot_id": "123456" },
  { "email": "altro@example.com", "status": "AGGIORNATO", "hubspot_id": "789012" },
  { "email": "auto@noreply.com", "status": "IGNORATO",  "reason": "Email automatica" }
]
```

### Con cron (ogni 5 minuti)

```cron
*/5 * * * * cd /path/to/project && HUBSPOT_ACCESS_TOKEN="..." python gmail_hubspot_sync.py --once >> sync.log 2>&1
```

---

## 📋 Logica di sincronizzazione

```
Email in arrivo
       │
       ▼
  È automatica? (noreply, newsletter...)
       │ Sì ──→ IGNORATO
       │ No
       ▼
  È forwarded?
       │ Sì ──→ estrai mittente originale
       │
       ▼
  Ricerca in HubSpot per email
       │
       ├── NON ESISTE → CREA contatto
       │     ├── email
       │     ├── nome / cognome
       │     ├── azienda (dal dominio)
       │     └── hs_lead_source = "Gmail"
       │
       └── ESISTE → verifica campi vuoti
             ├── aggiungi campi mancanti
             └── se tutto completo → IGNORATO
```

---

## 🏷️ Campi HubSpot mappati

| Campo HubSpot | Fonte |
|---|---|
| `email` | Header `From:` |
| `firstname` | Parte sinistra del nome mittente |
| `lastname` | Parte destra del nome mittente |
| `company` | Dominio email (es. `company.it` → "Company") |
| `hs_lead_source` | Fisso: `"Gmail"` |

---

## 🛡️ Filtri automatici

Il sistema ignora automaticamente:

- Indirizzi `noreply` / `no-reply`
- Notifiche da Google, LinkedIn, Facebook, Fiverr, ecc.
- Alias di sistema: `postmaster`, `mailer-daemon`, `bounce`
- Domini generici di piattaforme marketing

---

## 🗂️ File di progetto

```
├── gmail_hubspot_sync.py   # Script principale
├── requirements.txt         # Dipendenze Python
├── .env.example             # Template variabili d'ambiente
├── credentials.json         # OAuth Gmail (da scaricare, non committare)
├── token.json               # Token Gmail (generato automaticamente)
└── README.md                # Questa guida
```

> ⚠️ **Non committare mai** `credentials.json` e `token.json` — aggiungili al `.gitignore`
