/**
 * Live sync runner — uses MCP tools (Gmail + HubSpot) instead of direct API calls.
 * This is the version executed by Claude Code in scheduled sessions.
 * The standalone sync.js uses googleapis + @hubspot/api-client for unattended cron runs.
 */
'use strict';

// Contacts extracted from the current Gmail inbox scan (last 7 days)
// Automated senders have been filtered out by the isAutomated() logic
const INBOX_CONTACTS = [
  {
    email: 'staff@spiegamelofacile.com',
    firstName: 'Staff',
    lastName: '',
    company: 'Spiegamelofacile',
    subject: 'La lista email vale più di 10.000 follower',
    date: '2026-07-11T06:06:07Z',
  },
  {
    email: 'general@mediacloud.press',
    firstName: 'Media',
    lastName: 'Cloud',
    company: 'Media Cloud Press',
    subject: 'Are you sure, Cristian? One last reminder about Media Cloud Basic plan checkout...',
    date: '2026-07-10T20:05:02Z',
  },
  {
    email: 'commerciale@raffaprivatejet.com',
    firstName: 'Commerciale',
    lastName: '',
    company: 'Raffa Private Jet',
    subject: '🏖️ Una settimana a Parigi (quasi gratis)',
    date: '2026-07-10T18:28:05Z',
  },
  {
    email: 'info@seozoom.it',
    firstName: 'Info',
    lastName: '',
    company: 'SEO Zoom',
    subject: 'Cristian hai perso gli aggiornamenti più utili su SEO e AI?',
    date: '2026-07-10T16:20:21Z',
  },
  {
    email: 'redazione@startupitalia.eu',
    firstName: 'Redazione',
    lastName: '',
    company: 'Startup Italia',
    subject: 'La svolta di Satispay, il ritorno di Grom e i 134 milioni di Quaise.',
    date: '2026-07-10T16:17:06Z',
  },
  {
    email: 'info@coinranking.com',
    firstName: 'Info',
    lastName: '',
    company: 'Coinranking',
    subject: 'While Everyone Watched Bitcoin... 👀',
    date: '2026-07-10T13:33:45Z',
  },
];

// Senders skipped because they are automated/system accounts
const SKIPPED = [
  { email: 'no-reply@youtube.com', reason: 'automated_sender' },
  { email: 'messaging-digest-noreply@linkedin.com', reason: 'automated_sender' },
  { email: 'confirm@mailchimp.com', reason: 'automated_sender' },
  { email: 'notifications@vercel.com', reason: 'automated_sender' },
  { email: 'noreply@email.openai.com', reason: 'automated_sender' },
  { email: 'noreply@discord.com', reason: 'automated_sender' },
  { email: 'noreply-accounts@google.com', reason: 'automated_sender' },
  { email: 'notify-noreply@google.com', reason: 'automated_sender' },
  { email: 'noreply@mail.bybit.com', reason: 'automated_sender' },
  { email: 'pinbot@info.pinterest.com', reason: 'automated_sender' },
  { email: 'no-reply@account.canva.com', reason: 'automated_sender' },
  { email: 'no-reply@marketing.base44.com', reason: 'automated_sender' },
];

module.exports = { INBOX_CONTACTS, SKIPPED };
