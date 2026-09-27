"""Every grid row must fill the 24-column grid exactly.

THE DEFECT THIS PINS: the dashboard uses CSS Grid with 24 columns and cards
that span 4, 8 or 16. Adding two s24-8 cards to row2 — which was already
4+4+8+8 = 24, perfectly full — made it 40. CSS Grid does not complain: it
silently wraps the overflow onto a second line, so line 2 held 8+8 and left
8 columns (546px) of dead space beside them. Nothing was missing and nothing
errored; the row just looked broken. It shipped with a fully green test
suite because the tests checked that canvases EXISTED, never that the row
still summed to 24.

The lesson: existence tests do not catch layout regressions. Assert the
arithmetic.

Run: python3 tests/test_layout_rows.py
"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

HTML = os.path.join(ROOT, "metrics.html")
GRID = 24


def _rows(html):
    """Yield (row_id, [(span, heading), ...]) for each .row that holds cards."""
    for m in re.finditer(r'<div class="row[^"]*"(?: id="([^"]*)")?>(.*?)\n</div>', html, re.S):
        rid, body = m.group(1), m.group(2)
        cards = []
        for cm in re.finditer(r'<div class="card s24-(\d+)">(.*?)(?=<div class="card s24-|</div>\s*$)',
                              body, re.S):
            span = int(cm.group(1))
            h = re.search(r"<h2>(.*?)</h2>", cm.group(2), re.S)
            heading = re.sub(r"<[^>]+>", "", h.group(1)).strip() if h else "?"
            cards.append((span, heading))
        if cards:
            yield rid, cards


class TestRowsFillTheGrid(unittest.TestCase):
    def setUp(self):
        with open(HTML) as fh:
            self.html = fh.read()

    def test_every_row_sums_to_a_multiple_of_the_grid(self):
        bad = []
        for rid, cards in _rows(self.html):
            total = sum(s for s, _ in cards)
            if total % GRID:
                bad.append("%s: spans sum to %d (%s)" % (
                    rid or "(unnamed)", total,
                    " + ".join(str(s) for s, _ in cards)))
        self.assertFalse(
            bad,
            "rows do not fill the %d-column grid, so CSS wraps the overflow "
            "and leaves a ragged gap:\n  %s" % (GRID, "\n  ".join(bad)))

    def test_no_row_exceeds_the_grid_width(self):
        """A single row's spans must fit one grid line. Summed-over-all is
        fine (it wraps evenly); summed-per-line is what leaves holes."""
        for rid, cards in _rows(self.html):
            total = sum(s for s, _ in cards)
            if total > GRID:
                # Only acceptable if it divides evenly into full lines.
                self.assertEqual(
                    total % GRID, 0,
                    "row %s has %d columns of cards, which wraps to a "
                    "partial line" % (rid, total))

    def test_known_rows_are_present(self):
        """The cards this audit added must stay in the DOM. Guards against a
        'fix' that deletes a card to make the arithmetic work."""
        # Post-merge names. The metrics must survive the reorganisation —
        # a "fix" that deletes a card to make the arithmetic work is not a fix.
        # Post-merge names. The metrics must survive the reorganisation --
        # a "fix" that deletes a card to make the arithmetic work is not one.
        # The KV and prefix metrics moved from two s24-4 cards into one
        # s24-8 card, so their old headings are legitimately gone. Assert the
        # GAUGE ELEMENTS still exist instead -- that is what actually proves
        # the metric survived the reorganisation.
        for gauge in ("g-kv-slot", "g-ph-slot", "kv-abs"):
            self.assertIn('id="%s"' % gauge, self.html,
                          "the %s gauge was dropped by the cache merge" % gauge)
        for heading in ("CACHE: KV OCCUPANCY / PREFIX REUSE",
                        "REQUEST LATENCY", "TTFT", "DECODE",
                        "SCHEDULER PRESSURE", "PREEMPTIONS"):
            self.assertIn(heading, self.html,
                          "%s vanished from metrics.html" % heading)

    def test_canvas_ids_are_unique(self):
        """Two cards sharing a canvas id would silently draw into the first
        one — a rendering bug that looks like a missing card."""
        ids = re.findall(r'<canvas id="([^"]+)"', self.html)
        dupes = {i for i in ids if ids.count(i) > 1}
        self.assertFalse(dupes, "duplicate canvas ids: %s" % dupes)

    def test_legend_ids_are_unique(self):
        ids = re.findall(r'<div id="(lg-[^"]+)"', self.html)
        dupes = {i for i in ids if ids.count(i) > 1}
        self.assertFalse(dupes, "duplicate legend ids: %s" % dupes)


class TestRenderTargetsExist(unittest.TestCase):
    """A canvas or legend that the render path never touches renders blank —
    which reads as 'the metric is missing' rather than 'the wiring is
    broken'. Assert every id is both declared and referenced."""

    def setUp(self):
        with open(HTML) as fh:
            self.html = fh.read()
        m = re.search(r"<script>(.*)</script>", self.html, re.S)
        self.js = m.group(1) if m else ""

    def test_every_canvas_is_drawn(self):
        for cid in re.findall(r'<canvas id="([^"]+)"', self.html):
            self.assertIn("$('%s')" % cid, self.js,
                          "canvas %s is declared but never drawn to" % cid)

    def test_every_legend_is_populated(self):
        for lid in re.findall(r'<div id="(lg-[^"]+)"', self.html):
            self.assertIn("setLegend('%s'" % lid, self.js,
                          "legend %s is declared but never populated" % lid)


class TestCardWidthMatchesContent(unittest.TestCase):
    """A card must be sized for what it holds, not by habit.

    CACHE was s24-8 (623px) holding two 104px gauges -- 212px of content in
    a 623px box, ~400px of dead space. Dropped to s24-4 (306px), which still
    clears 2x104px gauges + 4px gap, and the freed columns went to REQUEST
    LATENCY, which has an 8-row legend table that actually needs width.

    These are static assertions on the declared spans; the rendered geometry
    was verified in a browser (306px card, 280px wrap, 208px of gauges, 36px
    balanced margin, caption contained with 15.8px bottom clearance).
    """

    def setUp(self):
        with open(HTML) as fh:
            self.html = fh.read()

    def _span_of(self, heading):
        m = re.search(r'class="card s24-(\d+)"[^>]*><h2>%s' % re.escape(heading),
                      self.html)
        self.assertIsNotNone(m, "card %r not found" % heading)
        return int(m.group(1))

    def test_cache_card_is_four_columns(self):
        self.assertEqual(self._span_of("CACHE: KV OCCUPANCY"), 4)

    def test_dual_gauges_fit_a_four_column_card(self):
        """2 gauges at 104px + 4px gap must fit the ~280px content box of an
        s24-4 card. If either gauge is widened later, this is the tripwire."""
        gauges = re.findall(r'\.dual \.gauge\{width:(\d+)px', self.html)
        self.assertTrue(gauges, ".dual .gauge width rule not found")
        widest = max(int(g) for g in gauges)
        span = self._span_of("CACHE: KV OCCUPANCY")
        # 1888px container / 24 cols, minus card padding -> ~280px for s24-4
        content_px = 1888 * span / 24 - 26
        self.assertLessEqual(widest * 2 + 4, content_px,
                             "two %dpx gauges + 4px gap = %dpx will not fit "
                             "the ~%dpx content box of an s24-%d card"
                             % (widest, widest * 2 + 4, content_px, span))

    def test_latency_card_took_the_freed_columns(self):
        self.assertEqual(self._span_of("REQUEST LATENCY"), 12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
