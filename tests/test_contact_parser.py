from src.contact_parser import parse_sender


def test_full_name_and_company():
    result = parse_sender("Mario Rossi <mario.rossi@acme.com>")
    assert result is not None
    assert result.email == "mario.rossi@acme.com"
    assert result.first_name == "Mario"
    assert result.last_name == "Rossi"
    assert result.company == "Acme"
    assert result.domain == "acme.com"


def test_no_reply_returns_none():
    assert parse_sender("no-reply@example.com") is None
    assert parse_sender("noreply@service.io") is None
    assert parse_sender("do-not-reply@newsletter.com") is None
    assert parse_sender("mailer-daemon@example.com") is None


def test_known_company():
    result = parse_sender("Alice <alice@github.com>")
    assert result is not None
    assert result.company == "GitHub"


def test_personal_domain_no_company():
    result = parse_sender("Luca Bianchi <luca@gmail.com>")
    assert result is not None
    assert result.company is None


def test_missing_display_name_parses_local():
    result = parse_sender("john.doe@startup.io")
    assert result is not None
    assert result.first_name == "John"
    assert result.last_name == "Doe"


def test_invalid_email_returns_none():
    assert parse_sender("") is None
    assert parse_sender("not-an-email") is None


def test_single_name():
    result = parse_sender("Valentina <valentina@corp.it>")
    assert result is not None
    assert result.first_name == "Valentina"
    assert result.last_name is None
