'use strict';

const hubspot = require('@hubspot/api-client');

let _client;
function getClient() {
  if (!_client) {
    _client = new hubspot.Client({ accessToken: process.env.HUBSPOT_ACCESS_TOKEN });
  }
  return _client;
}

/**
 * Search for a contact by email address.
 * Returns the contact object or null if not found.
 */
async function findContactByEmail(email) {
  const client = getClient();
  const res = await client.crm.contacts.searchApi.doSearch({
    filterGroups: [
      {
        filters: [{ propertyName: 'email', operator: 'EQ', value: email }],
      },
    ],
    properties: ['email', 'firstname', 'lastname', 'company', 'hs_lead_status', 'hs_analytics_source'],
    limit: 1,
  });
  return res.results.length > 0 ? res.results[0] : null;
}

/**
 * Create a new HubSpot contact.
 */
async function createContact(properties) {
  const client = getClient();
  const res = await client.crm.contacts.basicApi.create({ properties });
  return res;
}

/**
 * Update an existing HubSpot contact, filling in only missing/blank fields.
 */
async function updateContact(contactId, properties) {
  const client = getClient();
  const res = await client.crm.contacts.basicApi.update(contactId, { properties });
  return res;
}

/**
 * Build the set of properties to write for a new contact.
 */
function buildContactProperties({ email, firstName, lastName, company, subject }) {
  const domain = email.split('@')[1] || '';
  return {
    email,
    firstname: firstName || email.split('@')[0],
    lastname: lastName || '',
    company: company || '',
    hs_lead_status: 'NEW',
    // hs_analytics_source is read-only; use a custom property if available
    // For tracking we rely on the lead_source_detail custom field if present
    // lead_source_detail: 'Inbound Gmail',
    // Store raw source context in notes or lifecycle stage
    // Lifecycle / source tracking via standard field:
    // "Gmail" is not a default HubSpot source enum; use "OTHER" for the hs_analytics_source
  };
}

module.exports = { findContactByEmail, createContact, updateContact, buildContactProperties };
