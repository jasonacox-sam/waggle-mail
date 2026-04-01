import os
import pytest
from unittest.mock import patch, Mock, MagicMock, call
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import waggle


def test_append_sent_default_folder(mock_imap, base_config):
    """Auto-detection tries 'Sent' first and succeeds."""
    with patch("waggle.imaplib.IMAP4_SSL", return_value=mock_imap):
        result = waggle._imap_append_sent(base_config, b"test message")
        assert result == "Sent"
        mock_imap.select.assert_called_once_with("Sent", readonly=True)
        mock_imap.append.assert_called_once()
        args = mock_imap.append.call_args[0]
        assert args[0] == "Sent"
        assert args[1] == r"(\Seen)"
        assert args[3] == b"test message"


def test_append_sent_fallback_order(mock_imap, base_config):
    """'Sent' fails, falls back to 'Sent Items', then 'INBOX.Sent'."""
    # First two selects fail, third succeeds
    mock_imap.select.side_effect = [
        Exception("Folder not found"),  # "Sent" fails
        ("NO", []),  # "Sent Items" fails
        ("OK", []),  # "INBOX.Sent" succeeds
    ]

    with patch("waggle.imaplib.IMAP4_SSL", return_value=mock_imap):
        result = waggle._imap_append_sent(base_config, b"test message")
        assert result == "INBOX.Sent"
        # Should have tried all three
        assert mock_imap.select.call_count == 3
        mock_imap.append.assert_called_once()
        args = mock_imap.append.call_args[0]
        assert args[0] == "INBOX.Sent"


def test_append_sent_explicit_folder(mock_imap, base_config):
    """sent_folder='Archive.Sent' skips auto-detect."""
    with patch("waggle.imaplib.IMAP4_SSL", return_value=mock_imap):
        result = waggle._imap_append_sent(base_config, b"test message", folder="Archive.Sent")
        assert result == "Archive.Sent"
        # Should NOT have called select (no auto-detection)
        mock_imap.select.assert_not_called()
        mock_imap.append.assert_called_once()
        args = mock_imap.append.call_args[0]
        assert args[0] == "Archive.Sent"


def test_append_sent_smtp_succeeds_on_imap_failure(base_config, caplog):
    """IMAP append raises, SMTP send still completes, warning logged."""
    mock_smtp_ssl = MagicMock()
    mock_smtp_ssl.__enter__ = Mock(return_value=mock_smtp_ssl)
    mock_smtp_ssl.__exit__ = Mock(return_value=False)

    mock_imap = MagicMock()
    mock_imap.select.side_effect = Exception("IMAP connection failed")

    with patch("waggle.smtplib.SMTP_SSL", return_value=mock_smtp_ssl), \
         patch("waggle.imaplib.IMAP4_SSL", return_value=mock_imap), \
         caplog.at_level("WARNING"):
        waggle.send_email(
            to="recipient@example.com",
            subject="Test",
            body_md="Test body",
            config=base_config,
            save_sent=True,
        )
        # SMTP should have succeeded
        mock_smtp_ssl.sendmail.assert_called_once()
        # IMAP failure should be logged as warning (either "No suitable Sent folder" or "Sent folder append failed")
        assert "Sent folder" in caplog.text or "IMAP" in caplog.text


def test_save_sent_false_skips_append(base_config):
    """save_sent=False — no IMAP connection attempted."""
    mock_smtp_ssl = MagicMock()
    mock_smtp_ssl.__enter__ = Mock(return_value=mock_smtp_ssl)
    mock_smtp_ssl.__exit__ = Mock(return_value=False)

    with patch("waggle.smtplib.SMTP_SSL", return_value=mock_smtp_ssl), \
         patch("waggle.imaplib.IMAP4_SSL") as mock_imap_class:
        waggle.send_email(
            to="recipient@example.com",
            subject="Test",
            body_md="Test body",
            config=base_config,
            save_sent=False,
        )
        # SMTP should succeed
        mock_smtp_ssl.sendmail.assert_called_once()
        # IMAP should not be called
        mock_imap_class.assert_not_called()


def test_no_imap_configured_skips_append():
    """No imap_host — no IMAP connection attempted."""
    config = {
        "host": "smtp.example.com",
        "port": 465,
        "user": "test@example.com",
        "password": "testpass",
        "from_addr": "test@example.com",
        "tls": True,
        # No imap_host
    }

    mock_smtp_ssl = MagicMock()
    mock_smtp_ssl.__enter__ = Mock(return_value=mock_smtp_ssl)
    mock_smtp_ssl.__exit__ = Mock(return_value=False)

    with patch("waggle.smtplib.SMTP_SSL", return_value=mock_smtp_ssl), \
         patch("waggle.imaplib.IMAP4_SSL") as mock_imap_class:
        waggle.send_email(
            to="recipient@example.com",
            subject="Test",
            body_md="Test body",
            config=config,
            save_sent=True,
        )
        # SMTP should succeed
        mock_smtp_ssl.sendmail.assert_called_once()
        # IMAP should not be called
        mock_imap_class.assert_not_called()


def test_sent_folder_validation():
    """Folder name with control chars is rejected via _validate_folder_name()."""
    with pytest.raises(ValueError, match="Folder name contains control characters"):
        waggle._validate_folder_name("Sent\r\nINJECTION")


def test_env_var_sent_folder(monkeypatch):
    """WAGGLE_SENT_FOLDER env var is picked up by _build_cfg()."""
    monkeypatch.setenv("WAGGLE_SENT_FOLDER", "MySentFolder")
    cfg = waggle._build_cfg()
    assert cfg["sent_folder"] == "MySentFolder"


