"""TPOT / e2e / preemption must be visible, not just collected.

THE DEFECT THIS PINS: the dashboard computed TPOT (inter-token gap), e2e
latency and preemptions on every poll, and wrote TPOT + preemptions to
the DB — then never projected them into the history series and never drew
them. TTFT alone only shows the wait to the FIRST token, which says
nothing about how fast the rest of a response streams, so the card read
"fast" for a lane that was actually trickling tokens out.

This suite asserts:
  1. the history API exports the latency series it has columns for
  2. tpot_p50 is projected (it had a column and 92 rows but no series key)
  3. the frontend declares the canvases/legends the render path draws to
  4. the series keys the render path reads actually exist

Run: python3 tests/test_latency_visibility.py
"""
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import _bootstrap  # noqa: F401  (must precede dashboard: isolates log + DB)
from _bootstrap import D

HTML = os.path.join(ROOT, "metrics.html")


class TestHistoryProjectsLatency(unittest.TestCase):
    def test_history_series_include_tpot_and_e2e(self):
        """The projection list is a literal tuple in api history; assert the
        keys the UI needs are in it."""
        with open(os.path.join(ROOT, "dashboard.py")) as fh:
            src = fh.read()
        i = src.index('series = {k: [r.get(k) for r in rows] for k in')
        block = src[i:i + 800]
        # tpot_p99 is deliberately absent: there is no such COLUMN (the live
        # path computes it, but only p50/p95 are persisted), so projecting it
        # would yield an all-None series. The live path still shows p99.
        for key in ("tpot_p50", "tpot_p95", "e2e_p95"):
            self.assertIn(key, block,
                          "%s missing from the history series projection" % key)

    def test_tpot_is_persisted(self):
        """tpot_p50/p95/p99 columns must exist and hold real values, or the
        series would project all-None."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        try:
            import metadb
            metadb.init_db(path)
            c = metadb.connect(path)
            cols = [r["name"] for r in c.execute("PRAGMA table_info(samples)")]
            for k in ("tpot_p50", "tpot_p95", "e2e_p95", "preempt_per_min"):
                self.assertIn(k, cols, "%s column missing" % k)
            now = int(time.time())
            metadb.write_sample(c, {"ts": now, "port": 8001, "model": "m",
                                    "tpot_p50": 0.064, "tpot_p95": 0.097,
                                    "e2e_p95": 3.75, "preempt_per_min": 0.0})
            c.commit()
            row = c.execute("SELECT tpot_p50, preempt_per_min FROM samples "
                            "WHERE port=8001 ORDER BY ts DESC LIMIT 1").fetchone()
            c.close()
            self.assertAlmostEqual(row["tpot_p50"], 0.064, places=3)
            self.assertEqual(row["preempt_per_min"], 0.0)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_window_stats_computes_tpot_and_e2e(self):
        """Live path must produce the fields the card reads.

        Buckets are CUMULATIVE and _hist_delta differences them between two
        samples, so the fixture has to GROW between samples — two identical
        histograms give a zero delta and a None percentile, which looks
        identical to "the engine published nothing".
        """
        now = time.time()
        key = "vllm:inter_token_latency_seconds"

        def smp(n, total):
            # cumulative buckets: <=0.05s holds n, +Inf holds total
            return {"gauges": {}, "counters": {},
                    "histograms": {key: {"buckets": [(0.05, n),
                                                    (float("inf"), total)],
                                         "sum": 0.05 * n, "count": total}}}

        st = {"samples": [(now - 12, smp(0, 0)),
                          (now - 2, smp(8, 10))],
              "slot_cap": 4}
        res = D._window_stats(st, 60)
        self.assertIsNotNone(res.get("tpot_p50"),
                             "live path did not derive tpot_p50 from the "
                             "inter-token histogram")
        self.assertGreater(res["tpot_p50"], 0)


class TestFrontendRendersThem(unittest.TestCase):
    def setUp(self):
        with open(HTML) as fh:
            self.h = fh.read()

    def test_cards_exist(self):
        for cid in ("c-tpot", "lg-tpot", "c-preempt", "lg-preempt"):
            self.assertIn('id="%s"' % cid, self.h,
                          "%s missing from metrics.html" % cid)

    def test_render_path_draws_to_those_ids(self):
        """A canvas that is never drawn to is the exact bug we are fixing —
        assert the render path references each id."""
        m = re.search(r"<script>(.*)</script>", self.h, re.S)
        self.assertIsNotNone(m, "no <script> block in metrics.html")
        js = m.group(1)
        # Canvases are fetched as $('c-x'); legends are written through
        # setLegend('lg-x', rows). Both must be referenced, otherwise the
        # element exists in the DOM but stays empty.
        for cid in ("c-tpot", "c-preempt"):
            self.assertIn("$('%s')" % cid, js,
                          "%s canvas is never drawn to — it would render "
                          "blank" % cid)
        for lid in ("lg-tpot", "lg-preempt"):
            self.assertIn("setLegend('%s'" % lid, js,
                          "%s is never populated by the render path" % lid)

    def test_series_keys_exist_for_tpot_e2e_preempt(self):
        sk = re.search(r"function seriesKeys\(.*?return \{(.*?)\n  \};",
                       self.h, re.S)
        self.assertIsNotNone(sk, "seriesKeys not found")
        body = sk.group(1)
        keys = re.findall(r"^\s+(\w+):", body, re.M)
        for key in ("tpot50", "tpot95", "e2e95", "preempt"):
            self.assertIn(key, keys, "series key %s missing" % key)

    def test_live_branch_reads_stats_not_series(self):
        """On the LIVE path the engine object carries these as scalars under
        e.stats (tpot_p50, tpot_p95, e2e_p95); the history path carries them
        as ARRAYS under the series object. Reading sr.* on the live branch
        yields undefined, which renders as 'no data in window' — the card
        looks broken while the API is returning good values. Assert each
        branch reads the object that actually holds the data.
        """
        m = re.search(r"function seriesKeys\(.*?return \{(.*?)\n  \};",
                      self.h, re.S)
        body = m.group(1)
        for key, stat in (("tpot50", "tpot_p50"), ("tpot95", "tpot_p95"),
                          ("e2e95", "e2e_p95")):
            line = next((l for l in body.splitlines()
                         if re.match(r"^\s+%s:" % key, l)), None)
            self.assertIsNotNone(line, "key %s not found" % key)
            self.assertIn("e.stats", line,
                          "%s live branch does not read e.stats — the live "
                          "path stores %s as a scalar there, not in the "
                          "series object" % (key, stat))
            self.assertIn("sr.", line,
                          "%s history branch does not read the series "
                          "object" % key)

    def test_series_keys_reference_only_in_scope_names(self):
        """seriesKeys(e,hist,d) may only use e, hist, d and sr.

        THE DEFECT: the preemption key referenced `s` (the caller's stats
        variable) which does not exist in that scope, and the prefix-hit key
        referenced `K` — the very object being built. Both threw
        ReferenceError at render time and blanked the ENTIRE page, because the
        seriesKeys() call sits at the top of renderPanels(). A presence-only
        test passes straight through this, so assert the scope explicitly.
        """
        m = re.search(r"function seriesKeys\(([^)]*)\)", self.h)
        self.assertIsNotNone(m, "seriesKeys not found")
        params = {x.strip() for x in m.group(1).split(",")}
        allowed = params | {"sr", "hist", "Math", "Number", "JSON",
                            "sumSeries", "mean", "maxOf", "lastOf",
                            "cumSeries", "toArr", "null", "true", "false"}
        body = re.search(r"function seriesKeys\(.*?return \{(.*?)\n  \};",
                         self.h, re.S).group(1)
        # strip string literals and comments before scanning for identifiers
        code = re.sub(r"/\*.*?\*/", " ", body, flags=re.S)
        code = re.sub(r"'(?:[^'\\]|\\.)*'", "''", code)
        used = set(re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)", code))
        # object KEYS legitimately match the pattern; drop anything followed by ':'
        keys = set(re.findall(r"^\s*(\w+):", body, re.M))
        unknown = {u for u in used
                   if u not in allowed and u not in keys
                   and u not in ("var", "return", "function", "new", "in",
                                 "of", "if", "else", "typeof")}
        self.assertFalse(unknown,
                         "seriesKeys references names not in scope: %s "
                         "(would throw ReferenceError and blank the page)"
                         % sorted(unknown))

    def test_no_duplicate_series_keys(self):
        """A duplicate object key silently drops the earlier one — that is
        how the ph/ph ratio bug shipped."""
        m = re.search(r"function seriesKeys\(.*?return \{(.*?)\n  \};",
                      self.h, re.S)
        self.assertIsNotNone(m, "seriesKeys not found")
        keys = re.findall(r"^\s+(\w+):", m.group(1), re.M)
        dupes = {k for k in keys if keys.count(k) > 1}
        self.assertFalse(dupes, "duplicate series keys: %s" % dupes)

    def test_single_stream_guards_against_zero_tpot(self):
        """1/tpot with tpot==0 must not produce Infinity in the legend."""
        self.assertIn("t50v>0", self.h,
                      "1/tpot is not guarded against a zero TPOT")
        self.assertIn("single-stream", self.h)


if __name__ == "__main__":
    unittest.main(verbosity=2)
