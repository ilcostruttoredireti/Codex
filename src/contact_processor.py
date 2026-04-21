import re
import logging

log = logging.getLogger(__name__)

# Domini personali: non usare il dominio come nome azienda
_PERSONAL_DOMAINS = {
    'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com',
    'live.com', 'icloud.com', 'me.com', 'mac.com', 'aol.com',
    'protonmail.com', 'proton.me', 'mail.com', 'gmx.com',
    'yandex.com', 'zoho.com', 'fastmail.com', 'tutanota.com',
    'yahoo.it', 'hotmail.it', 'libero.it', 'virgilio.it',
    'alice.it', 'tim.it', 'tiscali.it', 'email.it',
}


class ContactProcessor:
    def extract(self, email_data):
        from_header = (email_data.get('from') or '').strip()
        if not from_header:
            return None

        sender_email, first_name, last_name = self._parse_from(from_header)
        if not sender_email or '@' not in sender_email:
            return None

        sender_email = sender_email.lower().strip()
        domain = sender_email.split('@')[1]
        company = self._company_from_domain(domain)

        return {
            'email': sender_email,
            'first_name': first_name,
            'last_name': last_name,
            'company': company,
            'domain': domain,
            'gmail_message_id': email_data.get('id'),
            'subject': email_data.get('subject', ''),
            'date': email_data.get('date', ''),
        }

    def _parse_from(self, from_header):
        # "Display Name <email@domain>" or '"Display Name" <email@domain>'
        match = re.match(r'^"?([^"<]*?)"?\s*<([^>]+)>\s*$', from_header)
        if match:
            display = match.group(1).strip().strip('"').strip()
            email = match.group(2).strip()
            if '@' not in email:
                return None, None, None
            first, last = self._split_name(display) if display else (None, None)
            return email, first, last

        # Indirizzo email semplice
        if re.match(r'^[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+$', from_header):
            local = from_header.split('@')[0]
            first, last = self._name_from_local(local)
            return from_header, first, last

        return None, None, None

    def _split_name(self, full_name):
        parts = full_name.split()
        if not parts:
            return None, None
        if len(parts) == 1:
            return parts[0].capitalize(), None
        return parts[0].capitalize(), ' '.join(parts[1:]).title()

    def _name_from_local(self, local):
        # "john.doe" → "John", "Doe"
        clean = re.sub(r'[^a-zA-Z.]', '.', local)
        parts = [p.capitalize() for p in clean.split('.') if len(p) > 1]
        if len(parts) >= 2:
            return parts[0], parts[-1]
        if len(parts) == 1:
            return parts[0], None
        return None, None

    def _company_from_domain(self, domain):
        if domain in _PERSONAL_DOMAINS:
            return None
        name = domain.split('.')[0]
        return name.capitalize() if name else None
