# Gmail → HubSpot Contact Sync

Routine automatica Claude Code che monitora la casella Gmail e sincronizza
i mittenti come contatti in HubSpot.

## Funzionamento

Ad ogni esecuzione la routine:

1. **Legge Gmail** — cerca thread in arrivo negli ultimi N giorni
   (`in:inbox -from:me newer_than:7d`)
2. **Estrae i mittenti** — email, nome, dominio aziendale; per le email
   inoltrate da `redazione@latestata.it` estrae anche il mittente originale
3. **Controlla HubSpot** — cerca ogni email come contatto esistente
4. **Crea o aggiorna** il contatto:
   - **CREATO** — il contatto non esiste, viene creato con tutti i campi disponibili
   - **AGGIORNATO** — il contatto esiste ma ha campi vuoti che vengono riempiti
   - **IGNORATO** — il contatto esiste e ha già tutti i dati
5. **Salva lo stato** in `sync_state.json` con gli ID dei thread processati

## Campi HubSpot compilati

| Campo HubSpot | Fonte |
|---|---|
| `email` | Indirizzo mittente |
| `firstname` | Dal display name |
| `lastname` | Dal display name |
| `company` | Dal dominio email (se non personale) |
| `leadsource` | Fisso: `"Gmail"` |

## Regole di esclusione

Vengono ignorati automaticamente:
- Email interne: `pubblica.latestata@gmail.com`, `redazione@latestata.it`, `cristian.mameli.editore@gmail.com`
- Notifiche di sistema: `mailer-daemon`, `noreply`, `notification`, `analytics-noreply`
- Domini di notifica: `facebookmail.com`, `googlemail.com`

## Stato della sincronizzazione

Il file `sync_state.json` traccia:
- `last_run` — timestamp dell'ultima esecuzione
- `processed_thread_ids` — ID dei thread Gmail già processati (deduplicazione)
- `total_created / updated / skipped` — contatori cumulativi

## Utilizzo standalone (Python)

```bash
pip install -r requirements.txt
cp .env.example .env
# Configura HUBSPOT_TOKEN e GOOGLE_CREDENTIALS_FILE in .env

# Dry run (solo parsing, nessuna scrittura su HubSpot)
python gmail_hubspot_sync.py --dry-run

# Sincronizzazione reale (ultimi 2 giorni)
python gmail_hubspot_sync.py --days 2

# Con token esplicito
python gmail_hubspot_sync.py --hubspot-token pat-eu1-...
```

## Utilizzo come routine Claude Code

Questa routine viene eseguita automaticamente come sessione Claude Code
programmata. I MCP di Gmail e HubSpot già configurati forniscono l'accesso
alle API senza necessità di credenziali aggiuntive.

Il file `sync_state.json` viene aggiornato ad ogni esecuzione e committato
nel repository per tracciare i progressi tra le sessioni.
