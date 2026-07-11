'use strict';

require('dotenv').config();
const { syncInbox } = require('./sync-lib');

syncInbox().catch((err) => {
  console.error('[sync] Fatal error:', err);
  process.exit(1);
});
