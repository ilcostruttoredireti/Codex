# Gmail → HubSpot Contact Sync

Automazione che monitora la casella Gmail in arrivo, estrae i mittenti (diretti e da email inoltrate) e li sincronizza automaticamente come contatti in HubSpot.

## Funzionalità

| Feature | Dettaglio |
|---------|-----------|
| 📥 **Monitoraggio Gmail** | Analizza tutti i thread in arrivo degli ultimi N giorni |
| 🔍 **Estrazione mittenti** | Mittenti diretti + originali estratti da Fw:/Fwd: |
| 🔄 **Deduplicazione** | Usa l'email come chiave unica — mai duplicati |
| ✅ **Crea contatto** | Nuovo contatto HubSpot se non esiste |
| 🔄 **Aggiorna contatto** | Compila solo i campi vuoti (non sovrascrive dati esistenti) |
| 🏷️ **Label Gmail** | Applica il tag "Inbound Gmail" ai thread processati |
| 👁️ **Watch mode** | Polling continuo ogni 5 minuti |

## Installazione

```bash
# 1. Clona il repo
git clone https://github.com/ilcostruttoredireti/codex.git
cd codex

# 2. Crea e attiva virtualenv
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# 3. Installa dipendenze
pip install -r requirements.txt

# 4. Configura le credenziali
cp .env.example .env
# Modifica .env con i tuoi token
```

## Configurazione Gmail (OAuth2)

1. Vai su [Google Cloud Console](https://console.cloud.google.com)
2. Crea un progetto (o usa uno esistente)
3. Abilita **Gmail API**
4. Vai su **Credentials → OAuth 2.0 Client IDs → Create**
5. Tipo applicazione: **Desktop app**
6. Scarica il file `credentials.json` nella cartella del progetto
7. Al primo avvio si apre il browser per l'autorizzazione → viene salvato `gmail_token.json`

## Configurazione HubSpot (Private App)

1. In HubSpot: **Settings → Integrations → Private Apps → Create a private app**
2. Nome: `Gmail Sync`
3. Scope richiesti:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
4. Copia il token generato in `.env` come `HUBSPOT_API_KEY`

## Utilizzo

```bash
# Sync delle ultime 24 ore (default)
python gmail_hubspot_sync.py

# Sync degli ultimi 7 giorni
python gmail_hubspot_sync.py --days 7

# Modalità watch (polling ogni 5 min)
python gmail_hubspot_sync.py --watch

# Watch con intervallo personalizzato (ogni 10 min)
python gmail_hubspot_sync.py --watch --interval 600 --days 1
```

## Output esempio

```
══════════════════════════════════════════════════════════════
  SYNC COMPLETATO — 2026-05-26 15:30:00
══════════════════════════════════════════════════════════════
  📧 Email processate : 28
  ✅ Creati           : 5
  🔄 Aggiornati       : 3
  ⏭  Ignorati         : 20
  ❌ Errori           : 0
══════════════════════════════════════════════════════════════

Dettaglio:
  ✅ CREATO       silvia.voltan@pragmatika.it               ID: 785879101647
  ✅ CREATO       helelfiori@hotmail.it                     ID: 785863386317
  🔄 AGGIORNATO   b.mancia@amat.marche.it                   ID: 775146953930
  ⏭  IGNORATO     marco@moonsrl.it                          ID: 769166753984
  ...
```

## Campi HubSpot compilati

| Campo HubSpot | Fonte |
|---------------|-------|
| `email` | Header `From` Gmail |
| `firstname` | Nome display mittente |
| `lastname` | Cognome display mittente |
| `company` | Inferito dal dominio email |
| `hs_lead_status` | Impostato a `NEW` |

## Domini/indirizzi ignorati

Il sync salta automaticamente:
- `no-reply@*`, `noreply@*`, `mailer-daemon@*`
- `*@accounts.google.com`, `*@noreply.github.com`
- Indirizzi già presenti con tutti i campi compilati (IGNORATO)

## Struttura progetto

```
codex/
├── gmail_hubspot_sync.py    # Script principale
├── requirements.txt          # Dipendenze Python
├── .env.example              # Template variabili d'ambiente
├── .env                      # Variabili locali (non committare!)
├── credentials.json          # OAuth2 Gmail (non committare!)
└── gmail_token.json          # Token OAuth2 salvato (non committare!)
```

## Sicurezza

⚠️ **Non committare mai** i file `credentials.json`, `gmail_token.json` e `.env` — sono già in `.gitignore`.
