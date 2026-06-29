require('dotenv').config();

const { buildGmailClient, fetchRecentThreads, companyFromDomain } = require('./gmail');
const { buildHubSpotClient, findContactByEmail, createContact, updateContact } = require('./hubspot');

const LOOKBACK_HOURS = parseInt(process.env.SYNC_LOOKBACK_HOURS || '24', 10);
const POLL_INTERVAL_MS = parseInt(process.env.POLL_INTERVAL_MINUTES || '15', 10) * 60 * 1000;
const WATCH_MODE = process.argv.includes('--watch');

async function runSync() {
  const gmail = buildGmailClient();
  const hs = buildHubSpotClient();

  console.log(`[sync] Starting Gmail → HubSpot sync (lookback: ${LOOKBACK_HOURS}h)`);

  const senders = await fetchRecentThreads(gmail, LOOKBACK_HOURS);

  // Deduplicate by email (lowercase)
  const seen = new Map();
  for (const s of senders) {
    const key = s.email.toLowerCase();
    if (!seen.has(key)) seen.set(key, s);
  }

  const unique = Array.from(seen.values());
  console.log(`[sync] ${senders.length} senders found → ${unique.length} unique emails`);

  const results = [];

  for (const sender of unique) {
    try {
      const existing = await findContactByEmail(hs, sender.email);
      const company = sender.company || companyFromDomain(sender.email);

      if (!existing) {
        const props = {
          email: sender.email,
          hs_lead_source: 'Gmail',
          ...(sender.firstName && { firstname: sender.firstName }),
          ...(sender.lastName && { lastname: sender.lastName }),
          ...(company && { company }),
        };
        const created = await createContact(hs, props);
        results.push({ status: 'Creato', email: sender.email, id: created.id });
        console.log(`[sync] CREATO   ${sender.email}  →  ID ${created.id}`);
      } else {
        const incomingProps = {
          firstname: sender.firstName,
          lastname: sender.lastName,
          company,
        };
        const updated = await updateContact(hs, existing.id, existing, incomingProps);
        if (updated) {
          results.push({ status: 'Aggiornato', email: sender.email, id: existing.id });
          console.log(`[sync] AGGIORNATO  ${sender.email}  →  ID ${existing.id}`);
        } else {
          results.push({ status: 'Ignorato', email: sender.email, id: existing.id });
          console.log(`[sync] IGNORATO    ${sender.email}  →  ID ${existing.id} (nessuna modifica)`);
        }
      }
    } catch (err) {
      console.error(`[sync] ERRORE per ${sender.email}: ${err.message}`);
      results.push({ status: 'Errore', email: sender.email, id: null, error: err.message });
    }
  }

  const created = results.filter(r => r.status === 'Creato').length;
  const updated = results.filter(r => r.status === 'Aggiornato').length;
  const ignored = results.filter(r => r.status === 'Ignorato').length;

  console.log(`\n[sync] ✓ Completato — Creati: ${created}  Aggiornati: ${updated}  Ignorati: ${ignored}`);
  return results;
}

async function main() {
  await runSync();

  if (WATCH_MODE) {
    console.log(`\n[sync] Watch mode attivo — prossimo ciclo tra ${POLL_INTERVAL_MS / 60000} minuti`);
    setInterval(runSync, POLL_INTERVAL_MS);
  }
}

main().catch(err => {
  console.error('[sync] Errore fatale:', err);
  process.exit(1);
});
