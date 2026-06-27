import 'dotenv/config';
import cron from 'node-cron';
import { createGmailClient } from './gmail.js';
import { createHubSpotClient } from './hubspot.js';
import { runSync } from './sync.js';

const {
  GMAIL_CLIENT_ID,
  GMAIL_CLIENT_SECRET,
  GMAIL_REFRESH_TOKEN,
  GMAIL_USER_EMAIL,
  HUBSPOT_ACCESS_TOKEN,
  SYNC_INTERVAL_MINUTES = '30',
  LOOKBACK_HOURS = '24',
  DRY_RUN = 'false',
} = process.env;

function validateConfig() {
  const required = {
    GMAIL_CLIENT_ID,
    GMAIL_CLIENT_SECRET,
    GMAIL_REFRESH_TOKEN,
    GMAIL_USER_EMAIL,
    HUBSPOT_ACCESS_TOKEN,
  };
  const missing = Object.entries(required).filter(([, v]) => !v).map(([k]) => k);
  if (missing.length) {
    console.error(`Variabili d'ambiente mancanti: ${missing.join(', ')}`);
    console.error('Copia .env.example in .env e compila i valori richiesti.');
    process.exit(1);
  }
}

function buildClients() {
  const gmail = createGmailClient({
    clientId: GMAIL_CLIENT_ID,
    clientSecret: GMAIL_CLIENT_SECRET,
    refreshToken: GMAIL_REFRESH_TOKEN,
    userEmail: GMAIL_USER_EMAIL,
  });

  const hubspot = createHubSpotClient(HUBSPOT_ACCESS_TOKEN);

  return { gmail, hubspot };
}

async function syncOnce() {
  const { gmail, hubspot } = buildClients();
  const dryRun = DRY_RUN === 'true';
  const lookbackHours = Number(LOOKBACK_HOURS);

  console.log(`\n${'='.repeat(60)}`);
  console.log(`Gmail → HubSpot Sync  [${new Date().toISOString()}]`);
  console.log(`Modalità: ${dryRun ? 'DRY RUN' : 'LIVE'}  |  Finestra: ${lookbackHours}h`);
  console.log('='.repeat(60));

  const results = await runSync({ gmail, hubspot, lookbackHours, dryRun });

  const summary = results.reduce((acc, r) => {
    acc[r.status] = (acc[r.status] ?? 0) + 1;
    return acc;
  }, {});

  console.log('\n--- Riepilogo ---');
  for (const [status, count] of Object.entries(summary)) {
    console.log(`  ${status}: ${count}`);
  }
  console.log(`  Totale processati: ${results.length}`);
  console.log('='.repeat(60));

  return results;
}

// Run-once se lanciato con --once o come scheduled task
if (process.argv.includes('--once')) {
  validateConfig();
  syncOnce().catch(err => {
    console.error('Sync fallita:', err.message);
    process.exit(1);
  });
} else {
  // Modalità continua con cron
  validateConfig();
  const intervalMinutes = Number(SYNC_INTERVAL_MINUTES);
  console.log(`Avvio sync continua ogni ${intervalMinutes} minuti...`);

  // Prima run immediata
  syncOnce().catch(console.error);

  // Run periodica
  cron.schedule(`*/${intervalMinutes} * * * *`, () => {
    syncOnce().catch(console.error);
  });
}
