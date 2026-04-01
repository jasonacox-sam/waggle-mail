"""Shared fixtures for waggle tests."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure waggle module is importable from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def mock_imap():
    """Provide a mock IMAP4 connection with configurable fetch responses."""
    mock_conn = MagicMock()
    mock_conn.select.return_value = ("OK", [b"1"])
    mock_conn.logout.return_value = ("BYE", [])
    return mock_conn


@pytest.fixture
def imap_config():
    """Minimal config dict that enables IMAP without hitting real servers."""
    return {
        "imap_host": "test.example.com",
        "imap_port": 993,
        "imap_tls": True,
        "user": "test@example.com",
        "password": "secret",
        "host": "test.example.com",
        "port": 465,
        "from_addr": "test@example.com",
        "from_name": "",
        "tls": True,
    }


@pytest.fixture
def sample_email_bytes():
    """A minimal RFC822 message for fetch response mocking."""
    return (
        b"From: sender@example.com\r\n"
        b"To: recipient@example.com\r\n"
        b"Subject: Test Message\r\n"
        b"Message-ID: <test-123@example.com>\r\n"
        b"Date: Mon, 1 Apr 2026 12:00:00 +0000\r\n"
        b"\r\n"
        b"Hello, this is a test message.\r\n"
    )
