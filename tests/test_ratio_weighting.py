"""Ratio gauges must be weighted by VOLUME, not averaged as ratios.

DEFECT: the history path rendered mean(per-sample hit_rate). The stored
per-point value is itself a cumulative hits/queries ratio, so averaging
those ratios is not the window ratio. A window with one cached query
among many idle polls reads as a healthy hit rate when in reality almost
nothing was ever cached — the idle polls contribute a 0% sample each and
should contribute nothing at all.

The live path already computed sum(hits)/sum(queries) correctly; only
history was wrong, and it was wrong because the counts were not stored.

Run: python3 tests/test_ratio_weighting.py
"""
import tempfile
import json
import os
import sqlite3
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import _bootstrap  # noqa: F401  (must precede dashboard: isolates log + DB)

from _bootstrap import D
import metadb


def _row(ts, hits, queries, pct=None):
    return {"ts": ts, "port": 8001, "model": "m",
            "prefix_hits": hits, "prefix_queries": queries,
            "prefix_hit_rate": pct}


class TestWindowRatioIsVolumeWeighted(unittest.TestCase):
    def test_window_ratio_is_sum_over_sum_not_mean_of_ratios(self):
        """Construct the exact shape the mean() got wrong.

        4 samples. One carries a real cache hit; three sit idle with 0
        queries. The stored per-point ratios are 100% (the one hit) and
        undefined/0 for the idle ones.
          mean of per-point ratios  -> ~25%   (wrong: 3 of 4 polls saw
                                                  no query at all)
          sum(hits)/sum(queries)   -> 100%  (correct)
        """
        rows = [
            _row(1000, 0, 0, None),        # window start: nothing yet
            _row(1001, 50, 50, 100.0),     # 50 hits / 50 queries
            _row(1002, 50, 50, 100.0),     # idle: unchanged
            _row(1003, 50, 50, 100.0),     # idle
        ]
        # The mean() version over the per-point series.
        vals = [r["prefix_hit_rate"] for r in rows if r["prefix_hit_rate"] is not None]
        mean_of_ratios = sum(vals) / len(vals)
        # The correct window ratio: delta hits / delta queries. Only ONE
        # sample saw a query; the other two saw none at all and must not
        # dilute the answer.
        dh = rows[-1]["prefix_hits"] - rows[0]["prefix_hits"]
        dq = rows[-1]["prefix_queries"] - rows[0]["prefix_queries"]
        window_ratio = 100.0 * dh / dq
        self.assertAlmostEqual(window_ratio, 100.0, delta=0.01)
        # Both happen to be 100 here because every non-null sample shares the
        # same ratio; the divergence case is the next test.
        self.assertEqual(len(vals), 3)

    def test_mean_diverges_when_a_burst_sample_differs(self):
        """A burst sample with a LOW ratio among high-ratio idle samples is
        where mean() and sum/sum genuinely diverge.

        Counters are cumulative, so the window ratio is always
        (last - first) / (last - first) over the deltas — the idle samples
        contribute ZERO to both sides and must not dilute anything.
        """
        rows = [
            _row(1000, 900, 1000, 90.0),    # cumulative at window start
            _row(1001, 900, 1000, 90.0),    # idle: unchanged
            _row(1002, 900, 1000, 90.0),    # idle
            _row(1003, 1000, 1200, 83.3),   # +100 hits / +200 queries
        ]
        vals = [r["prefix_hit_rate"] for r in rows]
        mean_of_ratios = sum(vals) / len(vals)
        dh = rows[-1]["prefix_hits"] - rows[0]["prefix_hits"]
        dq = rows[-1]["prefix_queries"] - rows[0]["prefix_queries"]
        window_ratio = 100.0 * dh / dq
        # sum/sum: 100 hits over 200 queries in-window = 50%
        self.assertAlmostEqual(window_ratio, 50.0, delta=0.1)
        # mean() drags the answer toward the idle samples' 90% and more than
        # doubles the reported hit rate.
        self.assertGreater(mean_of_ratios, 1.5 * window_ratio,
                           "mean of ratios over-weights idle samples")


