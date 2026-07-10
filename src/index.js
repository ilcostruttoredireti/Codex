import cron from 'node-cron';
import { config } from './config.js';
import { runSync } from './sync.js';

const runOnce = process.argv.includes('--once');

async function main() {
  if (runOnce) {
    await runSync({ config });
    process.exit(0);
  }

  const intervalMinutes = config.sync.intervalMinutes;
  const cronExpr = `*/${intervalMinutes} * * * *`;

  console.log(`[main] Gmail→HubSpot sync starting, interval: every ${intervalMinutes} min`);

  // Run immediately on startup
  await runSync({ config });

  cron.schedule(cronExpr, async () => {
    try {
      await runSync({ config });
    } catch (err) {
      console.error('[main] Sync failed:', err.message);
    }
  });
}

main().catch(err => {
  console.error('[main] Fatal:', err.message);
  process.exit(1);
});
