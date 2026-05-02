"""Tests for HTML entity handling in waggle — Clean Pipe v1.9.15."""

import pytest
from unittest.mock import patch, MagicMock
import email.message
import waggle


class TestHtmlEntityPreservation:
    """Verify entities pass through unmodified (clean pipe)."""

    def _make_raw_message(self, body_text, content_type="text/plain"):
        """Build a minimal raw RFC-822 bytes payload for _parse_message."""
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        if content_type == "text/html":
            msg = MIMEMultipart("alternative")
            msg["From"] = "sender@example.com"
            msg["To"] = "recipient@example.com"
            msg["Subject"] = "Test"
            msg["Message-Id"] = "<test123@example.com>"
            msg.attach(MIMEText(body_text, "html", "utf-8"))
        else:
            msg = email.message.EmailMessage()
            msg["From"] = "sender@example.com"
            msg["To"] = "recipient@example.com"
            msg["Subject"] = "Test"
            msg["Message-Id"] = "<test123@example.com>"
            msg.set_content(body_text, subtype="plain")
        return msg.as_bytes()

    def test_numeric_entities_preserved_in_plain_text(self):
        """&#160; and &#8217; remain as raw entities."""
        raw = self._make_raw_message("Hello&#160;World&#8217;s test", "text/plain")
        result = waggle._parse_message(raw)
        assert "&#160;" in result["body_plain"]
        assert "&#8217;" in result["body_plain"]
        # Verify NOT decoded to actual characters
        assert "\xa0" not in result["body_plain"]
        assert "\u2019" not in result["body_plain"]

    def test_numeric_entities_preserved_in_html_body(self):
        """&#8220; and &#8221; remain as raw entities in HTML part."""
        raw = self._make_raw_message('&#8220;Hello&#8221; &#8212; test&#8230;', "text/html")
        result = waggle._parse_message(raw)
        assert "&#8220;" in result["body_html"]
        assert "&#8221;" in result["body_html"]
        # Verify NOT decoded to actual characters
        assert "\u201c" not in result["body_html"]
        assert "\u201d" not in result["body_html"]

    def test_named_entities_preserved(self):
        """Named entities like &amp; &lt; &gt; remain as raw text."""
        raw = self._make_raw_message("A &amp; B &lt; C &gt; D", "text/plain")
        result = waggle._parse_message(raw)
        assert "&amp;" in result["body_plain"]
        assert "&lt;" in result["body_plain"]
        assert "&gt;" in result["body_plain"]
        # Verify NOT decoded: if decoded, we'd see bare "A & B < C > D"
        assert result["body_plain"] != "A & B < C > D\n"

    def test_plain_text_without_entities(self):
        """Plain text without entities passes through unchanged."""
        raw = self._make_raw_message("Just plain text.", "text/plain")
        result = waggle._parse_message(raw)
        assert result["body_plain"] == "Just plain text.\n"


class TestQuotedBodyEntityPreservation:
    """Tests that fetch_quoted_body preserves entities (clean pipe)."""

    def _make_raw_message(self, body_text, content_type="text/plain"):
        """Build a minimal raw RFC-822 bytes payload."""
        from email.mime.text import MIMEText
        msg = email.message.EmailMessage()
        msg["From"] = "sender@example.com"
        msg["To"] = "recipient@example.com"
        msg["Subject"] = "Test"
        msg["Message-Id"] = "<quoted123@example.com>"
        msg.set_content(body_text, subtype="plain" if content_type == "text/plain" else "html")
        return msg.as_bytes()

    def test_quoted_plain_preserves_numeric_entities(self):
        """fetch_quoted_body preserves &#8217; as raw entity."""
        raw = self._make_raw_message("It&#8217;s working", "text/plain")
        msg = email.message_from_bytes(raw)
        p = msg.get_payload(decode=True)
        decoded = p.decode(msg.get_content_charset() or "utf-8", errors="replace")
        assert "&#8217;" in decoded
        assert "\u2019" not in decoded  # NOT decoded

    def test_quoted_plain_preserves_named_entities(self):
        """fetch_quoted_body preserves &amp; as raw entity."""
        raw = self._make_raw_message("A &amp; B", "text/plain")
        msg = email.message_from_bytes(raw)
        p = msg.get_payload(decode=True)
        decoded = p.decode(msg.get_content_charset() or "utf-8", errors="replace")
        assert "&amp;" in decoded
        assert decoded != "A & B\n"  # NOT decoded to bare &
