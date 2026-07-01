"""Tests for the `waggle check` subcommand and count_unread() (issue #4).

`waggle check` is cron-native: exit 0 if there are unread messages, exit 1 if
there are none, exit 2 on error. count_unread() backs it with a single
``UID SEARCH UNSEEN`` (no body fetches).

All 7 required categories are covered below, each labelled in a comment.
"""

import json

import pytest
from unittest.mock import patch

import waggle


# --- Security: input validation, injection, credential handling, boundaries ---
class TestSecurity:
    def test_folder_with_control_chars_rejected(self, mock_imap):
        # Injection guard: a folder name with CR/LF must raise before any IMAP op.
        with patch("waggle._imap_connect", return_value=(mock_imap, "imap.example.com")):
            with pytest.raises(ValueError):
                waggle.count_unread(folder="INBOX\r\nA001 DELETE")
        mock_imap.select.assert_not_called()

    def test_no_credentials_leaked_on_error(self, mock_imap, capsys):
        # The CLI error path must not print the password to stderr.
        cfg = {"imap_host": "h", "imap_port": 993, "imap_tls": True,
               "user": "u@example.com", "password": "s3cret-pw"}
        with patch("waggle._build_cfg", return_value=cfg), \
             patch("waggle._imap_connect", side_effect=RuntimeError("connect boom")):
            args = _Args(folder="INBOX", json=False)
            with pytest.raises(SystemExit):
                waggle._cli_check(args)
        err = capsys.readouterr().err
        assert "s3cret-pw" not in err


# --- Performance: bounded work, no unbounded loops, no body fetches ---
class TestPerformance:
    def test_single_search_no_body_fetch(self, mock_imap):
        mock_imap.uid.return_value = ("OK", [b"1 2 3"])
        with patch("waggle._imap_connect", return_value=(mock_imap, "imap.example.com")):
            waggle.count_unread(folder="INBOX")
        # Exactly one IMAP command, and it is a SEARCH — never a FETCH of bodies.
        mock_imap.uid.assert_called_once_with("SEARCH", None, "UNSEEN")
        assert mock_imap.select.call_count == 1


# --- Retry: external calls retried with backoff (N/A here — see note) ---
class TestRetry:
    # NOTE: waggle uses a fresh connection per operation by design (ROADMAP:
    # "Fresh connection per operation") and performs a single IMAP round-trip per
    # `check`; there is no retry/backoff layer to exercise. This placeholder
    # asserts the single-attempt contract so the category is not silently omitted.
    def test_single_attempt_no_retry(self, mock_imap):
        mock_imap.uid.return_value = ("OK", [b""])
        with patch("waggle._imap_connect", return_value=(mock_imap, "imap.example.com")) as conn:
            waggle.count_unread(folder="INBOX")
        assert conn.call_count == 1  # one connection attempt, no retries


# --- Unit: edge cases and error handling for count_unread() ---
class TestUnit:
    def test_empty_folder_returns_zero(self, mock_imap):
        mock_imap.uid.return_value = ("OK", [b""])
        with patch("waggle._imap_connect", return_value=(mock_imap, "imap.example.com")):
            assert waggle.count_unread() == 0

    def test_none_data_returns_zero(self, mock_imap):
        mock_imap.uid.return_value = ("OK", [None])
        with patch("waggle._imap_connect", return_value=(mock_imap, "imap.example.com")):
            assert waggle.count_unread() == 0

    def test_counts_unread_uids(self, mock_imap):
        mock_imap.uid.return_value = ("OK", [b"7 12 34 99"])
        with patch("waggle._imap_connect", return_value=(mock_imap, "imap.example.com")):
            assert waggle.count_unread() == 4

    def test_not_configured_raises(self):
        with patch("waggle._imap_connect", return_value=(None, None)):
            with pytest.raises(RuntimeError):
                waggle.count_unread()

    def test_select_failure_raises(self, mock_imap):
        mock_imap.select.return_value = ("NO", None)
        with patch("waggle._imap_connect", return_value=(mock_imap, "imap.example.com")):
            with pytest.raises(RuntimeError):
                waggle.count_unread(folder="Nope")

    def test_search_failure_raises(self, mock_imap):
        mock_imap.uid.return_value = ("NO", None)
        with patch("waggle._imap_connect", return_value=(mock_imap, "imap.example.com")):
            with pytest.raises(RuntimeError):
                waggle.count_unread()


# --- Integration: CLI handler wired to count_unread, exit codes end-to-end ---
class TestIntegration:
    def test_exit_zero_when_unread(self, capsys):
        with patch("waggle.count_unread", return_value=3):
            with pytest.raises(SystemExit) as exc:
                waggle._cli_check(_Args(folder="INBOX", json=False))
        assert exc.value.code == 0
        assert "3 unread" in capsys.readouterr().out

    def test_exit_one_when_none(self):
        with patch("waggle.count_unread", return_value=0):
            with pytest.raises(SystemExit) as exc:
                waggle._cli_check(_Args(folder="INBOX", json=False))
        assert exc.value.code == 1

    def test_exit_two_on_error(self, capsys):
        with patch("waggle.count_unread", side_effect=RuntimeError("imap down")):
            with pytest.raises(SystemExit) as exc:
                waggle._cli_check(_Args(folder="INBOX", json=False))
        assert exc.value.code == 2
        assert "error:" in capsys.readouterr().err

    def test_json_output(self, capsys):
        with patch("waggle.count_unread", return_value=2):
            with pytest.raises(SystemExit) as exc:
                waggle._cli_check(_Args(folder="INBOX", json=True))
        assert exc.value.code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload == {"folder": "INBOX", "unread": 2}


# --- Functional: golden + error path through the real count_unread() ---
class TestFunctional:
    def test_golden_unread_present(self, mock_imap):
        mock_imap.uid.return_value = ("OK", [b"1 2 3 4 5"])
        with patch("waggle._imap_connect", return_value=(mock_imap, "imap.example.com")):
            assert waggle.count_unread(folder="INBOX") == 5
        mock_imap.logout.assert_called_once()

    def test_error_path_propagates(self, mock_imap):
        mock_imap.uid.side_effect = OSError("network reset")
        with patch("waggle._imap_connect", return_value=(mock_imap, "imap.example.com")):
            with pytest.raises(OSError):
                waggle.count_unread(folder="INBOX")


# --- Frame: smoke import / wiring sanity ---
class TestFrame:
    def test_symbols_present(self):
        assert callable(waggle.count_unread)
        assert callable(waggle._cli_check)

    def test_check_registered_in_help(self, capsys):
        # argparse builds the `check` subcommand and its help exits cleanly.
        with patch("sys.argv", ["waggle", "check", "--help"]):
            with pytest.raises(SystemExit) as exc:
                waggle.main()
        assert exc.value.code == 0
        assert "check" in capsys.readouterr().out.lower()


class _Args:
    """Lightweight argparse.Namespace stand-in for CLI handler tests."""

    def __init__(self, **kw):
        self.__dict__.update(kw)
