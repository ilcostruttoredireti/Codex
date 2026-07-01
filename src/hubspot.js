import { Client } from '@hubspot/api-client';

export function createHubSpotClient(accessToken) {
  return new Client({ accessToken });
}

export async function findContactByEmail(client, email) {
  try {
    const res = await client.crm.contacts.searchApi.doSearch({
      filterGroups: [{
        filters: [{
          propertyName: 'email',
          operator: 'EQ',
          value: email,
        }],
      }],
      properties: ['email', 'firstname', 'lastname', 'company', 'hs_analytics_source'],
      limit: 1,
    });
    return res.results[0] ?? null;
  } catch {
    return null;
  }
}

export async function createContact(client, { email, firstname, lastname, company }, dryRun = false) {
  const properties = {
    email,
    ...(firstname && { firstname }),
    ...(lastname && { lastname }),
    ...(company && { company }),
    hs_analytics_source: 'EMAIL_MARKETING',
    hs_lead_status: 'NEW',
  };

  if (dryRun) {
    return { id: 'DRY_RUN', properties };
  }

  const res = await client.crm.contacts.basicApi.create({ properties });
  await addGmailNote(client, res.id);
  return res;
}

export async function updateContact(client, contactId, { email, firstname, lastname, company }, dryRun = false) {
  const existing = await client.crm.contacts.basicApi.getById(contactId, [
    'firstname', 'lastname', 'company', 'hs_analytics_source',
  ]);

  const updates = {};
  if (!existing.properties.firstname && firstname) updates.firstname = firstname;
  if (!existing.properties.lastname && lastname) updates.lastname = lastname;
  if (!existing.properties.company && company) updates.company = company;

  if (Object.keys(updates).length === 0) {
    return { id: contactId, updated: false };
  }

  if (dryRun) {
    return { id: contactId, updated: true, updates };
  }

  await client.crm.contacts.basicApi.update(contactId, { properties: updates });
  return { id: contactId, updated: true, updates };
}

async function addGmailNote(client, contactId) {
  try {
    const note = await client.crm.objects.basicApi.create('notes', {
      properties: {
        hs_note_body: 'Contatto acquisito tramite email in arrivo su Gmail. Fonte: Gmail. Tag: Inbound Gmail.',
        hs_timestamp: new Date().toISOString(),
      },
    });

    await client.crm.associations.v4.basicApi.create(
      'notes',
      note.id,
      'contacts',
      contactId,
      [{ associationCategory: 'HUBSPOT_DEFINED', associationTypeId: 202 }]
    );
  } catch {
    // note creation is best-effort
  }
}
