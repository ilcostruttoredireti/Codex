# Gmail → HubSpot Contact Sync

Monitora la casella Gmail in arrivo ed esporta automaticamente i mittenti come contatti HubSpot, evitando duplicati e aggiornando i campi mancanti.

## Come funziona

```
Gmail inbox
    │
    ▼
Estrae mittenti unici          (extract_sender + deduplicate)
    │
    ▼
Cerca in HubSpot per email     (search_crm_objects, operatore IN)
    │
    ├─► Non trovato → Crea nuovo contatto
    ├─► Trovato + campi vuoti → Aggiorna
    └─► Trovato + completo   → Ignora (nessuna azione)
```

## Logica di estrazione

- Le email inoltrate da `redazione@latestata.it` vengono analizzate per ricavare il mittente originale dallo snippet (`Da "Nome" email@...`).
- Indirizzi di sistema (mailer-daemon, Facebook, indirizzi interni) vengono scartati automaticamente.
- L'email viene usata come chiave univoca per prevenire duplicati.

## Campi HubSpot compilati

| Campo HubSpot      | Fonte                                      |
|--------------------|--------------------------------------------|
| `email`            | mittente Gmail                             |
| `firstname`        | nome estratto dall'intestazione            |
| `lastname`         | cognome estratto dall'intestazione         |
| `company`          | dominio email (best-effort)                |
| `hs_lead_source`   | `OTHER` (valore più vicino a "Gmail")      |
| `lead_source_detail` | `"Inbound Gmail"`                        |

## Output per ogni email processata

| Stato      | Significato                                  |
|------------|----------------------------------------------|
| Creato     | Nuovo contatto aggiunto in HubSpot           |
| Aggiornato | Contatto esistente: campi vuoti compilati    |
| Ignorato   | Contatto completo, nessuna modifica          |

## Esecuzione

Lo script è pensato per girare come routine pianificata tramite **Claude Code on the web** con accesso ai server MCP Gmail e HubSpot.

```bash
python gmail_to_hubspot_sync.py   # test locale / documentazione
```
