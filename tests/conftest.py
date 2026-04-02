import pytest
from unittest.mock import Mock, MagicMock


@pytest.fixture
def mock_imap():
    """Mock IMAP4_SSL connection."""
    m = MagicMock()
    m.select.return_value = ("OK", [])
    m.append.return_value = ("OK", [])
    m.close.return_value = None
    m.logout.return_value = ("BYE", [])
    return m


@pytest.fixture
def mock_smtp_ssl():
    """Mock SMTP_SSL connection."""
    s = MagicMock()
    s.login.return_value = None
    s.sendmail.return_value = {}
    return s


@pytest.fixture
def mock_smtp():
    """Mock SMTP connection (STARTTLS)."""
    s = MagicMock()
    s.ehlo.return_value = None
    s.starttls.return_value = None
    s.login.return_value = None
    s.sendmail.return_value = {}
    return s


@pytest.fixture
def base_config():
    """Basic configuration for testing."""
    return {
        "host": "smtp.example.com",
        "port": 465,
        "user": "test@example.com",
        "password": "testpass",
        "from_addr": "test@example.com",
        "from_name": "Test User",
        "tls": True,
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "imap_tls": True,
    }
