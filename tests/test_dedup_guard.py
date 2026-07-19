"""Tests for send-side dedup guard (check_recently_sent wired into send_email)."""
import time
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


def test_check_recently_sent_no_log(tmp_path, monkeypatch):
    """Returns False (safe to send) when no log file exists."""
    import waggle
    monkeypatch.setattr(waggle, "_SEND_LOG", tmp_path / "sent.log")
    assert waggle.check_recently_sent("a@b.com", "Hello") is False


def test_check_recently_sent_fresh_entry(tmp_path, monkeypatch):
    """Returns True when a matching entry is within the window."""
    import waggle
    log = tmp_path / "sent.log"
    monkeypatch.setattr(waggle, "_SEND_LOG", log)
    waggle._log_sent("a@b.com", "Hello")
    assert waggle.check_recently_sent("a@b.com", "Hello", within_minutes=5) is True


def test_check_recently_sent_expired_entry(tmp_path, monkeypatch):
    """Returns False when the matching entry is outside the window."""
    import waggle
    log = tmp_path / "sent.log"
    monkeypatch.setattr(waggle, "_SEND_LOG", log)
    # Write an entry timestamped 10 minutes ago
    old_ts = int(time.time()) - 600
    log.write_text(f"{old_ts}|a@b.com|hello\n")
    assert waggle.check_recently_sent("a@b.com", "Hello", within_minutes=5) is False


def test_send_email_raises_on_duplicate(tmp_path, monkeypatch):
    """send_email() raises DuplicateSendError when dedup_minutes > 0 and duplicate found."""
    import waggle
    monkeypatch.setattr(waggle, "_SEND_LOG", tmp_path / "sent.log")
    # Seed the log with a recent send
    waggle._log_sent("recipient@example.com", "Test subject")

    with pytest.raises(waggle.DuplicateSendError, match="Duplicate send suppressed"):
        waggle.send_email(
            to="recipient@example.com",
            subject="Test subject",
            body_md="body",
            dedup_minutes=5,
            config={
                "smtp_host": "localhost", "smtp_port": 587, "smtp_user": "u",
                "smtp_password": "p", "from_addr": "sam@example.com",
                "from_name": "Sam",
            },
        )


def test_send_email_dedup_disabled_by_default(tmp_path, monkeypatch):
    """send_email() does NOT raise when dedup_minutes=0 (default), even with a log entry."""
    import waggle
    monkeypatch.setattr(waggle, "_SEND_LOG", tmp_path / "sent.log")
    waggle._log_sent("recipient@example.com", "Test subject")

    # Should NOT raise — dedup is disabled by default
    # We mock SMTP so the actual send doesn't fire
    with patch("smtplib.SMTP") as mock_smtp:
        mock_smtp.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
        try:
            waggle.send_email(
                to="recipient@example.com",
                subject="Test subject",
                body_md="body",
                save_sent=False,
                config={
                    "smtp_host": "localhost", "smtp_port": 587, "smtp_user": "u",
                    "smtp_password": "p", "from_addr": "sam@example.com",
                    "from_name": "Sam", "tls": False,
                },
            )
        except waggle.DuplicateSendError:
            pytest.fail("DuplicateSendError raised with dedup_minutes=0 (should be disabled)")
        except Exception:
            pass  # SMTP failure is fine — we just need no DuplicateSendError
