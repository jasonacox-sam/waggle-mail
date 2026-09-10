"""Issue #39 — send_email(return_receipt=True) returns the SMTP accept line.

The locally-minted Message-ID is an *assertion* (sender-claimed); the final
250 accept line is a *receipt* (transport-observed, provider-attested). Both
should be obtainable from one call. Default behavior must stay unchanged.
"""
import unittest
from unittest.mock import patch

import waggle


def _make_cfg():
    return {
        "host": "smtp.example.com", "port": 465, "user": "agent@example.com",
        "password": "pw", "from_addr": "agent@example.com", "from_name": "Agent",
        "tls": True, "imap_host": None,
    }


SES_LINE = (
    "010101a087de6661-5f2e6b8a-0000-0000-0000-000000000000@amazonses.com Ok"
)


class _FakeReceiptSMTP:
    """Stands in for waggle._ReceiptSMTP_SSL."""
    instances = []

    def __init__(self, *a, **k):
        self.last_data_reply = None
        _FakeReceiptSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, *a):
        pass

    def sendmail(self, envelope_from, envelope_to, data):
        self.last_data_reply = (250, SES_LINE)
        return {}


class TestReturnReceipt(unittest.TestCase):
    def setUp(self):
        _FakeReceiptSMTP.instances = []

    @patch("waggle._build_cfg", return_value=_make_cfg())
    @patch("waggle._ReceiptSMTP_SSL", _FakeReceiptSMTP)
    @patch("waggle._log_sent")
    def test_receipt_carries_minted_id_and_accept_line(self, *_):
        r = waggle.send_email("to@example.com", "hi", "body", return_receipt=True)
        self.assertIsInstance(r, waggle.SendReceipt)
        # message_id first — tuple-unpacking callers keep working
        mid, code, resp = r
        self.assertTrue(mid.startswith("<") and mid.endswith(">"))
        self.assertEqual(code, 250)
        self.assertIn("010101a087de6661", resp)

    @patch("waggle._build_cfg", return_value=_make_cfg())
    @patch("waggle._ReceiptSMTP_SSL", _FakeReceiptSMTP)
    @patch("waggle._log_sent")
    def test_provider_id_parsed_from_ses_line(self, *_):
        r = waggle.send_email("to@example.com", "hi", "body", return_receipt=True)
        self.assertIn("amazonses.com", r.provider_id)
        self.assertIn("010101a087de6661", r.provider_id)

    @patch("waggle._build_cfg", return_value=_make_cfg())
    @patch("waggle._ReceiptSMTP_SSL", _FakeReceiptSMTP)
    @patch("waggle._log_sent")
    def test_default_return_unchanged(self, *_):
        # When return_receipt=False waggle uses plain smtplib.SMTP_SSL, not the
        # receipt subclass — patch that too so no real connection is attempted.
        with patch("waggle.smtplib.SMTP_SSL", _FakeReceiptSMTP):
            result = waggle.send_email("to@example.com", "hi", "body")
        self.assertIsInstance(result, str)


class TestProviderIdParsing(unittest.TestCase):
    def test_angle_brackets(self):
        resp = "250 2.0.0 Ok: queued as 12345 <abc@mail.example.com>"
        self.assertEqual(
            waggle._provider_id_from_response(resp), "abc@mail.example.com"
        )

    def test_ses_bare_id(self):
        self.assertIn("amazonses.com", waggle._provider_id_from_response(SES_LINE))

    def test_no_id(self):
        self.assertIsNone(waggle._provider_id_from_response("250 Ok"))

    def test_empty(self):
        self.assertIsNone(waggle._provider_id_from_response(None))


if __name__ == "__main__":
    unittest.main()
