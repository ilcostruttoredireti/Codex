import { google } from 'googleapis';

export class GmailPoller {
  constructor(gmailConfig) {
    const auth = new google.auth.OAuth2(
      gmailConfig.clientId,
      gmailConfig.clientSecret,
    );
    auth.setCredentials({ refresh_token: gmailConfig.refreshToken });
    this.gmail = google.gmail({ version: 'v1', auth });
  }

  /**
   * Fetches inbox messages received after `afterDate` (ISO string).
   * Returns a flat array of { id, sender, date } objects.
   */
  async fetchInboxMessages(afterDate) {
    const query = afterDate
      ? `in:inbox -from:me after:${this._toGmailDate(afterDate)}`
      : 'in:inbox -from:me newer_than:7d';

    const messages = [];
    let pageToken;

    do {
      const res = await this.gmail.users.messages.list({
        userId: 'me',
        q: query,
        maxResults: 100,
        pageToken,
      });

      const batch = res.data.messages || [];
      for (const { id } of batch) {
        const msg = await this.gmail.users.messages.get({
          userId: 'me',
          id,
          format: 'metadata',
          metadataHeaders: ['From', 'Date'],
        });

        const headers = msg.data.payload?.headers || [];
        const from = headers.find(h => h.name === 'From')?.value || '';
        const date = headers.find(h => h.name === 'Date')?.value || '';

        messages.push({
          id,
          sender: from,
          date: date ? new Date(date).toISOString() : null,
        });
      }

      pageToken = res.data.nextPageToken;
    } while (pageToken);

    return messages;
  }

  _toGmailDate(isoString) {
    // Gmail "after:" expects YYYY/MM/DD
    return isoString.slice(0, 10).replace(/-/g, '/');
  }
}
