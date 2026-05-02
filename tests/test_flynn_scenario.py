"""Document the Flynn scenario trade-off for v1.9.15 Clean Pipe."""

from waggle import _md_to_html_simple


class TestFlynnScenario:
    """Document that entities are preserved in markdown rendering (clean pipe)."""

    def test_rd_in_text(self):
        """R&D passes through — markdown escapes & to &amp; (normal behavior)."""
        md = "R&D is a great department."
        html = _md_to_html_simple(md)
        assert "R&amp;D" in html  # markdown escapes & to &amp;
        assert "R&D" not in html  # bare & is invalid HTML

    def test_entities_preserved_in_markdown(self):
        """&#8217; gets escaped by markdown renderer to &amp;#8217; — not decoded."""
        md = "It&#8217;s working"
        html = _md_to_html_simple(md)
        assert "&amp;#8217;" in html  # markdown escapes & to &amp;
        assert "\u2019" not in html  # NOT decoded to real apostrophe

    def test_em_dash_preserved(self):
        """&#8212; gets escaped by markdown renderer — not decoded."""
        md = "Test&#8212;test"
        html = _md_to_html_simple(md)
        assert "&amp;#8212;" in html  # markdown escapes & to &amp;
        assert "\u2014" not in html  # NOT decoded to real em dash
