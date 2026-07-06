'use strict';

const hubspot = require('@hubspot/api-client');

function createHubspotClient() {
  return new hubspot.Client({ accessToken: process.env.HUBSPOT_ACCESS_TOKEN });
}

// Look up a contact by email. Returns the contact object or null.
async function findContactByEmail(client, email) {
  try {
    const res = await client.crm.contacts.searchApi.doSearch({
      filterGroups: [{
        filters: [{
          propertyName: 'email',
          operator: 'EQ',
          value: email,
        }],
      }],
      properties: ['email', 'firstname', 'lastname', 'company', 'hs_analytics_source_data_1'],
      limit: 1,
    });
    return res.results[0] || null;
  } catch (err) {
    console.error(`HubSpot search error for ${email}:`, err.message);
    return null;
  }
}

// Create a new HubSpot contact and add an "Inbound Gmail" note.
async function createContact(client, fields, dryRun) {
  if (dryRun) {
    console.log(`[DRY-RUN] Would CREATE contact: ${fields.email}`);
    return { id: 'dry-run', status: 'Creato' };
  }

  const properties = {
    email: fields.email,
    firstname: fields.firstname,
    company: fields.company,
  };
  if (fields.lastname) properties.lastname = fields.lastname;

  const contact = await client.crm.contacts.basicApi.create({ properties });

  await client.crm.objects.notesApi.create({
    properties: {
      hs_note_body: `Contatto aggiunto automaticamente dal monitoraggio email Gmail.\nDominio: ${fields.domain}`,
      hs_timestamp: new Date().toISOString(),
    },
    associations: [{
      to: { id: contact.id },
      types: [{ associationCategory: 'HUBSPOT_DEFINED', associationTypeId: 202 }],
    }],
  }).catch(() => {});

  return { id: contact.id, status: 'Creato' };
}

// Update a contact: fill in any missing fields.
async function updateContact(client, contactId, existing, fields, dryRun) {
  const updates = {};

  if (!existing.properties.company && fields.company) updates.company = fields.company;
  if (!existing.properties.firstname && fields.firstname) updates.firstname = fields.firstname;
  if (!existing.properties.lastname && fields.lastname) updates.lastname = fields.lastname;

  if (Object.keys(updates).length === 0) {
    return { id: contactId, status: 'Ignorato' };
  }

  if (dryRun) {
    console.log(`[DRY-RUN] Would UPDATE contact ${contactId} (${fields.email}):`, updates);
    return { id: contactId, status: 'Aggiornato' };
  }

  await client.crm.contacts.basicApi.update(contactId, { properties: updates });
  return { id: contactId, status: 'Aggiornato' };
}

module.exports = { createHubspotClient, findContactByEmail, createContact, updateContact };
