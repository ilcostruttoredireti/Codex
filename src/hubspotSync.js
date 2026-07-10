import hubspot from '@hubspot/api-client';

// hs_analytics_source accepts HubSpot's predefined enum values
const ANALYTICS_SOURCE_PROPERTY = 'hs_analytics_source';
const ANALYTICS_SOURCE_VALUE = 'EMAIL_MARKETING';
// hs_analytics_source_data_1 is a free-text detail field for the specific source
const SOURCE_DETAIL_PROPERTY = 'hs_analytics_source_data_1';
const SOURCE_DETAIL_VALUE = 'Inbound Gmail';

export class HubSpotSync {
  constructor(accessToken) {
    this.client = new hubspot.Client({ accessToken });
  }

  /**
   * Syncs a contact extracted from a Gmail message.
   * Returns { status, email, hubspotId }
   *   status: "created" | "updated" | "ignored"
   */
  async syncContact(contact) {
    const existing = await this._findByEmail(contact.email);

    if (existing) {
      const updated = await this._updateIfNeeded(existing, contact);
      return {
        status: updated ? 'updated' : 'ignored',
        email: contact.email,
        hubspotId: existing.id,
      };
    }

    const created = await this._createContact(contact);
    return {
      status: 'created',
      email: contact.email,
      hubspotId: created.id,
    };
  }

  async _findByEmail(email) {
    try {
      const res = await this.client.crm.contacts.searchApi.doSearch({
        filterGroups: [{
          filters: [{
            propertyName: 'email',
            operator: 'EQ',
            value: email,
          }],
        }],
        properties: ['email', 'firstname', 'lastname', 'company', ANALYTICS_SOURCE_PROPERTY, SOURCE_DETAIL_PROPERTY],
        limit: 1,
      });
      return res.results?.[0] ?? null;
    } catch (err) {
      if (err.code === 404) return null;
      throw err;
    }
  }

  async _updateIfNeeded(existing, contact) {
    const props = existing.properties;
    const updates = {};

    if (!props.company && contact.company) updates.company = contact.company;
    if (!props.firstname && contact.firstName) updates.firstname = contact.firstName;
    if (!props.lastname && contact.lastName) updates.lastname = contact.lastName;
    if (!props[ANALYTICS_SOURCE_PROPERTY]) updates[ANALYTICS_SOURCE_PROPERTY] = ANALYTICS_SOURCE_VALUE;
    if (!props[SOURCE_DETAIL_PROPERTY]) updates[SOURCE_DETAIL_PROPERTY] = SOURCE_DETAIL_VALUE;

    if (Object.keys(updates).length === 0) return false;

    await this.client.crm.contacts.basicApi.update(existing.id, { properties: updates });
    return true;
  }

  async _createContact(contact) {
    const properties = {
      email: contact.email,
      firstname: contact.firstName,
      lastname: contact.lastName,
      company: contact.company,
      [ANALYTICS_SOURCE_PROPERTY]: ANALYTICS_SOURCE_VALUE,
      [SOURCE_DETAIL_PROPERTY]: SOURCE_DETAIL_VALUE,
    };

    // Remove blank values to avoid overwriting HubSpot defaults
    for (const [k, v] of Object.entries(properties)) {
      if (!v) delete properties[k];
    }

    return await this.client.crm.contacts.basicApi.create({ properties });
  }

  /**
   * Logs an "email received" engagement on the contact's timeline.
   */
  async logEmailActivity(hubspotId, contact) {
    try {
      await this.client.crm.objects.notes.basicApi.create({
        properties: {
          hs_note_body: `📧 Email ricevuta da Gmail\nDa: ${contact.rawFrom}\nData: ${contact.receivedAt}\nFonte: Gmail Inbound Sync`,
          hs_timestamp: new Date(contact.receivedAt).getTime().toString(),
        },
        associations: [{
          to: { id: hubspotId },
          types: [{ associationCategory: 'HUBSPOT_DEFINED', associationTypeId: 202 }],
        }],
      });
    } catch {
      // Timeline logging is best-effort; don't fail the sync
    }
  }
}
