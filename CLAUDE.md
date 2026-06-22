# Gmail → HubSpot Contact Sync

Sessione Claude Code schedulata che monitora Gmail e sincronizza i mittenti in HubSpot.

## Cosa fa

Ad ogni esecuzione:
1. Legge le email in arrivo nelle ultime 24 ore (INBOX, escluse le proprie)
2. Estrae email, nome e dominio aziendale da ogni mittente unico
3. Verifica in HubSpot se il contatto esiste già
   - **Esiste** → aggiorna i campi vuoti + aggiunge nota di timeline
   - **Non esiste** → crea il contatto con fonte "Gmail" + nota di timeline
4. Salta automaticamente: mittenti propri, notifiche automatiche (`noreply@`, `pageupdates@`, ecc.), domini Gmail senza nome

## Campi HubSpot compilati

| Campo HubSpot | Valore |
|---|---|
| `email` | indirizzo del mittente |
| `firstname` | nome (dal campo From) |
| `lastname` | cognome (dal campo From) |
| `company` | derivato dal dominio email |
| `hs_analytics_source` | `EMAIL_MARKETING` |
| `hs_analytics_source_data_1` | `Gmail` |
| Nota di timeline | data, mittente, tag "Inbound Gmail" |

## Utilizzo locale (alternativo alla sessione schedulata)

```bash
pip install -r requirements.txt
export HUBSPOT_ACCESS_TOKEN="pat-eu1-..."
python gmail_hubspot_sync.py --hours 24
```

Richiede Application Default Credentials Google configurate:
```bash
gcloud auth application-default login
```

## Output per ogni email processata

```
[Creato]    info@newagency.com   →  ID 12345678  ()
[Aggiornato] mario@example.it   →  ID 87654321  (campi aggiornati: ['company'])
[Ignorato]  noreply@system.com  →  ID None       (skip automatico)
```
