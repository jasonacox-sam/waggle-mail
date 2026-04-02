"""Shared pytest fixtures for waggle tests."""

import pytest
from unittest.mock import Mock, MagicMock


@pytest.fixture
def mock_imap():
    """Mock IMAP4_SSL connection for testing."""
    m = MagicMock()
    m.select.return_value = ("OK", None)
    m.uid.return_value = ("OK", None)
    m.logout.return_value = ("BYE", None)
    return m


@pytest.fixture
def mock_config():
    """Standard test config dict."""
    return {
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "imap_tls": True,
        "user": "test@example.com",
        "password": "testpass",
    }
