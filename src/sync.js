'use strict';

require('dotenv').config();

const { fetchInboxMessages } = require('./gmail');
const { isAutomated } = require('./filters');
const { extractContact } = require('./extract');
const {
  findContactByEmail,
  createContact,
  updateContactIfNeeded,
  addEmailActivityNote,
} = require('./hubspot');
const state = require('./state');

const LOOKBACK_HOURS = parseInt(process.env.LOOKBACK_HOURS || '24', 10);
const SYNC_INTERVAL_MS = parseInt(process.env.SYNC_INTERVAL_MINUTES || '15', 10) * 60 * 1000;

/**
 * Runs one full sync cycle:
 *  1. Fetch inbox messages since last sync
 *  2. Filter automated senders
 *  3. Create or update contacts in HubSpot
 *  4. Persist state
 *
 * @returns {Promise<SyncResult[]>}
 */
async function runSync() {
  const currentState = state.load();
  const processedSet = new Set(currentState.processedIds || []);

  // Determine how far back to look
  const lookbackTs = currentState.lastSyncAt
    ? Math.floor(new Date(currentState.lastSyncAt).getTime() / 1000)
    : Math.floor(Date.now() / 1000) - LOOKBACK_HOURS * 3600;

  console.log(`[sync] Fetching messages since ${new Date(lookbackTs * 1000).toISOString()}`);

  const messages = await fetchInboxMessages(lookbackTs);
  console.log(`[sync] ${messages.length} messages fetched`);

  const results = [];

  for (const msg of messages) {
    if (processedSet.has(msg.messageId)) continue;

    // Mark as processed regardless of outcome to avoid re-processing
    processedSet.add(msg.messageId);

    const contact = extractContact(msg);
    if (!contact) {
      results.push({ status: 'Ignorato', email: msg.from, reason: 'parse_failed' });
      continue;
    }

    if (isAutomated(contact.email)) {
      results.push({ status: 'Ignorato', email: contact.email, reason: 'automated_sender' });
      continue;
    }

    try {
      const existing = await findContactByEmail(contact.email);

      if (existing) {
        const { updated, fields } = await updateContactIfNeeded(
          existing.id,
          existing.properties,
          contact,
        );
        await addEmailActivityNote(existing.id, { subject: contact.subject, date: contact.date });
        results.push({
          status: 'Aggiornato',
          email: contact.email,
          hubspotId: existing.id,
          updatedFields: fields,
        });
      } else {
        const { id } = await createContact(contact);
        await addEmailActivityNote(id, { subject: contact.subject, date: contact.date });
        results.push({
          status: 'Creato',
          email: contact.email,
          hubspotId: id,
        });
      }
    } catch (err) {
      console.error(`[sync] Error processing ${contact.email}:`, err.message);
      results.push({ status: 'Errore', email: contact.email, error: err.message });
    }
  }

  // Persist updated state
  state.save({
    lastSyncAt: new Date().toISOString(),
    processedIds: [...processedSet],
  });

  return results;
}

function printResults(results) {
  const counts = { Creato: 0, Aggiornato: 0, Ignorato: 0, Errore: 0 };
  console.log('\n┌─────────────────────────────────────────────────────────────┐');
  console.log('│              Gmail → HubSpot Sync — Risultati              │');
  console.log('├──────────────┬────────────────────────────────┬────────────┤');
  console.log('│   Stato      │   Email Contatto               │  HS ID     │');
  console.log('├──────────────┼────────────────────────────────┼────────────┤');

  for (const r of results) {
    if (!counts[r.status]) counts[r.status] = 0;
    counts[r.status]++;
    const stato = r.status.padEnd(12);
    const email = (r.email || '').substring(0, 30).padEnd(30);
    const id = (r.hubspotId || r.reason || r.error || '').toString().substring(0, 10).padEnd(10);
    console.log(`│ ${stato} │ ${email} │ ${id} │`);
  }

  console.log('└──────────────┴────────────────────────────────┴────────────┘');
  console.log(`\n  Creati: ${counts.Creato}  Aggiornati: ${counts.Aggiornato}  Ignorati: ${counts.Ignorato}  Errori: ${counts.Errore || 0}\n`);
}

async function main() {
  const args = process.argv.slice(2);
  const watchMode = args.includes('--watch');

  try {
    const results = await runSync();
    printResults(results);

    if (watchMode) {
      console.log(`[sync] Prossima sincronizzazione tra ${SYNC_INTERVAL_MS / 60000} minuti…`);
      setTimeout(main, SYNC_INTERVAL_MS);
    }
  } catch (err) {
    console.error('[sync] Fatal error:', err);
    process.exit(1);
  }
}

main();
