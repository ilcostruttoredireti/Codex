# LaTestata.it – UX sessione scaduta (bozze)

Questo plugin front-end migliora l’esperienza della pagina `Area personale → Pubblica con noi` quando il token/nonce di WordPress è scaduto o l’utente non risulta più autenticato.

Obiettivi:
- Evita messaggi criptici e mantiene l’utente nel flusso.
- Non perde il lavoro in corso (testo già digitato). Avvisa che l’immagine potrebbe dover essere riselezionata.
- Offre un tasto “Accedi di nuovo” con redirect di ritorno alla pagina corrente.
- Dopo il rientro, prova a ri‑cliccare “Salva bozza” (best‑effort, non invasivo).
- Rileva in anticipo la scadenza tramite un ping periodico REST non distruttivo.

Sicurezza:
- Non scrive nel database.
- Non pubblica mai contenuti (bozza soltanto e solo via UI esistente).
- Nessuna azione lato server oltre a un endpoint di “ping” read‑only.

Installazione (ambiente di sviluppo/staging):
1. Copia la cartella in `wp-content/plugins/lt-session-ux/`.
2. Attiva il plugin in WordPress.
3. Vai su `/area-personale/pubblica/` e tieni la pagina aperta.
4. Scade il cookie/nonce (attendi o invalida la sessione).
5. Al click su “Salva bozza” o al ping periodico, appare il modale:
   - “Accedi di nuovo” porta al login e ritorna alla stessa pagina.
   - Al ritorno, se possibile, viene tentato l’auto‑click di “Salva bozza”.

Note tecniche:
- Gli asset vengono caricati solo su URL che contengono `/area-personale/`.
- Il ping usa `GET /wp-json/lt/v1/ping` con `X-WP-Nonce`.
- Intercetta anche gli errori 401/403 a livello `fetch` e `wp.apiFetch`.

Non tocca la produzione finché non viene unita la PR e distribuita dal normale processo di deploy del progetto.

