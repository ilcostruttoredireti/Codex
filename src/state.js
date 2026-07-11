'use strict';

const fs = require('fs');
const path = require('path');

const STATE_FILE = process.env.STATE_FILE || path.join(__dirname, '..', 'state.json');

const DEFAULT_STATE = {
  lastSyncAt: null,        // ISO timestamp of last successful sync
  processedIds: [],        // Gmail message IDs already processed
};

function load() {
  try {
    const raw = fs.readFileSync(STATE_FILE, 'utf8');
    return { ...DEFAULT_STATE, ...JSON.parse(raw) };
  } catch {
    return { ...DEFAULT_STATE };
  }
}

function save(state) {
  // Keep only the last 5000 processed IDs to prevent unbounded growth
  const trimmed = {
    ...state,
    processedIds: (state.processedIds || []).slice(-5000),
  };
  fs.writeFileSync(STATE_FILE, JSON.stringify(trimmed, null, 2), 'utf8');
}

module.exports = { load, save };
