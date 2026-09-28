"""Two presentation-honesty fixes, pinned.

1. ONE spec-decode card, not two.
   "SPEC DECODE ACCEPTANCE" (a gauge) and "SPEC DECODE ACCEPTANCE BY POSITION"
   (the per-draft-position bars) were separate cards reading the SAME value
   (SP.acceptance). With no per-position data the detail card printed the
   headline number as fallback text, so the two cards said the same thing twice
   and the gauge card was pure duplication. The gauge now lives INSIDE the
   detail card: one heading, one number, one breakdown under it.

2. Cloud SKU tiles are marked as estimates.
   The €/Mtok figures are OUR reference prices for comparison, not measured
   spend, but they rendered in the same tile style as measured readings. A
   tooltip is not enough -- the distinction has to be visible in the tile, so
   they get a dashed border and an "· est" marker on the label.
"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401

with open(os.path.join(ROOT, "metrics.html")) as _fh:
    HTML = _fh.read()


class TestOneSpecDecodeCard(unittest.TestCase):

    def test_single_spec_heading_in_markup(self):
        headings = re.findall(r"<h2>(?:<span[^>]*>)?(SPEC DECODE[^<]*)", HTML)
        self.assertEqual(len(headings), 1,
                         "one spec-decode card, got: %r" % headings)

    def test_no_by_position_heading_anywhere(self):
        self.assertNotIn("SPEC DECODE ACCEPTANCE BY POSITION", HTML)

    def test_gauge_lives_inside_the_spec_card(self):
        """The gauge is kept, not deleted -- it just is not its own card."""
        i = HTML.index('id="sp-gauge-wrap"')
        j = HTML.index('id="sp-wrap"')
        self.assertLess(i, j, "gauge renders above the per-position rows")
        # and both are inside the SAME card element
        card_start = HTML.rindex('<div class="card', 0, i)
        self.assertLess(card_start, i)
        self.assertNotIn('<div class="card', HTML[i:j],
                         "gauge and rows must not be split across cards")

    def test_title_is_set_to_one_name(self):
        titles = set(re.findall(r"setText\('sp-title','([^']*)'", HTML))
        self.assertEqual(titles, {"SPEC DECODE ACCEPTANCE"})

    def test_gauge_still_fed(self):
        """Deleting the card must not orphan the gauge renderer."""
        self.assertIn("$('g-spec-slot').innerHTML=gaugeShell('g-spec','ok');", HTML)
        self.assertIn("setArc('g-spec',sa,", HTML)

    def test_rows_still_sum_to_24(self):
        for m in re.finditer(r'<div class="row r24"[^>]*>(.*?)(?=<div class="row|\Z)',
                             HTML, re.S):
            spans = [int(x) for x in
                     re.findall(r'class="card s24-(\d+)"', m.group(1))]
            if spans:
                self.assertEqual(sum(spans), 24,
                                 "row sums to %d: %r" % (sum(spans), spans))


class TestSkuTilesMarkedAsEstimates(unittest.TestCase):

    def test_est_css_exists(self):
        self.assertIn(".tile.est{", HTML)
        self.assertIn("border-style:dashed", HTML)

    def test_est_marker_in_label(self):
        self.assertIn("\\00b7 est", HTML)

    def test_window_sku_tiles_get_the_class(self):
        self.assertIn("tile.classList.add('est');", HTML)

    def test_today_meter_tiles_get_the_class(self):
        self.assertIn("meter.children[mi].classList.add('est')", HTML)

    def test_only_sku_tiles_are_marked_not_measured_energy(self):
        """The same row holds DGX ENERGY, which IS measured.

        The first pass blanket-marked every child of the meter row, so real
        electricity read "· est" -- a fresh lie of exactly the kind the marker
        exists to prevent. The guard now requires a label test.
        """
        self.assertIn("CLOUD|MID|LOW|ULTRA", HTML)
        i = HTML.index("for(var mi=0;mi<meter.children.length;mi++)")
        seg = HTML[i:i+400]
        self.assertIn("querySelector('.tt')", seg,
                      "must inspect the label before marking")

    def test_measured_meters_are_not_marked(self):
        """ALL-TIME ENERGY/COST are real electricity -- they must stay plain."""
        i = HTML.index("id='row-at-meter'") if "id='row-at-meter'" in HTML \
            else HTML.index("row-at-meter")
        seg = HTML[i:i+1200]
        self.assertNotIn("classList.add('est')", seg,
                         "all-time electricity tiles are measured, not estimates")


class TestMutationsCaught(unittest.TestCase):

    def test_duplicate_heading_mutation(self):
        # Re-introduce the exact shape that was removed: a standalone
        # <h2>SPEC DECODE ACCEPTANCE ...</h2> card. An earlier version of this
        # mutation injected the heading in a shape the guard's regex never
        # matched, so the "mutation" passed and proved nothing.
        # Anchor on the id, not the class list: the gauge wrap gained a
        # "shared" modifier when it stopped claiming the full card height, and
        # a class-exact anchor silently stopped matching -- the mutation
        # became a no-op that still "passed".
        anchor = re.search(r'<div class="gwrap[^"]*" id="sp-gauge-wrap">', HTML)
        self.assertIsNotNone(anchor, "gauge wrap anchor must exist")
        mutated = HTML.replace(
            anchor.group(0),
            '</div></div><div class="card s24-4"><h2>SPEC DECODE ACCEPTANCE'
            ' <span class="info" data-tip="x">i</span></h2><div class="gwrap">', 1)
        self.assertNotEqual(mutated, HTML, "mutation must actually change the file")
        headings = re.findall(r"<h2>(?:<span[^>]*>)?(SPEC DECODE[^<]*)", mutated)
        self.assertEqual(len(headings), 2,
                         "mutation should recreate the duplicate card")
        self.assertNotEqual(len(headings), 1,
                            "a second spec heading must fail the guard")

    def test_est_class_removal_mutation(self):
        mutated = HTML.replace("tile.classList.add('est');", "")
        self.assertNotIn("tile.classList.add('est');", mutated)
        self.assertIn("tile.classList.add('est');", HTML,
                      "guard must be present in the real file")

    def test_row_overflow_mutation(self):
        mutated = HTML.replace('class="card s24-12"', 'class="card s24-20"', 1)
        bad = False
        for m in re.finditer(r'<div class="row r24"[^>]*>(.*?)(?=<div class="row|\Z)',
                             mutated, re.S):
            spans = [int(x) for x in
                     re.findall(r'class="card s24-(\d+)"', m.group(1))]
            if spans and sum(spans) != 24:
                bad = True
        self.assertTrue(bad, "an overflowing row must fail the 24-column guard")




    def test_spec_gauge_does_not_eat_the_card(self):
        """The merged spec card holds a gauge AND the per-position rows.

        .gwrap is height:calc(100% - 24px), which was written for a card that
        contains nothing else. Sharing the card, it claimed the whole height
        and pushed "no speculative decoding active in window" 24px BELOW the
        card border. Measured in the browser; guarded here.
        """
        with open(os.path.join(ROOT, "metrics.html")) as fh:
            h = fh.read()
        self.assertIn("sp-gauge-wrap", h)
        # it must be the .shared variant, not the plain full-height one
        self.assertIn('<div class="gwrap shared" id="sp-gauge-wrap">', h)
        self.assertNotIn('<div class="gwrap" id="sp-gauge-wrap">', h)
        # and the shared variant must be height:auto, not the calc() claim
        shared = h.split(".gwrap.shared{")[1].split("}")[0]
        self.assertIn("height:auto", shared)
        self.assertNotIn("calc(100% - 24px)", shared)
        # the sibling content must come after it, inside the same card
        self.assertLess(h.index("sp-gauge-wrap"), h.index('id="sp-wrap"'))


if __name__ == "__main__":
    unittest.main(verbosity=2)
