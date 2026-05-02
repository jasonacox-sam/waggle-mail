import pytest
from waggle import _md_to_html_simple


class TestFlynnScenario:
    """Document the trade-off: after-markdown unescape handles natural language
    entities but leaves R&amp;D as R&amp;D in HTML source (valid HTML, displays fine)."""

    def test_rd_in_text(self):
        """R&D becomes R&amp;D in HTML source — technically escaped, displays as R&D."""
        md = "R&D is a great department."
        html = _md_to_html_simple(md)
        assert "R&amp;D" in html
        assert "R&D" not in html  # bare & is invalid HTML

    def test_curly_quotes_from_entities(self):
        """&#8217; becomes actual apostrophe in HTML output."""
        md = "It&#8217;s working"
        html = _md_to_html_simple(md)
        assert "\u2019" in html  # right single quote
        assert "&#8217;" not in html

    def test_em_dash_from_entity(self):
        """&#8212; becomes actual em dash."""
        md = "Test&#8212;test"
        html = _md_to_html_simple(md)
        assert "\u2014" in html  # em dash
        assert "&#8212;" not in html
