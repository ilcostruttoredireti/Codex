# Gmail → HubSpot Contact Sync

Routine schedulata che monitora la casella Gmail in entrata, estrae i mittenti e li sincronizza come contatti in HubSpot.

## Come funziona

Per ogni email in arrivo nelle ultime 24 ore:

1. Estrae il mittente reale (gestendo email inoltrate con prefisso `Fw:`)
2. Cerca il contatto in HubSpot per email (chiave univoca)
3. **Se esiste** → aggiorna i campi mancanti (nome, cognome, azienda)
4. **Se non esiste** → crea un nuovo contatto con:
   - Email
   - Nome / Cognome (se ricavabile dal corpo dell'email)
   - Azienda (dal dominio email o dalla firma)
   - Fonte contatto: Gmail

## Campi HubSpot compilati

| Campo HubSpot | Fonte |
|---|---|
| `email` | Indirizzo mittente |
| `firstname` | Nome dalla firma / display name |
| `lastname` | Cognome dalla firma |
| `company` | Dal dominio o dalla firma email |
| `hs_lead_status` | `NEW` per nuovi contatti |

## Logica anti-duplicati

- La ricerca viene eseguita per indirizzo email esatto prima di creare
- Se il contatto esiste già con tutti i campi compilati → stato **Ignorato**
- Se esistono campi vuoti → stato **Aggiornato**

## Output per email processata

```
Stato: Creato | Aggiornato | Ignorato
Email contatto: mittente@dominio.com
ID contatto HubSpot: 123456789
```

## Note operative

- Filtra le email proprie (`redazione@latestata.it`) per non auto-importarsi
- Gestisce email inoltrate (pattern `Fw:`) estraendo il mittente originale
- La routine viene eseguita da Claude Code tramite MCP Gmail + HubSpot
