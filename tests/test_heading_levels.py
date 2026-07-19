"""Tests for full h1-h6 ATX heading rendering in the simple (fallback) renderer.

Before this, ``_md_to_html_simple`` (the renderer used when the optional
``markdown`` library is unavailable) only handled ``#``/``##``/``###``. Deeper
levels (``####``..``######``) fell through ``_escape_html`` and rendered as
literal ``#### Text`` in the HTML body.
"""
import re

import waggle


class TestHeadingLevels:
    """_md_to_html_simple should render all six ATX heading levels."""

    def test_all_six_levels_render(self):
        md = "# H1\n## H2\n### H3\n#### H4\n##### H5\n###### H6"
        html = waggle._md_to_html_simple(md)
        for n in range(1, 7):
            assert f"<h{n} " in html, f"h{n} not rendered"
            assert f"</h{n}>" in html, f"h{n} not closed"

    def test_no_literal_hashes_leak(self):
        # The regression this fixes: deep headings used to survive as literal text.
        html = waggle._md_to_html_simple("#### Sub\n##### Deeper\n###### Deepest")
        assert "####" not in html
        assert "<h4 " in html and "<h5 " in html and "<h6 " in html

    def test_sizes_strictly_descending(self):
        md = "\n".join(f"{'#' * n} H{n}" for n in range(1, 7))
        html = waggle._md_to_html_simple(md)
        sizes = [int(re.search(rf"<h{n}[^>]*font-size:(\d+)pt", html).group(1))
                 for n in range(1, 7)]
        assert sizes == sorted(sizes, reverse=True), sizes

    def test_h1_h3_sizes_unchanged(self):
        # Existing behaviour must be preserved.
        html = waggle._md_to_html_simple("# A\n## B\n### C")
        assert "font-size:20pt" in html  # h1
        assert "font-size:16pt" in html  # h2
        assert "font-size:14pt" in html  # h3

    def test_consistent_font_family_across_levels(self):
        html = waggle._md_to_html_simple("\n".join(f"{'#' * n} H" for n in range(1, 7)))
        assert html.count("font-family:Aptos,Calibri,Arial,sans-serif") >= 6

    def test_hash_without_space_not_a_heading(self):
        # "#nohash" / "####" alone are not ATX headings; leave them be.
        html = waggle._md_to_html_simple("#notaheading")
        assert "<h1" not in html
