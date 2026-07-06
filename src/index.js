'use strict';

require('dotenv').config();
const cron = require('node-cron');
const { runSync } = require('./sync');

const LOOKBACK_MINUTES = parseInt(process.env.LOOKBACK_MINUTES || '60', 10);
const CRON_SCHEDULE = process.env.CRON_SCHEDULE || '*/15 * * * *';
const DRY_RUN = process.env.DRY_RUN === 'true' || process.argv.includes('--dry-run');
const RUN_ONCE = process.argv.includes('--once');

if (DRY_RUN) {
  console.log('[index] DRY-RUN mode — no changes will be written to HubSpot');
}

async function main() {
  try {
    await runSync({ lookbackMinutes: LOOKBACK_MINUTES, dryRun: DRY_RUN });
  } catch (err) {
    console.error('[index] Sync failed:', err.message);
    process.exitCode = 1;
  }
}

if (RUN_ONCE || DRY_RUN) {
  // Single execution: used by `npm run sync` or `npm test`
  main();
} else {
  // Continuous monitoring: run immediately then on schedule
  console.log(`[index] Starting continuous monitoring (schedule: "${CRON_SCHEDULE}")`);
  main();
  cron.schedule(CRON_SCHEDULE, () => {
    console.log(`\n[index] Cron tick — ${new Date().toISOString()}`);
    main();
  });
}
