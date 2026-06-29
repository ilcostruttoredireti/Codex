const hubspot = require('@hubspot/api-client');

function buildHubSpotClient() {
  return new hubspot.Client({ accessToken: process.env.HUBSPOT_ACCESS_TOKEN });
}

// Search for a contact by email; returns null if not found
async function findContactByEmail(client, email) {
  const res = await client.crm.contacts.searchApi.doSearch({
    filterGroups: [{
      filters: [{ propertyName: 'email', operator: 'EQ', value: email }],
    }],
    properties: ['email', 'firstname', 'lastname', 'company', 'hs_lead_source'],
    limit: 1,
  });
  return res.results[0] || null;
}

// Create a new contact; returns the created object
async function createContact(client, props) {
  return client.crm.contacts.basicApi.create({ properties: props });
}

// Update an existing contact with only the properties that are missing/empty
async function updateContact(client, contactId, existing, incoming) {
  const updates = {};

  if (!existing.properties.firstname && incoming.firstname) {
    updates.firstname = incoming.firstname;
  }
  if (!existing.properties.lastname && incoming.lastname) {
    updates.lastname = incoming.lastname;
  }
  if (!existing.properties.company && incoming.company) {
    updates.company = incoming.company;
  }
  if (!existing.properties.hs_lead_source) {
    updates.hs_lead_source = 'Gmail';
  }

  if (Object.keys(updates).length === 0) return null;
  return client.crm.contacts.basicApi.update(contactId, { properties: updates });
}

// Add an "email received" activity note to a contact's timeline
async function logEmailActivity(client, contactId, subject, senderEmail) {
  await client.crm.objects.notes.basicApi.create({
    properties: {
      hs_note_body: `Email ricevuta da ${senderEmail}: "${subject}"`,
      hs_timestamp: new Date().toISOString(),
    },
    associations: [{
      to: { id: contactId },
      types: [{ associationCategory: 'HUBSPOT_DEFINED', associationTypeId: 202 }],
    }],
  });
}

module.exports = { buildHubSpotClient, findContactByEmail, createContact, updateContact, logEmailActivity };
