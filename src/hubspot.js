import { Client } from '@hubspot/api-client';

export const createHubSpotClient = t => new Client({ accessToken: t });

export async function findContactByEmail(client, email) {
  try {
    const r = await client.crm.contacts.searchApi.doSearch({
      filterGroups: [{ filters: [{ propertyName: 'email', operator: 'EQ', value: email }] }],
      properties: ['email', 'firstname', 'lastname', 'company'], limit: 1
    });
    return r.results[0] || null;
  } catch { return null; }
}

const BASE = { hs_lead_status: 'NEW', lifecyclestage: 'lead', leadsource: 'Gmail' };

export const createContact = (c, d) =>
  c.crm.contacts.basicApi.create({ properties: { ...BASE, email: d.email, firstname: d.firstname, lastname: d.lastname, company: d.company } });

export async function updateContact(c, id, ep, d) {
  const u = {};
  if (!ep.firstname && d.firstname) u.firstname = d.firstname;
  if (!ep.lastname && d.lastname) u.lastname = d.lastname;
  if (!ep.company && d.company) u.company = d.company;
  return Object.keys(u).length ? c.crm.contacts.basicApi.update(id, { properties: u }) : null;
}

export async function syncContact(c, d) {
  const ex = await findContactByEmail(c, d.email);
  if (ex) {
    const up = await updateContact(c, ex.id, ex.properties, d);
    return { status: up ? 'Aggiornato' : 'Ignorato', id: ex.id };
  }
  const cr = await createContact(c, d);
  return { status: 'Creato', id: cr.id };
}
