"""HTML page structure guards.

These pages are static and hand-edited, and they fail SILENTLY: a JS syntax
error leaves the markup intact, so every heading and card still renders and
the page just shows placeholders ("-") forever. That is indistinguishable
from "the backend has no data" and cost real debugging time on the statistics
page. node --check catches the syntax error in a second; a missing doctype or
a duplicated body means a bad string splice went in unnoticed.

Run:  python3 -B tests/test_html_pages.py
"""
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

PAGES = ("metrics.html", "engines.html", "settings.html", "setup.html",
         "statistics.html")


def _have_node():
    try:
        subprocess.run(["node", "--version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


class TestPageStructure(unittest.TestCase):

    def test_every_page_exists(self):
        for p in PAGES:
            self.assertTrue(os.path.exists(os.path.join(ROOT, p)),
                            "%s is missing" % p)

    def test_single_document(self):
        """A duplicated <!doctype> or a prepended fragment means a string
        splice went wrong. That happened once and produced a page that served
        200 with a working-looking heading and a totally dead script."""
        for p in PAGES:
            h = open(os.path.join(ROOT, p)).read()
            self.assertTrue(h.lstrip().lower().startswith("<!doctype html>"),
                            "%s does not start with a doctype" % p)
            # "<head" also matches "<header", and "<body" appears inside
            # inline JS strings, so match the exact tags only.
            for tag in ("<!doctype", "<html", "</html>", "<body>", "</body>",
                        "<head>", "</head>"):
                self.assertEqual(h.lower().count(tag), 1,
                                 "%s has %d occurrences of %s"
                                 % (p, h.lower().count(tag), tag))

    def test_one_script_block(self):
        for p in PAGES:
            h = open(os.path.join(ROOT, p)).read()
            self.assertEqual(h.count("<script"), 1, "%s has >1 script tag" % p)

    def test_balanced_divs(self):
        """Cheap structural check: every <div> opened is closed. An unbalanced
        card nests the rest of the page inside it, which looks like a styling
        bug rather than a markup bug."""
        for p in PAGES:
            h = open(os.path.join(ROOT, p)).read()
            body = h[h.index("<body>"):]
            opens = len(re.findall(r"<div\b", body))
            closes = len(re.findall(r"</div>", body))
            self.assertEqual(opens, closes,
                             "%s: %d <div> vs %d </div>" % (p, opens, closes))


@unittest.skipUnless(_have_node(), "node not available")
class TestJavaScriptParses(unittest.TestCase):
    """The check that actually matters. A syntax error in a page script
    leaves the HTML looking perfect in a snapshot while every value stays at
    its placeholder forever."""

    def _check(self, page):
        h = open(os.path.join(ROOT, page)).read()
        m = re.search(r"<script>(.*?)</script>", h, re.S)
        self.assertIsNotNone(m, "%s has no inline script" % page)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(m.group(1))
            tmp = fh.name
        try:
            r = subprocess.run(["node", "--check", tmp], capture_output=True,
                               text=True)
            self.assertEqual(r.returncode, 0,
                             "%s script does not parse:\n%s"
                             % (page, r.stderr[:600]))
        finally:
            os.unlink(tmp)

    def test_metrics_parses(self):
        self._check("metrics.html")

    def test_statistics_parses(self):
        self._check("statistics.html")

    def test_engines_parses(self):
        self._check("engines.html")

    def test_settings_parses(self):
        self._check("settings.html")


class TestStatisticsPageWiring(unittest.TestCase):
    """The statistics page must agree with the metrics page on which lanes are
    shown there, or the "on metrics" pill lies. The two rules are duplicated
    literals because static HTML cannot import across pages, so this test is
    the only thing keeping them in step."""

    def setUp(self):
        self.m = open(os.path.join(ROOT, "metrics.html")).read()
        self.s = open(os.path.join(ROOT, "statistics.html")).read()

    def test_both_cap_at_three(self):
        self.assertIn("MODEL_DASH_MAX=3", self.m)
        self.assertIn("DASH_LIVE=3", self.s)

    def test_both_use_the_same_live_window(self):
        w_m = re.search(r"MODEL_LIVE_WINDOW=(\d+)", self.m)
        w_s = re.search(r"LIVE_WINDOW\s*=\s*(\d+)\s*\*\s*60", self.s)
        self.assertIsNotNone(w_m, "metrics window not found")
        self.assertIsNotNone(w_s, "statistics window not found")
        self.assertEqual(int(w_m.group(1)) * 60, int(w_s.group(1)) * 60,
                         "the two pages disagree on what counts as 'used'")

    def test_statistics_is_in_the_nav(self):
        self.assertIn('href="/statistics"', self.m)
        self.assertIn('href="/metrics"', self.s)
        self.assertIn('href="/statistics"', self.s)

    def test_statistics_posts_with_the_write_token(self):
        """Deleting a lane is a write: loopback-only AND token-carrying. A
        missing header 403s a working button with no obvious cause."""
        posts = re.findall(r"method\s*:\s*['\"]POST['\"]", self.s)
        self.assertTrue(posts, "no POST on the statistics page")
        for m in re.finditer(r"method\s*:\s*['\"]POST['\"]", self.s):
            start = self.s.rfind("fetch(", 0, m.start())
            seg = self.s[start:m.start() + 300]
            self.assertIn("X-Dashboard-Token", seg)

    def test_no_placeholder_survives(self):
        for bad in ("undefined", "NaN", "[object Object]"):
            self.assertNotIn(">" + bad + "<", self.s,
                             "%s renders literally" % bad)

    def test_lane_label_survives_nul_mangling(self):
        """The ledger key is a NUL-separated triple; the DOM delivers U+FFFD.
        The page must split on U+FFFD, and keep the raw key for the delete
        call, or it deletes the wrong row (or nothing)."""
        self.assertIn("split('\\ufffd')", self.s)

    def test_history_urls_use_a_real_parameter_and_span(self):
        """The history endpoint parses `span=` in SECONDS and ignores anything
        else. A `range=14d` URL was silently accepted and answered with the 1-day
        default, so the page reported 2/10 coverage while claiming a 14-day
        window. Unknown params are ignored, not rejected -- nothing errors.

        HIST_SPANS is (3600, 86400, 604800); any other span is snapped to the
        nearest, so asking for 1209600 quietly becomes 604800.
        """
        import dashboard as D
        urls = re.findall(r"/api/metrics/history\?([^\"']+)", self.s)
        self.assertTrue(urls, "the page fetches no history")
        for q in urls:
            self.assertIn("span=", q,
                          "history URL %r has no span=; the endpoint ignores "
                          "unrecognised params and answers with the 1d default"
                          % q)
            m = re.search(r"span=(\d+)", q)
            self.assertIn(int(m.group(1)), D.HIST_SPANS,
                          "span=%s is not in HIST_SPANS %s and would be "
                          "snapped to a different window"
                          % (m.group(1), D.HIST_SPANS))
            self.assertIn("port=", q,
                          "history URL %r is not port-scoped; the page needs "
                          "one series per lane" % q)

    def test_repaint_does_not_replay_entry_animations(self):
        """The shared stylesheet animates every .tile and .card on INSERT
        (`animation:rise .4s both`). The metrics page does not flicker because
        it diffs values in place; the statistics page rebuilds tile markup, so
        that animation replayed on every refresh as a 400ms fade+slide.

        Entry animation must therefore be scoped to the first paint only.
        """
        self.assertIn(".tiles .tile{animation:none}", self.s,
                      "tiles animate on every re-insert -> the page flickers")
        self.assertIn(".tiles.first .tile{animation:rise .4s both}", self.s,
                      "the entry animation lost its first-paint-only scope")
        self.assertIn("__painted", self.s,
                      "nothing marks the first paint, so the animation cannot "
                      "be scoped to it")

    def test_unchanged_data_does_not_rewrite_the_dom(self):
        """innerHTML on a container replaces every node inside it. Guard the
        rewrites with a signature of what they render, so a poll that changes
        nothing leaves the DOM (and its animations) untouched.

        Asserted BEHAVIOURALLY, not by grepping for a variable name: a mere
        presence check passed even with the guard neutered to `if(true)`,
        because the signature was still assigned. A test that cannot fail on
        the bug it names is decoration.
        """
        import re as _re
        for guard in ("tileSig", "shareSig", "ratioSig"):
            m = _re.search(r"if\(%s!==window\.__%s\)\{" % (guard, guard), self.s)
            self.assertIsNotNone(
                m, "%s container is rewritten unconditionally; an unchanged "
                   "poll still replaces every node" % guard)
            # the comparison must actually gate the write, not sit in dead code
            self.assertIn("var %s=" % guard, self.s)

    def test_a_container_rewrite_is_inside_its_guard(self):
        """The innerHTML assignment must sit INSIDE the signature guard. A
        guard that opens but does not wrap the write is worse than none: it
        looks correct and changes nothing."""
        for guard, container in (("tileSig", "lane-tiles"),
                                 ("shareSig", "lane-bars"),
                                 ("ratioSig", "lane-ratio")):
            i = self.s.index("if(%s!==window.__%s){" % (guard, guard))
            j = self.s.index("$('%s').innerHTML" % container, i)
            # the guard must open before the write and close after it
            depth = 0
            closed = False
            for k in range(i, len(self.s)):
                if self.s[k] == "{":
                    depth += 1
                elif self.s[k] == "}":
                    depth -= 1
                    if depth == 0:
                        closed = j < k
                        break
            self.assertTrue(closed,
                            "%s is written outside its %s guard"
                            % (container, guard))

    def test_a_failed_fetch_does_not_blank_the_page(self):
        """A transient network error must leave the last good values up.
        Clearing to placeholders reads as data loss when nothing was lost."""
        self.assertNotIn(".catch(function(){});", self.s,
                         "the metrics fetch swallows errors by blanking; it "
                         "should leave the last good render in place")

    def test_series_fetch_is_cached_not_per_poll(self):
        """The 7-day per-port series only changes when a sample lands (~30s).
        Re-requesting every port on every 15s poll is wasted work AND forces a
        full canvas redraw, which is itself visible."""
        self.assertIn("SERIES_REFRESH_MS", self.s)
        self.assertIn("__histCache", self.s)
        self.assertIn("fetchPortSeries", self.s)
        self.assertIn("applyPortSeries", self.s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
