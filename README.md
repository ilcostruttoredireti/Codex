# 📧 Gmail → HubSpot Contact Sync

Monitora automaticamente le email in arrivo su Gmail, estrae i contatti dai mittenti
e li sincronizza in HubSpot — evitando duplicati e aggiornando i dati mancanti.

## ✨ Funzionalità

| Funzione | Descrizione |
|---|---|
| 📥 Monitoraggio Gmail | Controlla continuamente le email in arrivo |
| 👤 Estrazione contatti | Nome, cognome, email, dominio aziendale |
| 🔍 Deduplicazione | Usa l'email come chiave univoca |
| ✅ Creazione contatto | Se non esiste in HubSpot |
| 🔄 Aggiornamento | Se esiste, aggiorna solo i campi mancanti |
| 📝 Timeline attività | Aggiunge nota "Email ricevuta via Gmail" |
| 🏷️ Fonte contatto | Imposta "Gmail" come lead source |
| 💾 Stato persistente | Ricorda l'ultima sincronizzazione |

## 🚀 Setup

### 1. Dipendenze Python

```bash
pip install -r requirements.txt
```

### 2. Credenziali Gmail (Google Cloud Console)

1. Vai su [Google Cloud Console](https://console.cloud.google.com/)
2. Crea un progetto (o usa uno esistente)
3. Abilita la **Gmail API** → _APIs & Services → Enable APIs_
4. Crea **OAuth 2.0 credentials** → _Credentials → Create Credentials → OAuth client ID_
   - Tipo: **Desktop application**
5. Scarica il file JSON e rinominalo `credentials.json`
6. Mettilo nella stessa directory dello script

### 3. Token HubSpot

1. Vai su [HubSpot → Private Apps](https://app.hubspot.com/private-apps)
2. Crea una nuova app con questi **scopes**:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
   - `crm.objects.notes.read`
   - `crm.objects.notes.write`
3. Copia il token generato

### 4. File .env

```bash
cp .env.example .env
# Modifica .env con i tuoi valori
nano .env
```

## 📋 Utilizzo

```bash
# Sincronizzazione singola (nuove email da ultima esecuzione)
python gmail_hubspot_sync.py

# Monitoraggio continuo (polling ogni 10 minuti)
python gmail_hubspot_sync.py --watch

# Polling ogni 5 minuti
python gmail_hubspot_sync.py --watch --interval 5

# Test senza scrivere su HubSpot
python gmail_hubspot_sync.py --dry-run

# Riprocessa tutte le email (azzera stato)
python gmail_hubspot_sync.py --reset
```

## 📊 Output

Per ogni email processata lo script mostra:

```
────────────────────────────────────────────────────────────────────────────────
STATO        EMAIL                                    NOME                      HUBSPOT ID
────────────────────────────────────────────────────────────────────────────────
✅ Creato     mario.rossi@studio.com                   Mario Rossi               123456789
🔄 Aggiornato giulia.bianchi@agenzia.it                Giulia Bianchi            987654321
⏭  Ignorato   info@azienda.com                         Azienda Srl               111222333
────────────────────────────────────────────────────────────────────────────────
Totale: 3 | ✅ Creati: 1 | 🔄 Aggiornati: 1 | ⏭  Ignorati: 1
```

## ⚙️ Configurazione avanzata

### Ignorare mittenti/domini

Nel file `gmail_hubspot_sync.py`, modifica le variabili:

```python
# Domini sempre ignorati
SKIP_DOMAINS = {"gmail.com", "noreply.com", ...}

# Email specifiche sempre ignorate
SKIP_EMAILS = {"noreply@accounts.google.com", ...}
```

> **Nota:** `gmail.com` è nella lista di skip per default perché spesso sono
> email personali non aziendali. Rimuovilo se vuoi anche i contatti Gmail personali.

### Come schedulare con cron

```bash
# Ogni 15 minuti
*/15 * * * * cd /path/to/project && python gmail_hubspot_sync.py >> cron.log 2>&1
```

## 🔍 Campi HubSpot compilati

| Campo HubSpot | Fonte |
|---|---|
| `email` | Header `From` |
| `firstname` | Prima parola del display name |
| `lastname` | Resto del display name |
| `company` | Estratto dal dominio email |
| `lead_source` | Sempre `"Gmail"` |

## 📁 File generati

| File | Descrizione |
|---|---|
| `token.json` | Token OAuth Gmail (generato al primo login) |
| `sync_state.json` | Timestamp ultima sync + IDs processati |
| `gmail_hubspot_sync.log` | Log completo delle esecuzioni |
