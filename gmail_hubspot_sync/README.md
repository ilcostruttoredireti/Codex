# 📧 Gmail → HubSpot Contact Sync

Monitora automaticamente le email in arrivo su Gmail, estrae i mittenti e li sincronizza come contatti in HubSpot.

## ✨ Funzionalità

| Feature | Dettaglio |
|---|---|
| 📥 Monitoraggio continuo | Polling Gmail ogni N secondi (configurabile) |
| 🔍 Estrazione smart | Riconosce mittenti reali anche nelle email inoltrate (Da: "Nome" email) |
| 🔄 Deduplicazione | Usa l'email come chiave unica: niente doppioni |
| ✅ Crea contatti | Nome, Cognome, Azienda (dal dominio), Fonte = Gmail |
| 🔄 Aggiorna esistenti | Compila solo i campi vuoti, non sovrascrive dati esistenti |
| 📊 Report per email | Stato: `CREATO` / `AGGIORNATO` / `IGNORATO` + ID HubSpot |

---

## 🚀 Setup rapido

### 1. Installa le dipendenze

```bash
pip install -r requirements.txt
```

### 2. Configura le credenziali

```bash
cp .env.example .env
# Edita .env con le tue chiavi
```

### 3. Credenziali Gmail (OAuth2)

1. Vai su [Google Cloud Console](https://console.cloud.google.com/)
2. Crea un progetto → Abilita **Gmail API**
3. Crea credenziali **OAuth 2.0 Desktop**
4. Scarica il file JSON → salvalo come `credentials.json` nella cartella del progetto

Al primo avvio si aprirà il browser per autorizzare l'accesso.

### 4. Token HubSpot

1. In HubSpot: **Settings → Integrations → Private Apps**
2. Crea una nuova app privata con scope:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
3. Copia il token → `HUBSPOT_API_KEY=pat-xx-...` nel file `.env`

---

## ▶️ Avvio

```bash
# Sync continuo (loop ogni 60s)
python sync.py

# Una sola esecuzione manuale
python -c "
from sync import GmailHubSpotSync
sync = GmailHubSpotSync()
results = sync.process_threads()
sync._print_report(results)
"
```

---

## 📊 Esempio output

```
═════════════════════════════════════════════════════════════════
STATO        EMAIL CONTATTO                         ID HUBSPOT
─────────────────────────────────────────────────────────────────
✅ CREATO     helelfiori@hotmail.it                  784999111222
✅ CREATO     grifone.pao@aeronautica.difesa.it      784999333444
🔄 AGGIORNATO alerenzetti11@gmail.com                614199897309
🔄 AGGIORNATO luanapioppi@gmail.com                  784135191745
⏭  IGNORATO   no-reply@accounts.google.com           Email di sistema
─────────────────────────────────────────────────────────────────
Totale: 32 | ✅ Creati: 2 | 🔄 Aggiornati: 28 | ⏭  Ignorati: 2
═════════════════════════════════════════════════════════════════
```

---

## ⚙️ Configurazione avanzata

| Variabile | Default | Descrizione |
|---|---|---|
| `GMAIL_QUERY` | `in:inbox -from:me newer_than:1d` | Filtro Gmail (sintassi nativa) |
| `POLL_INTERVAL_SECONDS` | `60` | Secondi tra un ciclo e l'altro |
| `GMAIL_CREDENTIALS_FILE` | `credentials.json` | Path credenziali OAuth |
| `GMAIL_TOKEN_FILE` | `token.json` | Path token salvato |

---

## 🔧 Logica di parsing email inoltrate

Le email inoltrate in italiano seguono il pattern:
```
Da "Nome Cognome" email@dominio.it
```

Il sistema riconosce automaticamente questo formato ed estrae il mittente **originale**, non il relay.

---

## 📋 Campi HubSpot popolati

| Campo HubSpot | Sorgente |
|---|---|
| `email` | Estratto da From / corpo forward |
| `firstname` | Prima parte del nome visualizzato |
| `lastname` | Ultima parte del nome visualizzato |
| `company` | Dedotto dal dominio email (es. `amacalabria.org` → `Amacalabria`) |
| `leadsource` | Sempre `OFFLINE_SOURCES` (etichettato "Gmail") |
