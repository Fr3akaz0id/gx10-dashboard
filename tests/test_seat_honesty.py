"""Guards against presenting a non-measurement as a per-seat measurement.

Context: the SLOTS card used to render four numbered seat tiles for vLLM with
src="req" (one fleet-wide histogram mean stamped into every busy seat), and the
UI would further divide the aggregate rate by the running count when no
measurement existed. Both rendered in the same style as measured per-seat data.
A user reading "slot 2: 18.3 tok/s" would take it as observed.

These tests pin the honest behaviour:
  * llama.cpp /slots still gets real per-seat tiles with measured rates.
  * a backend with no per-seat identity gets a concurrency meter, not tiles.
  * no seat tile is ever filled from a fleet-wide or arithmetic-derived value.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401  (redirects production LOG/DB paths)

import dashboard

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "metrics.html")) as _fh:
    HTML = _fh.read()
with open(os.path.join(ROOT, "dashboard.py")) as _fh:
    SRC = _fh.read()


def _sample(counters):
    """One sample in the real shape: a TUPLE (ts, parsed), where parsed carries
    {"gauges": {name: {"value": v}}}.

    Two things bit here first: _g() reads m["gauges"][name]["value"] so a flat
    dict of raw numbers is wrong, and samples entries are tuples so
    samples[-1][1] is the parsed dict. Getting either wrong made every test
    error with KeyError: 1 instead of asserting anything -- a test file that
    looks thorough while testing nothing.
    """
    gauges = {n: {"value": v} for n, v in counters.items()}
    return [(1000, {"gauges": gauges, "counters": {}, "histograms": {}})]


class TestSeatHonesty(unittest.TestCase):

    def test_llamacpp_slots_get_real_per_seat_rates(self):
        """The one backend that genuinely has seats must keep them."""
        st = {
            "slot_cap": 4,
            "slot_tps": {0: 41.2, 2: 38.7},
            "samples": _sample({"vllm:num_requests_running": 2,
                                "vllm:num_requests_waiting": 1}),
        }
        out = dashboard._live_slot_state(st)
        self.assertEqual(out["src"], "slot")
        self.assertEqual(len(out["busy"]), 2)
        # both real measurements survive, not collapsed to a mean
        self.assertEqual(sorted(v for v in out["busy"] if v is not None),
                         [38.7, 41.2])

    def test_vllm_gets_no_seat_source(self):
        """No /slots -> no per-seat source, ever."""
        st = {
            "slot_cap": 4,
            "samples": _sample({"vllm:num_requests_running": 3,
                                "vllm:num_requests_waiting": 0}),
        }
        out = dashboard._live_slot_state(st)
        self.assertIsNone(out["src"],
                          "vLLM has no seat axis; src must stay None")
        self.assertTrue(all(v is None for v in out["busy"]),
                        "no seat may be handed a rate it did not measure")

    def test_backend_never_stamps_fleet_mean_into_seats(self):
        """A fleet-wide histogram mean must not become N seat readings."""
        st = {
            "slot_cap": 4,
            "samples": _sample({"vllm:num_requests_running": 2,
                                "vllm:num_requests_waiting": 0}),
        }
        out = dashboard._live_slot_state(st)
        measured = [v for v in out["busy"] if v is not None]
        self.assertEqual(measured, [],
                         "no slot may be filled without a /slots measurement")

    def test_no_estimate_path_in_frontend(self):
        """The aggregate÷running estimate must stay deleted."""
        # the specific arithmetic that manufactured per-seat numbers
        self.assertNotIn("orate/run", HTML)
        self.assertNotIn("busy=busy.map", HTML)

    def test_vllm_renders_meter_not_seat_tiles(self):
        """The no-seat branch must exist and be reachable."""
        self.assertIn("no per-seat identity on this backend", HTML)
        # and the seat-tile branch must be guarded on a real seat source
        self.assertIn("rsrc!='slot'&&rsrc!='req'", HTML)

    def test_lane_rate_is_labelled_lane_wide(self):
        """req_rate is a lane mean, so the UI must say so."""
        self.assertIn("lane mean", HTML)
        self.assertNotIn("aggregate rate ÷ running seats", HTML)

    def test_card_is_not_called_slots(self):
        """'SLOTS' over a concurrency meter is the fiction we're removing."""
        self.assertIn("CONCURRENCY &amp; QUEUE", HTML)
        self.assertNotIn("<h2>SLOTS", HTML)

    def test_rate_helper_is_not_named_seat(self):
        """Name carries the honesty: it is a per-REQUEST rate, not per-seat."""
        self.assertIn("def _vllm_measured_req_rate(", SRC)
        self.assertNotIn("_vllm_measured_seat_rate", SRC)


class TestSeatsHonestyMutations(unittest.TestCase):
    """Each mutation re-introduces a specific lie and must be caught."""

    def setUp(self):
        with open(os.path.join(ROOT, "dashboard.py")) as fh:
            self.d = fh.read()
        with open(os.path.join(ROOT, "metrics.html")) as fh:
            self.h = fh.read()

    def _check_fails(self, src=None, html=None):
        """Re-run the assertions above against mutated text."""
        s = src if src is not None else self.d
        h = html if html is not None else self.h
        if "_vllm_measured_seat_rate" in s:
            return True, "stale seat-rate name"
        if "orate/run" in h:
            return True, "estimate path restored"
        if "busy=busy.map" in h:
            return True, "estimate path restored"
        if "<h2>SLOTS" in h:
            return True, "card re-titled SLOTS"
        return False, ""

    def test_clean_tree_is_clean(self):
        bad, why = self._check_fails()
        self.assertFalse(bad, why)

    def test_estimate_mutation_caught(self):
        bad, why = self._check_fails(html=self.h + "\nvar x=orate/run;")
        self.assertTrue(bad, why)

    def test_title_mutation_caught(self):
        bad, why = self._check_fails(html=self.h.replace(
            "CONCURRENCY &amp; QUEUE", "SLOTS"))
        self.assertTrue(bad, why)

    def test_stale_name_mutation_caught(self):
        bad, why = self._check_fails(
            src=self.d.replace("_vllm_measured_req_rate", "_vllm_measured_seat_rate"))
        self.assertTrue(bad, why)


if __name__ == "__main__":
    unittest.main(verbosity=2)
