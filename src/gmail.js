'use strict';

const { google } = require('googleapis');

/**
 * Builds an authenticated Gmail client using OAuth2 refresh token.
 */
function buildGmailClient() {
  const auth = new google.auth.OAuth2(
    process.env.GMAIL_CLIENT_ID,
    process.env.GMAIL_CLIENT_SECRET,
  );
  auth.setCredentials({ refresh_token: process.env.GMAIL_REFRESH_TOKEN });
  return google.gmail({ version: 'v1', auth });
}

/**
 * Fetches inbox messages newer than `afterTimestamp` (Unix seconds).
 * Returns raw message objects with From, Subject, Date headers.
 *
 * @param {number} afterTimestamp  Unix timestamp (seconds)
 * @param {number} maxResults
 * @returns {Promise<Array<{from, subject, date, messageId}>>}
 */
async function fetchInboxMessages(afterTimestamp, maxResults = 100) {
  const gmail = buildGmailClient();

  // Gmail query: inbox messages after the given date, excluding drafts and sent
  const query = `in:inbox after:${afterTimestamp} -in:draft -in:sent`;

  const listRes = await gmail.users.messages.list({
    userId: 'me',
    q: query,
    maxResults,
  });

  const messages = listRes.data.messages || [];
  if (messages.length === 0) return [];

  // Fetch headers for each message in parallel (batched for efficiency)
  const BATCH = 20;
  const results = [];

  for (let i = 0; i < messages.length; i += BATCH) {
    const batch = messages.slice(i, i + BATCH);
    const fetched = await Promise.all(
      batch.map(m =>
        gmail.users.messages.get({
          userId: 'me',
          id: m.id,
          format: 'metadata',
          metadataHeaders: ['From', 'Subject', 'Date'],
        }),
      ),
    );

    for (const res of fetched) {
      const headers = res.data.payload?.headers || [];
      const get = name => headers.find(h => h.name === name)?.value || '';
      results.push({
        messageId: res.data.id,
        from: get('From'),
        subject: get('Subject'),
        date: get('Date'),
      });
    }
  }

  return results;
}

module.exports = { fetchInboxMessages };
