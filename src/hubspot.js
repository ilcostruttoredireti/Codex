import axios from 'axios';

const BASE_URL = 'https://api.hubapi.com';

export function createHubSpotClient(accessToken) {
  const http = axios.create({
    baseURL: BASE_URL,
    headers: {
      Authorization: `Bearer ${accessToken}`,
      'Content-Type': 'application/json',
    },
  });

  return {
    async findContactByEmail(email) {
      try {
        const res = await http.post('/crm/v3/objects/contacts/search', {
          filterGroups: [{
            filters: [{
              propertyName: 'email',
              operator: 'EQ',
              value: email.toLowerCase(),
            }],
          }],
          properties: ['email', 'firstname', 'lastname', 'company', 'hs_lead_status'],
          limit: 1,
        });
        return res.data.results[0] ?? null;
      } catch (err) {
        if (err.response?.status === 404) return null;
        throw err;
      }
    },

    async createContact(props) {
      const res = await http.post('/crm/v3/objects/contacts', { properties: props });
      return res.data;
    },

    async updateContact(id, props) {
      const res = await http.patch(`/crm/v3/objects/contacts/${id}`, { properties: props });
      return res.data;
    },

    async createNote(contactId, body) {
      const noteRes = await http.post('/crm/v3/objects/notes', {
        properties: {
          hs_note_body: body,
          hs_timestamp: new Date().toISOString(),
        },
      });
      const noteId = noteRes.data.id;

      await http.put(`/crm/v4/objects/notes/${noteId}/associations/contacts/${contactId}/202`);
      return noteId;
    },
  };
}
