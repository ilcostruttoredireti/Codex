'use strict';

const fs = require('fs');
const path = require('path');
const { listInboxMessages, getMessageSender, domainToCompany, isAutomatedSender } = require('./gmail');
const { findContactByEmail, createContact, updateContact } = require('./hubspot');

const PROCESSED_FILE = path.resolve(process.env.PROCESSED_IDS_FILE || '.processed_ids.json');
const LOOKBACK_HOURS = parseInt(process.env.SYNC_LOOKBACK_HOURS || '24', 10);

// ── Persist processed message IDs to avoid double-processing ──────────────────

function loadProcessed() {
  try {
    return new Set(JSON.parse(fs.readFileSync(PROCESSED_FILE, 'utf8')));
  } catch {
    return new Set();
  }
}

function saveProcessed(set) {
  fs.writeFileSync(PROCESSED_FILE, JSON.stringify([...set]), 'utf8');
}

// ── Main sync logic ───────────────────────────────────────────────────────────

async function syncInbox() {
  console.log(`[sync] Starting Gmail → HubSpot sync (lookback: ${LOOKBACK_HOURS}h)`);

  const processed = loadProcessed();
  const results = [];
  const seen = new Map(); // email → first messageId (dedup same sender across multiple emails)

  // Fetch inbox messages
  let pageToken = null;
  const cutoff = Date.now() - LOOKBACK_HOURS * 60 * 60 * 1000;

  do {
    const page = await listInboxMessages(50, pageToken);
    const messages = page.messages || [];
    pageToken = page.nextPageToken || null;

    for (const msg of messages) {
      if (processed.has(msg.id)) continue;

      let sender;
      try {
        sender = await getMessageSender(msg.id);
      } catch (err) {
        console.warn(`[sync] Could not fetch message ${msg.id}: ${err.message}`);
        continue;
      }

      // Stop if message is older than lookback window
      const msgDate = new Date(sender.date).getTime();
      if (!isNaN(msgDate) && msgDate < cutoff) {
        pageToken = null; // stop pagination
        break;
      }

      // Skip automated / no-reply senders
      if (!sender.email || isAutomatedSender(sender.email)) {
        processed.add(msg.id);
        results.push({ status: 'Ignorato', reason: 'automated sender', email: sender.email, messageId: msg.id });
        continue;
      }

      // Dedup: only process each sender email once per run
      if (seen.has(sender.email)) {
        processed.add(msg.id);
        results.push({ status: 'Ignorato', reason: 'dedup (already seen this run)', email: sender.email, messageId: msg.id });
        continue;
      }
      seen.set(sender.email, msg.id);

      // Derive company from domain if no explicit company name
      const company = domainToCompany(sender.email);

      // Check HubSpot
      let existing;
      try {
        existing = await findContactByEmail(sender.email);
      } catch (err) {
        console.error(`[sync] HubSpot search error for ${sender.email}: ${err.message}`);
        continue;
      }

      if (existing) {
        // Update only missing fields
        const updates = {};
        const p = existing.properties;

        if (!p.firstname && sender.firstName) updates.firstname = sender.firstName;
        if (!p.lastname && sender.lastName) updates.lastname = sender.lastName;
        if (!p.company && company) updates.company = company;

        if (Object.keys(updates).length > 0) {
          try {
            await updateContact(existing.id, updates);
            results.push({ status: 'Aggiornato', email: sender.email, hubspotId: existing.id, updates });
          } catch (err) {
            console.error(`[sync] Update failed for ${sender.email}: ${err.message}`);
          }
        } else {
          results.push({ status: 'Ignorato', reason: 'already complete', email: sender.email, hubspotId: existing.id });
        }
      } else {
        // Create new contact
        const props = {
          email: sender.email,
          firstname: sender.firstName || sender.email.split('@')[0],
          lastname: sender.lastName || '',
          company,
          hs_lead_status: 'NEW',
        };

        try {
          const created = await createContact(props);
          results.push({ status: 'Creato', email: sender.email, hubspotId: created.id });
        } catch (err) {
          console.error(`[sync] Create failed for ${sender.email}: ${err.message}`);
        }
      }

      processed.add(msg.id);
    }
  } while (pageToken);

  saveProcessed(processed);

  // ── Print report ─────────────────────────────────────────────────────────────
  console.log('\n─────────────────────────────────────────────');
  console.log('  Gmail → HubSpot Sync Report');
  console.log('─────────────────────────────────────────────');

  const grouped = { Creato: [], Aggiornato: [], Ignorato: [] };
  for (const r of results) grouped[r.status]?.push(r);

  console.log(`  Creati:     ${grouped.Creato.length}`);
  console.log(`  Aggiornati: ${grouped.Aggiornato.length}`);
  console.log(`  Ignorati:   ${grouped.Ignorato.length}`);
  console.log('─────────────────────────────────────────────');

  for (const r of results) {
    const id = r.hubspotId ? ` [HS:${r.hubspotId}]` : '';
    const extra = r.reason ? ` (${r.reason})` : '';
    console.log(`  [${r.status.padEnd(10)}] ${r.email || r.messageId}${id}${extra}`);
  }

  console.log('─────────────────────────────────────────────\n');
  return results;
}

module.exports = { syncInbox };
