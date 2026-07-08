# Gmail → HubSpot Contact Sync

Automated routine that monitors incoming Gmail emails and syncs sender contacts into HubSpot CRM, avoiding duplicates and updating existing records.

## Logic

1. Fetch the last 20 unread inbox emails (excluding noreply/no-reply senders)
2. For each sender: check HubSpot by email (unique key)
   - **Not found** → create contact with email, name, company (from domain), source = EMAIL_MARKETING
   - **Found** → update missing fields only
   - **noreply/no-reply** → skip entirely
3. Attach a timeline note: `Fonte: Inbound Gmail | Tag: Inbound Gmail`
4. Emit a status row per email: `Creato / Aggiornato / Ignorato / Saltato`

## Skipped sender patterns

- Address prefix is `noreply` or `no-reply`

## HubSpot fields populated

| HubSpot field | Source |
|---|---|
| `email` | Sender address |
| `firstname` | Name part of sender, or domain brand |
| `lastname` | Surname extracted from name, or domain label |
| `company` | Brand derived from email domain |
| `hs_analytics_source` | `EMAIL_MARKETING` |

A **Note** is always attached to new contacts:
```
Fonte: Inbound Gmail
Tag: Inbound Gmail
Email ricevuta: <subject>
Data: <date>
```

## Run log — 2026-07-08

| # | Sender | Stato | HubSpot ID |
|---|--------|-------|------------|
| 1 | noreply@produzionidalbasso.com | Saltato (noreply) | — |
| 2 | info@mailer.whitepress.com | **Creato** | 817129175280 |
| 3 | marketing@trackdesk.com | Ignorato (esistente) | 817094979773 |
| 4 | support@fatjoe.com | Ignorato (esistente) | 811676857565 |
| 5 | dailybriefing@thomsonreuters.com | Ignorato (esistente) | 407641562303 |
| 6 | info@marshyellow.net | Ignorato (esistente) | 811055655117 |
| 7 | info@linkeasy.it | Ignorato (esistente) | 817082919155 |
| 8 | info@endercomunicazione.it | Ignorato (esistente) | 784155419864 |
| 9 | mailer@semalt.org | Ignorato (esistente) | 397340254432 |
| 10 | chelsea.c@ifttt.com | Ignorato (esistente) | 811752190187 |
| 11 | solo@leasedadspace.com | Ignorato (esistente) | 816976409849 |
| 12 | CloudPlatform-noreply@google.com | Saltato (noreply) | — |
| 13 | riccardo@martes-ai.com | Ignorato (esistente) | 408286770372 |
| 14 | googlecloud@google.com | Ignorato (esistente) | 603722529999 |
| 15 | info@scuolaecommerce.com | Ignorato (esistente) | 811588908253 |
| 16 | commerciale@raffaprivatejet.com | Ignorato (esistente) | 810872065220 |
| 17 | support@yoast.com | Ignorato (esistente) | 814198441166 |
| 18 | ufficiostampatrousse@gmail.com | Ignorato (esistente) | 450685585630 |
| 19 | no-reply@serpapi.com | Saltato (no-reply) | — |
| 20 | premium@academia-mail.com | Ignorato (esistente) | 812356222142 |

**Totale:** 20 email elaborate — 1 Creato · 0 Aggiornati · 16 Ignorati · 3 Saltati
