import { extractContact } from './emailParser.js';
import { GmailPoller } from './gmailPoller.js';
import { HubSpotSync } from './hubspotSync.js';
import { SyncState } from './state.js';

/**
 * Runs a single sync pass:
 * 1. Fetch inbox messages since last run
 * 2. Extract non-automated senders
 * 3. Deduplicate by email (within this batch)
 * 4. Upsert each contact in HubSpot
 * 5. Persist state for the next run
 */
export async function runSync({ config, logger = console }) {
  const state = new SyncState(config.sync.stateFilePath);
  const poller = new GmailPoller(config.gmail);
  const hubspot = new HubSpotSync(config.hubspot.accessToken);

  logger.log(`[sync] Starting — last run: ${state.lastProcessedDate ?? 'never'}`);

  const messages = await poller.fetchInboxMessages(state.lastProcessedDate);
  logger.log(`[sync] ${messages.length} messages fetched`);

  const results = [];
  const seenEmails = new Set();

  for (const message of messages) {
    if (state.isProcessed(message.id)) continue;

    const contact = extractContact(message, config.sync.skipPatterns);
    if (!contact) {
      state.markProcessed(message.id);
      continue;
    }

    // Skip duplicates within this batch (same sender, multiple emails)
    if (seenEmails.has(contact.email)) {
      state.markProcessed(message.id);
      continue;
    }
    seenEmails.add(contact.email);

    try {
      const result = await hubspot.syncContact(contact);

      if (result.status !== 'ignored') {
        await hubspot.logEmailActivity(result.hubspotId, contact);
      }

      results.push(result);
      logger.log(`[sync] ${result.status.toUpperCase()} | ${result.email} | HubSpot ID: ${result.hubspotId}`);
    } catch (err) {
      logger.error(`[sync] ERROR processing ${contact.email}: ${err.message}`);
      results.push({ status: 'error', email: contact.email, error: err.message });
    }

    state.markProcessed(message.id);
  }

  // Update the watermark to now so the next run only looks at newer messages
  state.lastProcessedDate = new Date().toISOString();
  state.save();

  const summary = {
    processed: messages.length,
    created: results.filter(r => r.status === 'created').length,
    updated: results.filter(r => r.status === 'updated').length,
    ignored: results.filter(r => r.status === 'ignored').length,
    errors: results.filter(r => r.status === 'error').length,
    contacts: results,
  };

  logger.log(`[sync] Done — created: ${summary.created}, updated: ${summary.updated}, ignored: ${summary.ignored}, errors: ${summary.errors}`);
  return summary;
}
