"""Tests for skip_quote_fetch (decouples fetch_quoted_body() from threading headers
in send_email() — see issue #26). Callers that already have the original message
(e.g. fetched by UID) can skip the redundant Message-ID search while threading
headers (In-Reply-To/References) still get set."""
from unittest.mock import patch, MagicMock

_CFG = {
    "smtp_host": "localhost", "smtp_port": 587, "smtp_user": "u",
    "smtp_password": "p", "from_addr": "sam@example.com",
    "from_name": "Sam", "tls": False,
}


def test_skip_quote_fetch_true_does_not_call_fetch_quoted_body():
    import waggle
    with patch("waggle.fetch_quoted_body") as mock_fetch, \
         patch("smtplib.SMTP") as mock_smtp:
        mock_smtp.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
        try:
            waggle.send_email(
                to="recipient@example.com", subject="Re: hi", body_md="body",
                in_reply_to="<orig@example.com>", skip_quote_fetch=True,
                save_sent=False, config=_CFG,
            )
        except Exception:
            pass  # SMTP failure is fine — we're only asserting fetch_quoted_body wasn't called
        mock_fetch.assert_not_called()


def test_skip_quote_fetch_false_still_calls_fetch_quoted_body():
    """Default behavior (skip_quote_fetch=False) is unchanged."""
    import waggle
    with patch("waggle.fetch_quoted_body", return_value=(None, None)) as mock_fetch, \
         patch("smtplib.SMTP") as mock_smtp:
        mock_smtp.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
        try:
            waggle.send_email(
                to="recipient@example.com", subject="Re: hi", body_md="body",
                in_reply_to="<orig@example.com>",
                save_sent=False, config=_CFG,
            )
        except Exception:
            pass
        mock_fetch.assert_called_once()


def test_skip_quote_fetch_type_error_on_non_bool():
    import waggle
    import pytest
    with pytest.raises(TypeError, match="skip_quote_fetch must be bool"):
        waggle.send_email(
            to="recipient@example.com", subject="hi", body_md="body",
            skip_quote_fetch="yes", config=_CFG,
        )


def test_skip_quote_fetch_threading_headers_still_set():
    """skip_quote_fetch=True must not affect In-Reply-To/References — only the fetch."""
    import waggle
    with patch("waggle.fetch_quoted_body") as mock_fetch, \
         patch("smtplib.SMTP") as mock_smtp:
        server = MagicMock()
        mock_smtp.return_value.__enter__ = MagicMock(return_value=server)
        mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
        waggle.send_email(
            to="recipient@example.com", subject="Re: hi", body_md="body",
            in_reply_to="<orig@example.com>", skip_quote_fetch=True,
            save_sent=False, config=_CFG,
        )
        mock_fetch.assert_not_called()
        raw = server.sendmail.call_args[0][2]
        assert "In-Reply-To: <orig@example.com>" in raw


if __name__ == "__main__":
    import sys
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else print("\nall skip_quote_fetch tests passed") or 0)
