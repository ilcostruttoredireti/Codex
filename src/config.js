import 'dotenv/config';

function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`Missing required env var: ${name}`);
  return value;
}

export const config = {
  gmail: {
    clientId: required('GMAIL_CLIENT_ID'),
    clientSecret: required('GMAIL_CLIENT_SECRET'),
    refreshToken: required('GMAIL_REFRESH_TOKEN'),
  },
  hubspot: {
    accessToken: required('HUBSPOT_ACCESS_TOKEN'),
  },
  sync: {
    intervalMinutes: parseInt(process.env.SYNC_INTERVAL_MINUTES || '15', 10),
    initialLookbackDays: parseInt(process.env.INITIAL_LOOKBACK_DAYS || '7', 10),
    stateFilePath: process.env.STATE_FILE_PATH || './data/sync-state.json',
    skipPatterns: (process.env.SKIP_PATTERNS || 'noreply,no-reply,notification,notify-,pinbot,ads-,bounce,donotreply,daemon,mailer-daemon,postmaster')
      .split(',')
      .map(p => p.trim().toLowerCase()),
  },
};
