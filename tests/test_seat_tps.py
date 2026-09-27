"""Per-seat t/s + absolute KV budget, on BOTH backends (2026-09-27).

The live vLLM brain lane has no seat axis (vLLM exposes no /slots), and no
llama.cpp lane is currently served, so both sides are driven from synthetic
scrape samples and a stubbed /slots fetch. The contract under test:

  - llama.cpp: per-seat rates are a real measurement, one per busy seat,
    stored on the sample so history can read them, and derived from a
    same-task position delta.
  - vLLM: no seat identity. The code must return None/absent rather than
    invent a per-seat number. A fair-share estimate is labelled as such by
    the UI and must never be persisted as if it were measured.
  - Absolute KV tokens come from cache_config_info on vLLM and from the
    summed n_ctx on llama.cpp, and render through the same field.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard as D
import metadb
import promparse


def vllm_sample(kv_pct=33.34, running=2, cap_blocks="758", cap_bs="1632"):
    """A vLLM sample in the shape the pipeline ACTUALLY stores: raw /metrics
    text parsed, then run through _with_vllm_aliases. Building the derived
    gauges by hand here would test the test's own dict, not the code that
    derives them from cache_config_info."""
    lab = []
    if cap_blocks:
        lab.append(f'num_gpu_blocks="{cap_blocks}",block_size="{cap_bs}",'
                   f'kv_cache_memory_bytes="34000000000"')
    raw = (
        "# TYPE vllm:cache_config_info gauge\n"
        f'vllm:cache_config_info{{{",".join(lab)}}} 1.0\n'
        if lab else "")
    raw += ("# TYPE vllm:kv_cache_usage_perc gauge\n"
            f"vllm:kv_cache_usage_perc {kv_pct/100.0}\n"
            "# TYPE vllm:num_requests_running gauge\n"
            f"vllm:num_requests_running {running}\n"
            "# TYPE vllm:num_requests_waiting gauge\n"
            "vllm:num_requests_waiting 1\n")
    return D._with_vllm_aliases(promparse.parse(raw))


class TestKvAbsolute(unittest.TestCase):
    def test_vllm_capacity_derived_from_labels(self):
        raw = ('# TYPE vllm:cache_config_info gauge\n'
               'vllm:cache_config_info{num_gpu_blocks="758",block_size="1632",'
               'kv_cache_memory_bytes="34000000000"} 1.0\n')
        # _with_vllm_aliases returns `parsed`, whose gauges live under
        # ["gauges"] — not at the top level.
        g = D._with_vllm_aliases(promparse.parse(raw))["gauges"]
        self.assertEqual(g["vllm:kv_cache_capacity_tokens"]["value"],
                         758 * 1632)              # 1_237_056
        self.assertEqual(g["vllm:kv_cache_num_blocks"]["value"], 758)
        self.assertEqual(g["vllm:kv_cache_block_size"]["value"], 1632)
        self.assertEqual(g["vllm:kv_cache_memory_bytes"]["value"], 34e9)

    def test_partial_labels_do_not_fabricate_capacity(self):
        raw = 'vllm:cache_config_info{block_size="1632"} 1.0\n'
        g = D._with_vllm_aliases(promparse.parse(raw))["gauges"]
        self.assertNotIn("vllm:kv_cache_capacity_tokens", g)

    def test_window_stats_computes_used_and_free(self):
        now = time.time()
        st = {"samples": [(now - 5, vllm_sample()),
                          (now, vllm_sample())], "slot_cap": 4}
        s = D._window_stats(st, 30)
        self.assertEqual(s["kv_capacity_tokens"], 758 * 1632)
        self.assertEqual(s["kv_used_tokens"],
                         int(round(758 * 1632 * 33.34 / 100.0)))
        self.assertEqual(s["kv_used_tokens"] + s["kv_free_tokens"],
                         758 * 1632)

    def test_free_never_negative(self):
        """A percentage above 100 (a transient gauge spike, or a lane whose
        prefill just started) must not produce a negative free count. The
        alias scaler only rewrites 0-1 fractions, so drive the already-scaled
        value directly."""
        now = time.time()
        s = {"gauges": {"vllm:kv_cache_usage_perc": {"value": 140.0,
                                                      "labels": {}},
                        "vllm:kv_cache_capacity_tokens": {"value": 1237056.0,
                                                          "labels": {}}},
             "counters": {}, "histograms": {}}
        # _window_stats needs a >=2-point window (it rates over the span), so
        # the absolute-KV block only runs on a ring with real history.
        st = {"samples": [(now - 5, s), (now, s)], "slot_cap": 4}
        r = D._window_stats(st, 30)
        self.assertGreaterEqual(r["kv_free_tokens"], 0)


def _llama_slots(ids=(0, 1), n_ctx=131072, decoded=20, task=100, prompt=10):
    """One /slots entry per seat. pos = n_decoded + n_prompt_tokens_processed,
    so seed the baseline with the same prompt offset — otherwise the delta
    carries that constant and the rate is inflated by prompt/span."""
    return [{"id": i, "id_task": task + i, "n_ctx": n_ctx,
             "n_prompt_tokens_processed": prompt, "n_prompt_tokens": prompt,
             "is_processing": True,
             "next_token": [{"n_decoded": decoded, "n_remain": 1}]}
            for i in ids]


def _llama_st(n_slots=2):
    now = time.time()
    return {"port": 8890,
            "samples": [(now, {"gauges": {}, "counters": {}, "histograms": {}})],
            "slot_cap": n_slots, "slot_tps": {}, "slot_rate_pos": {},
            "n_slots": n_slots}


def _baseline(st, t, pos=10, ids=(0, 1)):
    """Seed the same tasks at `pos` tokens, 2s ago."""
    st["slot_rate_pos"] = {i: (100 + i, pos, t - 2.0) for i in ids}


def _feed(st, slots):
    """Drive _update_slots with a stubbed /slots fetch."""
    class R:
        @staticmethod
        def read():
            return json.dumps(slots).encode()

    with mock.patch.object(D.urllib.request, "urlopen",
                           return_value=R()):
        D._update_slots(st)


class TestSeatTps(unittest.TestCase):
    def test_seat_rate_is_per_seat_and_measured(self):
        st = _llama_st()
        t = time.time()
        # baseline pos 10 (prompt 10 + decoded 0) -> now 30 => 20 tok / 2 s
        _baseline(st, t)
        _feed(st, _llama_slots(decoded=20))
        rates = st["slot_tps"]
        self.assertEqual(set(rates), {0, 1})
        for r in rates.values():
            self.assertAlmostEqual(r, 10.0, delta=0.6)

    def test_seats_are_independent_not_a_fair_share(self):
        """One fast seat + one slow seat must NOT both show the average."""
        st = _llama_st()
        t = time.time()
        _baseline(st, t)
        # seat 0: decoded 20 -> pos 30 (delta 20 => 10/s)
        # seat 1: decoded 12 -> pos 22 (delta 12 =>  6/s)
        slots = _llama_slots()
        slots[0]["next_token"][0]["n_decoded"] = 20
        slots[1]["next_token"][0]["n_decoded"] = 12
        _feed(st, slots)
        self.assertAlmostEqual(st["slot_tps"][0], 10.0, delta=0.6)
        self.assertAlmostEqual(st["slot_tps"][1], 6.0, delta=0.6)

    def test_seat_rate_persisted_onto_sample(self):
        """A /slots delta cannot be reconstructed later, so store it."""
        st = _llama_st()
        t = time.time()
        _baseline(st, t)
        _feed(st, _llama_slots(decoded=20))
        self.assertTrue(st["samples"][-1][1]["seat_tps"])
        self.assertEqual(st["samples"][-1][1]["seat_tps"], st["slot_tps"])

    def test_no_rate_before_a_delta_exists(self):
        """First sighting has no prior poll -> no rate, not a fabricated one."""
        st = _llama_st()
        _feed(st, _llama_slots())
        self.assertEqual(st["slot_tps"], {})

    def test_task_change_resets_the_rate(self):
        """A new id_task is a new request: positions are unrelated, so the
        rate must go dark rather than diff against the previous task."""
        st = _llama_st()
        t = time.time()
        st["slot_rate_pos"] = {0: (100, 5, t - 2.0)}
        slots = _llama_slots(ids=(0,))
        slots[0]["id_task"] = 999
        slots[0]["next_token"][0]["n_decoded"] = 1
        _feed(st, slots)
        self.assertNotIn(0, st["slot_tps"])

    def test_vllm_never_claims_a_seat_identity(self):
        """vLLM has no seat axis: src is 'req' (histogram-measured) or None,
        never 'slot'."""
        now = time.time()
        st = {"port": 8001, "slot_cap": 4,
              "samples": [(now - 30, {"gauges": {}, "counters": {},
                                      "histograms": {}}),
                          (now, {"gauges": {"vllm:num_requests_running":
                                            {"value": 2.0, "labels": {}}},
                                 "counters": {}, "histograms": {}})]}
        L = D._live_slot_state(st)
        self.assertNotEqual(L.get("src"), "slot")
        self.assertEqual(len(L["seats"]), 4)

    def test_seat_series_null_on_vllm(self):
        now = time.time()
        st = {"samples": [(now - 5, {"gauges": {}, "counters": {},
                                      "histograms": {}}),
                          (now, {"gauges": {}, "counters": {},
                                 "histograms": {}})],
              "slot_cap": 4}
        ser = D._engine_series(st, 60)
        self.assertIn("seat_tps", ser["slot_series"])
        self.assertTrue(all(x is None for x in ser["slot_series"]["seat_tps"]))

    def test_seat_series_carries_measured_rates(self):
        st = _llama_st()
        t = time.time()
        _baseline(st, t)
        _feed(st, _llama_slots(decoded=20))
        ser = D._engine_series(st, 60)
        rates = ser["slot_series"]["seat_tps"][-1]
        self.assertIsNotNone(rates)
        self.assertEqual(len(rates), 2)

    def test_llama_kv_capacity_from_slots(self):
        """llama.cpp's summed n_ctx is its budget; it must reach the same
        field vLLM fills, so the card is backend-neutral."""
        st = _llama_st(n_slots=2)
        _feed(st, _llama_slots(n_ctx=131072))
        g = st["samples"][-1][1]["gauges"]
        self.assertEqual(g["vllm:kv_cache_capacity_tokens"]["value"],
                         2 * 131072)


class TestSeatTpsDb(unittest.TestCase):
    def test_roundtrip_and_parse(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        try:
            metadb.init_db(path)
            c = metadb.connect(path)
            now = int(time.time())
            metadb.write_sample(c, {
                "ts": now, "port": 8890, "model": "m",
                "seat_tps_json": json.dumps({0: 41.0, 1: 12.5}),
                "kv_capacity_tokens": 262144.0, "kv_used_tokens": 1000.0})
            c.commit()
            rows = metadb.query_range(c, 8890, now - 3600, limit=100)
            c.close()
            self.assertEqual(len(rows), 1)
            d = json.loads(rows[0]["seat_tps_json"])
            # Parsed the way api_history parses: sorted by seat id.
            self.assertEqual([d[k] for k in sorted(d, key=int)],
                             [41.0, 12.5])
            self.assertEqual(rows[0]["kv_capacity_tokens"], 262144.0)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_null_seat_json_reads_back_as_none(self):
        """vLLM rows have no seat payload -> None, not {} -> a fake seat."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        try:
            metadb.init_db(path)
            c = metadb.connect(path)
            now = int(time.time())
            metadb.write_sample(c, {"ts": now, "port": 8001, "model": "m",
                                    "seat_tps_json": None})
            c.commit()
            rows = metadb.query_range(c, 8001, now - 3600, limit=100)
            c.close()
            self.assertIsNone(rows[0]["seat_tps_json"])
        finally:
            if os.path.exists(path):
                os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
