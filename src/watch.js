'use strict';

/**
 * Continuous watch mode: runs a sync every POLL_INTERVAL_MINUTES.
 * Usage: node src/watch.js
 * Or with custom interval: POLL_INTERVAL_MINUTES=10 node src/watch.js
 */

require('dotenv').config();

const INTERVAL_MS = (parseInt(process.env.POLL_INTERVAL_MINUTES || '15', 10)) * 60 * 1000;

// Require after dotenv so env vars are available
const { syncInbox } = require('./sync-lib');

async function watchLoop() {
  console.log(`[watch] Gmail → HubSpot watcher started (polling every ${INTERVAL_MS / 60000} min)`);

  const runOnce = async () => {
    try {
      await syncInbox();
    } catch (err) {
      console.error('[watch] Sync error:', err.message);
    }
  };

  // Run immediately on start
  await runOnce();

  setInterval(runOnce, INTERVAL_MS);
}

watchLoop();
