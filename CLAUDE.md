# Gmail → HubSpot Contact Sync

Routine schedulata che monitora le email in arrivo su Gmail e sincronizza i contatti in HubSpot.

## Cosa fa

1. Scansiona le email in arrivo degli ultimi 7 giorni su Gmail
2. Estrae i mittenti unici (esclude notifiche automatiche e l'account stesso)
3. Per ogni mittente verifica in HubSpot:
   - **Se esiste** → aggiunge una nota attività "Inbound Gmail" con data e dominio
   - **Se non esiste** → crea un nuovo contatto con email, nome, azienda (dal dominio), fonte: Gmail
4. Salva il log in `sync_log.json`

## Output per ogni email processata

| Campo | Valore |
|-------|--------|
| Stato | Creato / Aggiornato / Ignorato |
| Email contatto | mittente@dominio.it |
| ID HubSpot | numerico |

## Mittenti ignorati

- L'account Gmail stesso
- Notifiche automatiche (facebookmail.com, ecc.)
- Indirizzi no-reply

## Log

Ogni run produce un `sync_log.json` con il dettaglio completo dei contatti processati.
