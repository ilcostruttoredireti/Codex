# Gmail → HubSpot Sync — Guida alla configurazione

## Prerequisiti
- Python 3.11+
- Account Google con Gmail
- Account HubSpot (piano gratuito supportato)

---

## 1. HubSpot — Creare una Private App

1. Accedi a HubSpot → **Impostazioni** → **Integrazioni** → **App private**
2. Clicca **Crea app privata**
3. Assegna un nome (es. "Gmail Sync")
4. Nella scheda **Scopes**, abilita:
   | Scope | Tipo |
   |---|---|
   | `crm.objects.contacts.read` | lettura |
   | `crm.objects.contacts.write` | scrittura |
   | `crm.objects.notes.read` | lettura |
   | `crm.objects.notes.write` | scrittura |
5. Clicca **Crea app** e copia il **token** (inizia con `pat-`)
6. Incollalo in `.env` come `HUBSPOT_API_KEY`

---

## 2. Gmail — Credenziali OAuth

1. Vai su [Google Cloud Console](https://console.cloud.google.com/)
2. Crea un progetto (o seleziona uno esistente)
3. **APIs & Services** → **Libreria** → cerca **Gmail API** → Abilita
4. **APIs & Services** → **Credenziali** → **Crea credenziali** → **ID client OAuth 2.0**
   - Tipo applicazione: **App Desktop**
5. Scarica il file JSON e rinominalo `credentials.json`
6. Posizionalo nella cartella del progetto
7. Aggiungi il tuo indirizzo Gmail come **Utente di test** (Schermata consenso OAuth → Utenti di test)

---

## 3. Installazione e avvio

```bash
# Clona il repository
git clone <repo-url>
cd <repo-dir>

# Installa le dipendenze
pip install -r requirements.txt

# Configura le variabili d'ambiente
cp .env.example .env
# Modifica .env e inserisci HUBSPOT_API_KEY

# Primo avvio (apre il browser per l'autorizzazione Gmail)
python main.py
```

Al primo avvio si aprirà il browser per autorizzare l'accesso a Gmail.  
Il token verrà salvato in `token.json` per i run successivi.

---

## 4. Come funziona

```
Gmail (Inbox)
   │
   ▼  polling ogni 60s via Gmail History API
gmail_client.py
   │  estrae mittente (email, nome)
   ▼
sync.py
   │  cerca contatto in HubSpot per email
   ├─ esiste   → aggiorna campi vuoti + aggiunge nota timeline
   └─ non esiste → crea contatto + aggiunge nota timeline
   │
   ▼
hubspot_client.py  →  HubSpot CRM
```

### Output per ogni email

```
Stato: Creato    | Email: mario@acme.com | HubSpot ID: 12345678
Stato: Aggiornato| Email: giulia@firm.it | HubSpot ID: 87654321
Stato: Ignorato  | Email: noreply@news.it| HubSpot ID: 11223344
Stato: Errore    | Email: x@y.com        | HubSpot ID: N/A
```

### Campi compilati in HubSpot

| Campo HubSpot | Fonte |
|---|---|
| Email | Header `From:` |
| Nome | Header `From:` (parte testo) |
| Cognome | Header `From:` (seconda parola) |
| Azienda | Dominio email (se non provider comune) |
| Fonte | `"Gmail"` (campo `lead_source_detail`) |
| Nota timeline | Oggetto email + data + tag "Inbound Gmail" |

---

## 5. Variabili d'ambiente

| Variabile | Obbligatoria | Default | Descrizione |
|---|---|---|---|
| `HUBSPOT_API_KEY` | ✅ | — | Token Private App HubSpot |
| `GMAIL_CREDENTIALS_FILE` | ❌ | `credentials.json` | Percorso credenziali OAuth |
| `GMAIL_TOKEN_FILE` | ❌ | `token.json` | Percorso token OAuth salvato |
| `POLL_INTERVAL` | ❌ | `60` | Secondi tra un ciclo e l'altro |
| `LOG_LEVEL` | ❌ | `INFO` | Livello di log (DEBUG/INFO/WARNING/ERROR) |

---

## 6. Note sulla sicurezza

- **Non committare mai** `credentials.json`, `token.json` o `.env` — già esclusi dal `.gitignore`
- Il token OAuth ha scope `gmail.readonly`: non può modificare né inviare email
- Il token HubSpot è limitato agli scope selezionati in fase di creazione