def test_seen_flag_set(mock_imap, base_config):
    """Appended message has \\Seen flag."""
    with patch("waggle.imaplib.IMAP4_SSL", return_value=mock_imap):
        waggle._imap_append_sent(base_config, b"test message")
        mock_imap.append.assert_called_once()
        args = mock_imap.append.call_args[0]
        # Second argument should be the flags
        assert args[1] == r"(\Seen)"


def test_save_sent_parameter_validation():
    """Invalid save_sent type (non-bool) raises TypeError."""
    with pytest.raises(TypeError, match="save_sent must be bool"):
        waggle.send_email(
            to="test@example.com",
            subject="Test",
            body_md="Body",
            save_sent="true",  # String instead of bool
        )


def test_sent_folder_parameter_validation():
    """Invalid sent_folder type (non-str, non-None) raises TypeError."""
    with pytest.raises(TypeError, match="sent_folder must be str or None"):
        waggle.send_email(
            to="test@example.com",
            subject="Test",
            body_md="Body",
            sent_folder=123,  # Int instead of str
        )


def test_append_sent_returns_folder_name(mock_imap, base_config):
    """_imap_append_sent() returns the folder name used on success."""
    with patch("waggle.imaplib.IMAP4_SSL", return_value=mock_imap):
        result = waggle._imap_append_sent(base_config, b"test message")
        assert result == "Sent"

        # Test with explicit folder
        result = waggle._imap_append_sent(base_config, b"test message", folder="MyFolder")
        assert result == "MyFolder"


def test_append_sent_with_attachments(mock_imap, base_config, tmp_path):
    """Verify attachments are preserved in appended message."""
    # Create a test attachment
    attachment_path = tmp_path / "test.txt"
    attachment_path.write_text("test content")

    captured_msg = None

    def capture_append(folder, flags, date, msg_bytes):
        nonlocal captured_msg
        captured_msg = msg_bytes
        return ("OK", [])

    mock_imap.append.side_effect = capture_append

    mock_smtp_ssl = MagicMock()
    mock_smtp_ssl.__enter__ = Mock(return_value=mock_smtp_ssl)
    mock_smtp_ssl.__exit__ = Mock(return_value=False)

    with patch("waggle.smtplib.SMTP_SSL", return_value=mock_smtp_ssl), \
         patch("waggle.imaplib.IMAP4_SSL", return_value=mock_imap):
        waggle.send_email(
            to="recipient@example.com",
            subject="Test",
            body_md="Test body",
            attachments=[str(attachment_path)],
            config=base_config,
        )

        # Verify IMAP append was called and message contains attachment
        assert captured_msg is not None
        assert b"test.txt" in captured_msg
        assert b"test content" in captured_msg or b"dGVzdCBjb250ZW50" in captured_msg  # base64


def test_msg_as_bytes_encoding(mock_imap, base_config):
    """Verify msg.as_bytes() produces valid RFC822 format."""
    captured_msg = None

    def capture_append(folder, flags, date, msg_bytes):
        nonlocal captured_msg
        captured_msg = msg_bytes
        return ("OK", [])

    mock_imap.append.side_effect = capture_append

    mock_smtp_ssl = MagicMock()
    mock_smtp_ssl.__enter__ = Mock(return_value=mock_smtp_ssl)
    mock_smtp_ssl.__exit__ = Mock(return_value=False)

    with patch("waggle.smtplib.SMTP_SSL", return_value=mock_smtp_ssl), \
         patch("waggle.imaplib.IMAP4_SSL", return_value=mock_imap):
        waggle.send_email(
            to="recipient@example.com",
            subject="Test Subject",
            body_md="# Test\n\nBody content",
            config=base_config,
        )

        # Verify message format
        assert captured_msg is not None
        assert isinstance(captured_msg, bytes)
        assert b"Subject: Test Subject" in captured_msg
        assert b"To: recipient@example.com" in captured_msg
        # Should have both plain and HTML parts
        assert b"text/plain" in captured_msg
        assert b"text/html" in captured_msg


def test_append_sent_invalid_msg_bytes(base_config, caplog):
    """_imap_append_sent() with None or non-bytes returns None, logs warning."""
    with caplog.at_level("WARNING"):
        # Test with None
        result = waggle._imap_append_sent(base_config, None)
        assert result is None
        assert "Invalid message bytes" in caplog.text

        caplog.clear()

        # Test with string instead of bytes
        result = waggle._imap_append_sent(base_config, "not bytes")
        assert result is None
        assert "Invalid message bytes" in caplog.text


def test_cli_no_save_sent_flag(monkeypatch):
    """waggle send --no-save-sent passes save_sent=False."""
    # Set up environment for CLI
    monkeypatch.setenv("WAGGLE_HOST", "smtp.example.com")
    monkeypatch.setenv("WAGGLE_USER", "test@example.com")
    monkeypatch.setenv("WAGGLE_PASS", "testpass")
    monkeypatch.setenv("WAGGLE_FROM", "test@example.com")

    mock_smtp_ssl = MagicMock()
    mock_smtp_ssl.__enter__ = Mock(return_value=mock_smtp_ssl)
    mock_smtp_ssl.__exit__ = Mock(return_value=False)

    with patch("waggle.smtplib.SMTP_SSL", return_value=mock_smtp_ssl), \
         patch("waggle.imaplib.IMAP4_SSL") as mock_imap_class:
        # Simulate CLI args
        import argparse
        args = argparse.Namespace(
            to="recipient@example.com",
            subject="Test",
            body="Test body",
            cc=None,
            reply_to=None,
            in_reply_to=None,
            references=None,
            from_name=None,
            attach=None,
            rich=False,
            no_save_sent=True,
        )

        waggle._cli_send(args)

        # SMTP should succeed
        mock_smtp_ssl.sendmail.assert_called_once()
        # IMAP should not be called (save_sent=False)
        mock_imap_class.assert_not_called()
