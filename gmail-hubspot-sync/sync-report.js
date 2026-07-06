#!/usr/bin/env node
/**
 * Standalone report script: prints the last sync results stored in the
 * state file. Useful for cron-job log visibility.
 *
 * Usage:  node sync-report.js
 */

'use strict';

const fs = require('fs');
const path = require('path');

const STATE_FILE = process.env.STATE_FILE ?? path.join(__dirname, '.sync-state.json');

try {
  const state = JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));
  console.log(`Processed thread IDs in state: ${state.processedThreadIds?.length ?? 0}`);
  if (state.lastRun) {
    console.log(`Last run: ${state.lastRun}`);
    console.log(`Last run results:`);
    for (const r of state.lastRunResults ?? []) {
      console.log(`  [${r.status}] ${r.email} → ID ${r.contactId}`);
    }
  }
} catch {
  console.log('No state file found. Run the sync first.');
}
