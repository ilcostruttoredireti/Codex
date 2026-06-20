# Gmail → HubSpot Contact Sync

Monitora le email in arrivo su Gmail ed esegue la sincronizzazione dei mittenti in HubSpot come contatti.

## Logica

| Caso | Azione |
|------|--------|
| Contatto non esiste | Crea nuovo contatto con tutti i campi disponibili |
| Contatto esiste, campi mancanti | Aggiorna solo i campi vuoti |
| Contatto esiste, completo | Ignorato (nessuna modifica) |
| Mittente automatico / propria email | Saltato |

## Campi compilati in HubSpot

- `email` — email del mittente (chiave unica)
- `firstname` / `lastname` — dal display name
- `company` — dal dominio email se non generico (gmail, yahoo, ecc.)
- `lead_source` — sempre impostato su `Gmail`

## Struttura

```
routines/gmail_hubspot_sync/
├── sync.py      # logica principale (framework-agnostic)
└── README.md
```

## Come eseguire

Il modulo `sync.py` espone `sync_contacts()` che accetta funzioni injectable per la comunicazione con HubSpot. Integrare nelle proprie funzioni MCP/Claude Code passando i callable appropriati.

## Filtri anti-spam

- Domini bloccati: `facebookmail.com`, `notifications.google.com`, ecc.
- Prefissi bloccati: `noreply`, `no-reply`, `donotreply`, `mailer-daemon`, `postmaster`
- Email proprie dell'account: sempre saltate
