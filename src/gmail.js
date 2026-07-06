'use strict';

const { google } = require('googleapis');

function createGmailClient() {
  const oauth2Client = new google.auth.OAuth2(
    process.env.GOOGLE_CLIENT_ID,
    process.env.GOOGLE_CLIENT_SECRET,
  );
  oauth2Client.setCredentials({ refresh_token: process.env.GOOGLE_REFRESH_TOKEN });
  return google.gmail({ version: 'v1', auth: oauth2Client });
}

// Return message IDs for inbox messages newer than `sinceDate`
async function fetchInboxSenders(gmail, sinceDate) {
  const afterEpoch = Math.floor(sinceDate.getTime() / 1000);
  const senders = new Map(); // email → senderHeader (keeps first occurrence)

  let pageToken;
  do {
    const res = await gmail.users.messages.list({
      userId: 'me',
      q: `in:inbox -in:sent after:${afterEpoch}`,
      maxResults: 500,
      pageToken,
      fields: 'messages(id),nextPageToken',
    });

    const messages = res.data.messages || [];

    // Fetch headers for all messages in this page concurrently (batched)
    const headers = await Promise.all(
      messages.map(m =>
        gmail.users.messages.get({
          userId: 'me',
          id: m.id,
          format: 'metadata',
          metadataHeaders: ['From', 'Date'],
          fields: 'id,payload/headers',
        }).then(r => r.data).catch(() => null),
      ),
    );

    for (const msg of headers) {
      if (!msg) continue;
      const fromHeader = msg.payload?.headers?.find(h => h.name === 'From');
      if (fromHeader?.value) {
        const emailMatch = fromHeader.value.match(/[^\s<>]+@[^\s<>]+/);
        const key = emailMatch ? emailMatch[0].toLowerCase() : fromHeader.value;
        if (!senders.has(key)) {
          senders.set(key, fromHeader.value);
        }
      }
    }

    pageToken = res.data.nextPageToken;
  } while (pageToken);

  return Array.from(senders.values());
}

module.exports = { createGmailClient, fetchInboxSenders };
