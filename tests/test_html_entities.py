"""Tests for HTML entity decoding in email body display."""

import pytest
from unittest.mock import patch, MagicMock
import email.message
import waggle


class TestHtmlEntityDecoding:
    """Tests that numeric and named HTML entities are decoded in body text."""

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

    def test_numeric_entities_decoded_in_plain_text(self):
        """&#160; → non-breaking space, &#8217; → right single quote."""
        raw = self._make_raw_message("Hello&#160;World&#8217;s test", "text/plain")
        result = waggle._parse_message(raw)
        assert "\xa0" in result["body_plain"]          # non-breaking space
        assert "\u2019" in result["body_plain"]         # right single quote
        assert "&#160;" not in result["body_plain"]
        assert "&#8217;" not in result["body_plain"]

    def test_numeric_entities_decoded_in_html_body(self):
        """&#8220; and &#8221; → curly quotes in HTML part."""
        raw = self._make_raw_message('&#8220;Hello&#8221; &#8212; test&#8230;', "text/html")
        result = waggle._parse_message(raw)
        assert "\u201c" in result["body_html"]          # left double quote
        assert "\u201d" in result["body_html"]          # right double quote
        assert "\u2014" in result["body_html"]          # em dash
        assert "\u2026" in result["body_html"]          # ellipsis
        assert "&#8220;" not in result["body_html"]

    def test_named_entities_decoded(self):
        """Named entities like &amp; &lt; &gt; are decoded."""
        raw = self._make_raw_message("A &amp; B &lt; C &gt; D", "text/plain")
        result = waggle._parse_message(raw)
        assert "&" in result["body_plain"]
        assert "<" in result["body_plain"]
        assert ">" in result["body_plain"]
        assert "&amp;" not in result["body_plain"]
        assert "&lt;" not in result["body_plain"]

    def test_no_entities_unchanged(self):
        """Plain text without entities passes through unchanged."""
        raw = self._make_raw_message("Just plain text.", "text/plain")
        result = waggle._parse_message(raw)
        assert result["body_plain"] == "Just plain text.\n"


class TestHtmlEntityDecodingQuotedBody:
    """Tests that html.unescape() works in fetch_quoted_body()."""

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

    def test_quoted_plain_decodes_numeric_entities(self):
        """fetch_quoted_body plain text decodes &#8217; → right single quote."""
        raw = self._make_raw_message("It&#8217;s working", "text/plain")
        # Mock _parse_message-like extraction manually
        import email
        msg = email.message_from_bytes(raw)
        p = msg.get_payload(decode=True)
        decoded = p.decode(msg.get_content_charset() or "utf-8", errors="replace")
        import html
        result = html.unescape(decoded)
        assert "’" in result          # right single quote
        assert "&#8217;" not in result

    def test_quoted_plain_decodes_named_entities(self):
        """fetch_quoted_body plain text decodes &amp; → &."""
        raw = self._make_raw_message("A &amp; B", "text/plain")
        import email
        msg = email.message_from_bytes(raw)
        p = msg.get_payload(decode=True)
        decoded = p.decode(msg.get_content_charset() or "utf-8", errors="replace")
        import html
        result = html.unescape(decoded)
        assert "&" in result
        assert "&amp;" not in result
