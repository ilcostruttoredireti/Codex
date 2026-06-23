import 'dotenv/config';
import cron from 'node-cron';
import { createGmailClient, fetchNewSenders } from './gmail.js';
import { createHubSpotClient, syncContact } from './hubspot.js';
import { parseFromHeader, shouldSkipEmail, buildContactData } from './utils.js';

const GMAIL_CREDS = JSON.parse(process.env.GMAIL_CREDENTIALS || '{}');
const GMAIL_TOKEN = JSON.parse(process.env.GMAIL_TOKEN || '{}');
const HS_TOKEN = process.env.HUBSPOT_ACCESS_TOKEN;

async function runSync() {
  console.log(`[${new Date().toISOString()}] Sync Gmail -> HubSpot`);
  const gmail = createGmailClient(GMAIL_CREDS, GMAIL_TOKEN);
  const hs = createHubSpotClient(HS_TOKEN);
  const raw = await fetchNewSenders(gmail);
  const results = [];
  const seen = new Set();
  for (const { from } of raw) {
    const p = parseFromHeader(from);
    if (!p || seen.has(p.email)) continue;
    seen.add(p.email);
    if (shouldSkipEmail(p.email)) { results.push({ email: p.email, stato: 'Ignorato', id: '-' }); continue; }
    const d = buildContactData(p);
    try {
      const r = await syncContact(hs, d);
      results.push({ email: d.email, stato: r.status, id: r.id });
    } catch (e) {
      results.push({ email: d.email, stato: 'Errore', id: '-' });
    }
  }
  console.table(results);
  return results;
}

if (process.argv.includes('--once')) {
  runSync().catch(e => { console.error(e); process.exit(1); });
} else {
  runSync().catch(console.error);
  cron.schedule('*/15 * * * *', () => runSync().catch(console.error));
}