class TestStatsExposeCounts(unittest.TestCase):
    def test_window_stats_exposes_raw_prefix_counters(self):
        """_window_stats must surface the counts so they can be persisted;
        the derived percentage alone cannot be re-weighted."""
        now = time.time()

        def smp(hits, queries):
            return {"gauges": {}, "counters": {
                "vllm:prefix_cache_hits_total": {"value": float(hits),
                                                "labels": {}},
                "vllm:prefix_cache_queries_total": {"value": float(queries),
                                                   "labels": {}}},
                "histograms": {}}

        # _window_stats drops samples younger than 0.5s and older than the
        # window, and needs >= 2 survivors plus dt >= 1s. Age the pair so
        # both are inside the window with a real span.
        st = {"samples": [(now - 12, smp(100, 200)),
                          (now - 2, smp(300, 500))], "slot_cap": 4}
        res = D._window_stats(st, 60)
        self.assertEqual(res["prefix_hits"], 300)
        self.assertEqual(res["prefix_queries"], 500)
        # queries_total INCLUDES hits, so the denominator is 300 alone:
        # 200 hits / 300 queries = 66.7%. The old code divided by
        # hits+queries (500) and reported 40.0% — hits double-counted.
        self.assertAlmostEqual(res["prefix_hit_rate"], 66.7, delta=0.2)


class TestCountsPersistAndRoundTrip(unittest.TestCase):
    def test_prefix_counts_are_stored(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        try:
            metadb.init_db(path)
            c = metadb.connect(path)
            now = int(time.time())
            metadb.write_sample(c, {"ts": now, "port": 8001, "model": "m",
                                    "prefix_hits": 300.0,
                                    "prefix_queries": 500.0,
                                    "prefix_hit_rate": 60.0})
            c.commit()
            rows = metadb.query_range(c, 8001, now - 3600, limit=100)
            c.close()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["prefix_hits"], 300.0)
            self.assertEqual(rows[0]["prefix_queries"], 500.0)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_db_row_carries_the_counts(self):
        """_to_db_row must pass them through, or the migration adds columns
        nothing ever writes."""
        st = {"samples": [], "slot_cap": 4, "port": 8001}
        row = D._to_db_row(8001, st)
        self.assertIn("prefix_hits", row)
        self.assertIn("prefix_queries", row)


class TestNoZeroQueriesDivision(unittest.TestCase):
    def test_queries_include_hits_so_no_double_count(self):
        """The denominator is the query count ALONE. A hit is a query that was
        served from cache, so prefix_cache_queries_total already contains it.
        Dividing by hits+queries double-counts every hit and under-reports
        the rate: 200 hits / 300 queries is 66.7%, not 40.0%."""
        now = time.time()

        def smp(h, q):
            return {"gauges": {}, "counters": {
                "vllm:prefix_cache_hits_total": {"value": float(h),
                                                "labels": {}},
                "vllm:prefix_cache_queries_total": {"value": float(q),
                                                   "labels": {}}},
                "histograms": {}}

        st = {"samples": [(now - 12, smp(0, 0)),
                          (now - 2, smp(200, 300))], "slot_cap": 4}
        res = D._window_stats(st, 60)
        self.assertAlmostEqual(res["prefix_hit_rate"], 66.7, delta=0.2)
        # explicitly NOT the double-counted figure
        self.assertNotAlmostEqual(res["prefix_hit_rate"], 40.0, delta=0.5)

    def test_all_hits_is_100_percent(self):
        """Every query a hit: rate is 100%, and the old formula capped it at
        50% — a lane caching literally everything read as half-cached."""
        now = time.time()

        def smp(h, q):
            return {"gauges": {}, "counters": {
                "vllm:prefix_cache_hits_total": {"value": float(h),
                                                "labels": {}},
                "vllm:prefix_cache_queries_total": {"value": float(q),
                                                   "labels": {}}},
                "histograms": {}}

        st = {"samples": [(now - 12, smp(0, 0)),
                          (now - 2, smp(500, 500))], "slot_cap": 4}
        res = D._window_stats(st, 60)
        self.assertAlmostEqual(res["prefix_hit_rate"], 100.0, delta=0.2)

    def test_zero_queries_yields_none_not_a_crash(self):
        rows = [_row(1000, 0, 0, None), _row(1001, 0, 0, None)]
        out = []
        for r in rows:
            q = r.get("prefix_queries")
            h = r.get("prefix_hits")
            v = None
            if q and h is not None:
                try:
                    v = round(100.0 * float(h) / float(q), 1)
                except (TypeError, ValueError, ZeroDivisionError):
                    v = None
            out.append(v)
        self.assertEqual(out, [None, None])


if __name__ == "__main__":
    unittest.main(verbosity=2)
