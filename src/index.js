import 'dotenv/config';
import { createGmailClient } from './gmail.js';
import { createHubSpotClient } from './hubspot.js';
import { runSync } from './sync.js';

const REQUIRED_ENV = [
  'GMAIL_CLIENT_ID',
  'GMAIL_CLIENT_SECRET',
  'GMAIL_REFRESH_TOKEN',
  'HUBSPOT_ACCESS_TOKEN',
];

function validateEnv() {
  const missing = REQUIRED_ENV.filter(key => !process.env[key]);
  if (missing.length > 0) {
    console.error(`Missing required environment variables: ${missing.join(', ')}`);
    process.exit(1);
  }
}

async function main() {
  validateEnv();

  const args = process.argv.slice(2);
  const once = args.includes('--once');
  const dryRun = args.includes('--dry-run') || process.env.DRY_RUN === 'true';
  const intervalMinutes = parseInt(process.env.SYNC_INTERVAL_MINUTES ?? '15', 10);
  const lookbackDays = parseInt(process.env.LOOKBACK_DAYS ?? '1', 10);

  const gmail = createGmailClient({
    clientId: process.env.GMAIL_CLIENT_ID,
    clientSecret: process.env.GMAIL_CLIENT_SECRET,
    refreshToken: process.env.GMAIL_REFRESH_TOKEN,
  });

  const hubspot = createHubSpotClient(process.env.HUBSPOT_ACCESS_TOKEN);

  if (dryRun) console.log('--- DRY RUN MODE: no changes will be written ---');

  const execute = async () => {
    console.log(`\n[${new Date().toISOString()}] Starting Gmail → HubSpot sync...`);
    try {
      const results = await runSync({ gmail, hubspot, lookbackDays, dryRun });
      const summary = results.reduce((acc, r) => {
        acc[r.status] = (acc[r.status] ?? 0) + 1;
        return acc;
      }, {});
      console.log('\nRiepilogo:', summary);
    } catch (err) {
      console.error('Sync failed:', err.message);
    }
  };

  await execute();

  if (!once) {
    const intervalMs = intervalMinutes * 60 * 1000;
    console.log(`\nMonitoring active. Next sync in ${intervalMinutes} minutes...`);
    setInterval(execute, intervalMs);
  }
}

main();
