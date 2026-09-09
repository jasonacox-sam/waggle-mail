"""Issue #37 — send_email() must assert and return a Message-ID."""
import unittest
from unittest.mock import patch, MagicMock
from email import message_from_string

import waggle


def _make_cfg():
    return {
        "host": "smtp.example.com", "port": 465, "user": "agent@example.com",
        "password": "pw", "from_addr": "agent@example.com", "from_name": "Agent",
        "tls": True, "imap_host": None,
    }


class _FakeSMTP:
    def __init__(self, *a, **k):
        self.sent = None
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def login(self, *a):
        pass
    def sendmail(self, envelope_from, envelope_to, data):
        self.sent = data
        return {}


class TestMessageId(unittest.TestCase):
    @patch("waggle._build_cfg", return_value=_make_cfg())
    @patch("waggle.smtplib.SMTP_SSL", _FakeSMTP)
    @patch("waggle._log_sent")
    def test_send_email_mints_and_returns_message_id(self, *_):
        result = waggle.send_email("to@example.com", "hi", "body")
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("<") and result.endswith(">"))
        self.assertIn("@example.com", result)
        # The asserted id must appear in the actual message on the wire
        msg = message_from_string(_FakeSMTP.sent_data)
        self.assertEqual(msg["Message-ID"], result)

    @patch("waggle._build_cfg", return_value=_make_cfg())
    @patch("waggle.smtplib.SMTP_SSL", _FakeSMTP)
    @patch("waggle._log_sent")
    def test_send_email_honors_caller_supplied_message_id(self, *_):
        custom = "<my-custom-id@example.com>"
        result = waggle.send_email("to@example.com", "hi", "body", message_id=custom)
        self.assertEqual(result, custom)
        msg = message_from_string(_FakeSMTP.sent_data)
        self.assertEqual(msg["Message-ID"], custom)


_FakeSMTP.sent_data = None
_FakeSMTP.sent_id = None
_orig_sendmail = _FakeSMTP.sendmail


def _capture(self, envelope_from, envelope_to, data):
    _FakeSMTP.sent_data = data
    _FakeSMTP.sent_id = message_from_string(data)["Message-ID"]
    return {}


_FakeSMTP.sendmail = _capture


if __name__ == "__main__":
    unittest.main()
