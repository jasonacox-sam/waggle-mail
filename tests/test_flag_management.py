"""Tests for IMAP flag management functions."""

import pytest
from unittest.mock import patch, MagicMock
import waggle


class TestSetFlags:
    """Tests for set_flags() function."""

    def test_set_single_flag(self, mock_imap, mock_config):
        """Set a single flag on one UID."""
        with patch("waggle._imap_connect", return_value=(mock_imap, "imap.example.com")):
            result = waggle.set_flags("42", [r"\Seen"], folder="INBOX", config=mock_config)

        assert result is True
        mock_imap.select.assert_called_once_with("INBOX")
        mock_imap.uid.assert_called_once_with("STORE", b"42", "+FLAGS", r"(\Seen)")
        mock_imap.logout.assert_called_once()

    def test_set_multiple_flags(self, mock_imap, mock_config):
        """Set multiple flags at once."""
        with patch("waggle._imap_connect", return_value=(mock_imap, "imap.example.com")):
            result = waggle.set_flags("42", [r"\Seen", r"\Flagged"], folder="INBOX", config=mock_config)

        assert result is True
        mock_imap.uid.assert_called_once_with("STORE", b"42", "+FLAGS", r"(\Seen \Flagged)")

    def test_bulk_uids(self, mock_imap, mock_config):
        """Set flags on multiple UIDs (comma-separated)."""
        with patch("waggle._imap_connect", return_value=(mock_imap, "imap.example.com")):
            result = waggle.set_flags("42,43,44", [r"\Seen"], folder="INBOX", config=mock_config)

        assert result is True
        assert mock_imap.uid.call_count == 3
        calls = mock_imap.uid.call_args_list
        assert calls[0][0] == ("STORE", b"42", "+FLAGS", r"(\Seen)")
        assert calls[1][0] == ("STORE", b"43", "+FLAGS", r"(\Seen)")
        assert calls[2][0] == ("STORE", b"44", "+FLAGS", r"(\Seen)")


class TestClearFlags:
    """Tests for clear_flags() function."""

    def test_clear_single_flag(self, mock_imap, mock_config):
        """Clear a single flag."""
        with patch("waggle._imap_connect", return_value=(mock_imap, "imap.example.com")):
            result = waggle.clear_flags("42", [r"\Seen"], folder="INBOX", config=mock_config)

        assert result is True
        mock_imap.uid.assert_called_once_with("STORE", b"42", "-FLAGS", r"(\Seen)")

    def test_clear_multiple_flags(self, mock_imap, mock_config):
        """Clear multiple flags at once."""
        with patch("waggle._imap_connect", return_value=(mock_imap, "imap.example.com")):
            result = waggle.clear_flags("42", [r"\Seen", r"\Flagged"], folder="INBOX", config=mock_config)

        assert result is True
        mock_imap.uid.assert_called_once_with("STORE", b"42", "-FLAGS", r"(\Seen \Flagged)")


class TestValidation:
    """Tests for input validation."""

    def test_invalid_flag_rejected(self, mock_imap, mock_config):
        """Unknown flag raises ValueError."""
        with patch("waggle._imap_connect", return_value=(mock_imap, "imap.example.com")):
            with pytest.raises(ValueError, match="Unknown IMAP flag"):
                waggle.set_flags("42", [r"\CustomFlag"], config=mock_config)

        # Should not call IMAP at all
        mock_imap.select.assert_not_called()

    def test_empty_flags_rejected(self, mock_imap, mock_config):
        """Empty flags list raises ValueError."""
        with patch("waggle._imap_connect", return_value=(mock_imap, "imap.example.com")):
            with pytest.raises(ValueError, match="flags list cannot be empty"):
                waggle.set_flags("42", [], config=mock_config)

    def test_invalid_uid_rejected(self, mock_imap, mock_config):
        """Invalid UID raises ValueError."""
        with patch("waggle._imap_connect", return_value=(mock_imap, "imap.example.com")):
            # Non-numeric
            with pytest.raises(ValueError, match="Invalid UID"):
                waggle.set_flags("abc", [r"\Seen"], config=mock_config)

            # Zero
            with pytest.raises(ValueError, match="Invalid UID"):
                waggle.set_flags("0", [r"\Seen"], config=mock_config)

            # Negative
            with pytest.raises(ValueError, match="Invalid UID"):
                waggle.set_flags("-5", [r"\Seen"], config=mock_config)

    def test_folder_validation(self, mock_imap, mock_config):
        """Control characters in folder name are rejected."""
        with patch("waggle._imap_connect", return_value=(mock_imap, "imap.example.com")):
            with pytest.raises(ValueError, match="Folder name contains control characters"):
                waggle.set_flags("42", [r"\Seen"], folder="INBOX\r\n", config=mock_config)


class TestErrorHandling:
    """Tests for error handling."""

    def test_imap_not_configured(self, mock_config):
        """Missing IMAP host raises RuntimeError."""
        with patch("waggle._imap_connect", return_value=(None, None)):
            with pytest.raises(RuntimeError, match="IMAP not configured"):
                waggle.set_flags("42", [r"\Seen"], config={})

    def test_store_failure(self, mock_imap, mock_config):
        """IMAP STORE command failure raises RuntimeError."""
        mock_imap.uid.return_value = ("NO", None)

        with patch("waggle._imap_connect", return_value=(mock_imap, "imap.example.com")):
            with pytest.raises(RuntimeError, match="set_flags failed for UID 42"):
                waggle.set_flags("42", [r"\Seen"], config=mock_config)

    def test_bulk_operation_fail_fast(self, mock_imap, mock_config):
        """Bulk operation stops at first failure."""
        # First call succeeds, second fails, third should not be called
        mock_imap.uid.side_effect = [
            ("OK", None),  # UID 42 succeeds
            ("NO", None),  # UID 43 fails
        ]

        with patch("waggle._imap_connect", return_value=(mock_imap, "imap.example.com")):
            with pytest.raises(RuntimeError, match="set_flags failed for UID 43"):
                waggle.set_flags("42,43,44", [r"\Seen"], config=mock_config)

        # Should only call uid twice (42 success, 43 failure, 44 not attempted)
        assert mock_imap.uid.call_count == 2
