'use strict';

const hubspot = require('@hubspot/api-client');

let _client = null;

function getClient() {
  if (!_client) {
    _client = new hubspot.Client({ accessToken: process.env.HUBSPOT_ACCESS_TOKEN });
  }
  return _client;
}

/**
 * Finds an existing HubSpot contact by email.
 * @returns {Promise<{id: string, properties: object} | null>}
 */
async function findContactByEmail(email) {
  const client = getClient();
  try {
    const response = await client.crm.contacts.searchApi.doSearch({
      filterGroups: [
        {
          filters: [{ propertyName: 'email', operator: 'EQ', value: email }],
        },
      ],
      properties: ['email', 'firstname', 'lastname', 'company', 'hs_analytics_source', 'hs_lead_status'],
      limit: 1,
    });
    return response.results[0] || null;
  } catch (err) {
    if (err.statusCode === 404) return null;
    throw err;
  }
}

/**
 * Creates a new HubSpot contact with the given properties.
 * Sets source to "Gmail" and tag to "Inbound Gmail".
 * @returns {Promise<{id: string}>}
 */
async function createContact({ email, firstName, lastName, company }) {
  const client = getClient();
  const response = await client.crm.contacts.basicApi.create({
    properties: {
      email,
      firstname: firstName,
      lastname: lastName,
      company,
      hs_analytics_source: 'OTHER_CAMPAIGNS',
      hs_analytics_source_data_1: 'Gmail',
      hs_lead_status: 'NEW',
      // Custom tag stored in the notes/description field
      website: '',
    },
  });
  return { id: response.id };
}

/**
 * Updates an existing HubSpot contact with any missing fields.
 * Only patches properties that are currently empty/null.
 *
 * @param {string} contactId
 * @param {object} existing  Current properties from HubSpot
 * @param {object} candidate  Derived values from the email
 * @returns {Promise<{updated: boolean, fields: string[]}>}
 */
async function updateContactIfNeeded(contactId, existing, candidate) {
  const patch = {};

  const fill = (prop, value) => {
    if (value && !existing[prop]) patch[prop] = value;
  };

  fill('firstname', candidate.firstName);
  fill('lastname', candidate.lastName);
  fill('company', candidate.company);

  // Always ensure source is recorded
  if (!existing.hs_analytics_source) {
    patch.hs_analytics_source = 'OTHER_CAMPAIGNS';
    patch.hs_analytics_source_data_1 = 'Gmail';
  }

  if (Object.keys(patch).length === 0) {
    return { updated: false, fields: [] };
  }

  const client = getClient();
  await client.crm.contacts.basicApi.update(contactId, { properties: patch });
  return { updated: true, fields: Object.keys(patch) };
}

/**
 * Creates a timeline note on a HubSpot contact to record the email activity.
 *
 * @param {string} contactId
 * @param {{ subject: string, date: string }} email
 */
async function addEmailActivityNote(contactId, email) {
  const client = getClient();
  await client.crm.objects.notes.basicApi.create({
    properties: {
      hs_note_body: `📧 Inbound Gmail\nOggetto: ${email.subject}\nData: ${email.date}`,
      hs_timestamp: Date.now().toString(),
    },
    associations: [
      {
        to: { id: contactId },
        types: [
          {
            associationCategory: 'HUBSPOT_DEFINED',
            associationTypeId: 202,
          },
        ],
      },
    ],
  });
}

module.exports = { findContactByEmail, createContact, updateContactIfNeeded, addEmailActivityNote };
