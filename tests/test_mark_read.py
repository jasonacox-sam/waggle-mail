"""Tests for the mark_read parameter on read_message()."""

from unittest.mock import patch, call

import pytest

import waggle


class TestMarkReadParameter:
    """Verify mark_read controls both readonly mode and FETCH verb."""

    def test_default_behavior_unchanged(self, mock_imap, imap_config, sample_email_bytes):
        """Default mark_read=False uses BODY.PEEK[] and readonly=True."""
        mock_imap.fetch.return_value = ("OK", [(b"1 (BODY[]", sample_email_bytes)])

        with patch("waggle._imap_connect", return_value=(mock_imap, "test")):
            waggle.read_message("1", config=imap_config)

        mock_imap.select.assert_called_once_with("INBOX", readonly=True)
        fetch_verb = mock_imap.fetch.call_args[0][1]
        assert "BODY.PEEK[]" in str(fetch_verb)

    def test_mark_read_true(self, mock_imap, imap_config, sample_email_bytes):
        """mark_read=True uses BODY[] (not PEEK) and readonly=False."""
        mock_imap.fetch.return_value = ("OK", [(b"1 (BODY[]", sample_email_bytes)])

        with patch("waggle._imap_connect", return_value=(mock_imap, "test")):
            waggle.read_message("1", mark_read=True, config=imap_config)

        mock_imap.select.assert_called_once_with("INBOX", readonly=False)
        fetch_verb = mock_imap.fetch.call_args[0][1]
        assert "BODY[]" in fetch_verb
        assert "PEEK" not in fetch_verb

    def test_readonly_mode_matches_mark_read_value(self, mock_imap, imap_config, sample_email_bytes):
        """readonly=True when mark_read=False, readonly=False when mark_read=True."""
        mock_imap.fetch.return_value = ("OK", [(b"1 (BODY[]", sample_email_bytes)])

        # mark_read=False -> readonly=True
        with patch("waggle._imap_connect", return_value=(mock_imap, "test")):
            waggle.read_message("1", mark_read=False, config=imap_config)
        assert mock_imap.select.call_args == call("INBOX", readonly=True)

        mock_imap.reset_mock()
        mock_imap.select.return_value = ("OK", [b"1"])
        mock_imap.fetch.return_value = ("OK", [(b"1 (BODY[]", sample_email_bytes)])

        # mark_read=True -> readonly=False
        with patch("waggle._imap_connect", return_value=(mock_imap, "test")):
            waggle.read_message("1", mark_read=True, config=imap_config)
        assert mock_imap.select.call_args == call("INBOX", readonly=False)

    def test_invalid_parameter_type(self, imap_config):
        """Non-bool mark_read raises TypeError before any IMAP operations."""
        for bad_value in ["yes", 1, 0, None, [], {}]:
            with pytest.raises(TypeError, match="mark_read"):
                waggle.read_message("1", mark_read=bad_value, config=imap_config)

    def test_mark_read_without_imap_configured(self):
        """mark_read=True with no IMAP configured raises RuntimeError."""
        no_imap_config = {
            "imap_host": "",
            "imap_port": 993,
            "imap_tls": True,
            "user": "",
            "password": "",
            "host": "localhost",
            "port": 465,
            "from_addr": "",
            "from_name": "",
            "tls": True,
        }
        with pytest.raises(RuntimeError, match="IMAP not configured"):
            waggle.read_message("1", mark_read=True, config=no_imap_config)

    def test_mark_read_with_invalid_uid(self, mock_imap, imap_config):
        """Invalid UID fails regardless of mark_read value."""
        mock_imap.fetch.return_value = ("OK", [None])

        for mark_read in (False, True):
            mock_imap.reset_mock()
            mock_imap.select.return_value = ("OK", [b"1"])
            mock_imap.fetch.return_value = ("OK", [None])
            with patch("waggle._imap_connect", return_value=(mock_imap, "test")):
                with pytest.raises(RuntimeError, match="not found"):
                    waggle.read_message("999", mark_read=mark_read, config=imap_config)


class TestCLIMarkReadFlag:
    """Verify --mark-read CLI flag passes through to read_message()."""

    def test_cli_mark_read_flag(self, mock_imap, imap_config, sample_email_bytes):
        """--mark-read flag passes mark_read=True to read_message()."""
        mock_imap.fetch.return_value = ("OK", [(b"1 (BODY[]", sample_email_bytes)])

        with patch("waggle._imap_connect", return_value=(mock_imap, "test")), \
             patch("waggle._build_cfg", return_value=imap_config), \
             patch("waggle.read_message", wraps=waggle.read_message) as wrapped:
            # Simulate CLI args
            import argparse
            args = argparse.Namespace(uid="1", folder="INBOX", mark_read=True)
            waggle._cli_read(args)
            wrapped.assert_called_once_with("1", folder="INBOX", mark_read=True)

    def test_cli_default_no_mark_read(self, mock_imap, imap_config, sample_email_bytes):
        """Default CLI (no --mark-read) passes mark_read=False."""
        mock_imap.fetch.return_value = ("OK", [(b"1 (BODY[]", sample_email_bytes)])

        with patch("waggle._imap_connect", return_value=(mock_imap, "test")), \
             patch("waggle._build_cfg", return_value=imap_config), \
             patch("waggle.read_message", wraps=waggle.read_message) as wrapped:
            import argparse
            args = argparse.Namespace(uid="1", folder="INBOX", mark_read=False)
            waggle._cli_read(args)
            wrapped.assert_called_once_with("1", folder="INBOX", mark_read=False)
