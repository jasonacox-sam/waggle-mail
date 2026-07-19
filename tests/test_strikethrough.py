"""Tests for ~~strikethrough~~ rendering in the simple (fallback) renderer.

Before v1.9.22, ``_md_to_html_simple`` handled bold, italic, and inline code
but had no rule for ``~~strikethrough~~``. The tildes passed through as
literal text instead of rendering as ``<del>`` elements.
"""

import waggle


class TestStrikethrough:
    """_md_to_html_simple should render ~~text~~ as <del>text</del>."""

    def test_basic_strikethrough(self):
        html = waggle._md_to_html_simple("This is ~~struck~~ text.")
        assert "<del>struck</del>" in html
        assert "~~" not in html

    def test_strikethrough_at_start(self):
        html = waggle._md_to_html_simple("~~removed~~ from the beginning")
        assert "<del>removed</del>" in html

    def test_strikethrough_at_end(self):
        html = waggle._md_to_html_simple("at the end ~~gone~~")
        assert "<del>gone</del>" in html

    def test_multiple_strikethroughs(self):
        html = waggle._md_to_html_simple("~~old~~ and ~~older~~")
        assert "<del>old</del>" in html
        assert "<del>older</del>" in html

    def test_no_false_positive_single_tilde(self):
        # Single tildes should NOT trigger strikethrough.
        html = waggle._md_to_html_simple("~approximate~ value")
        assert "<del>" not in html
        assert "~approximate~" in html

    def test_strikethrough_with_other_formatting(self):
        html = waggle._md_to_html_simple("~~**bold strike**~~")
        assert "<del>" in html
        assert "<strong>bold strike</strong>" in html
