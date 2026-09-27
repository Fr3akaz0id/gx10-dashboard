"""The live tok/s card must track the engine, not a window average.

REGRESSION for a real defect found 2026-09-27: the OUTPUT TOKENS / SEC card
read ~5 tok/s while the brain lane was decoding at 40.

Root cause, measured not assumed. Sampling generation_tokens_total and the
completion histogram every 2s through a 600-token decode:

    t(s)  gen_total  dgen  tok/s | hist_sum  dhist
     2.0       1997     87   43.3 |      1812      0
     2.0       2077     80   39.9 |      1812      0
     2.0       2141     64   31.9 |      1812      0
     2.0       2213     72   35.9 |      1812      0
     2.0       2293     80   39.8 |      1812      0
     2.0       2376     83   41.3 |      1812      0
     2.0       2412     36   17.9 |      2412    600

The COUNTER advances every poll, mid-decode. The completion HISTOGRAM is
frozen at 1812 throughout and jumps by the whole 600 only at the end. The
old code had a guard reading "on vLLM NEVER use the counter, the
completion counters make a ~10s delta zero" -- exactly backwards. It fed
the histogram's 20s trailing average to the card, which is why the card
showed ~5 against a real 40.

These tests pin the CONTRACT on synthetic samples shaped like that trace,
so the defect cannot come back even though the numbers were measured on a
lane that is idle most of the time (and must never be used as a fixture).
"""
import os
import sys
import time
import unittest

sys.path.insert(0, __file__.rsplit("/", 1)[0].rsplit("/", 1)[0])
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard as D


def _sample(gen_total, prompt_total, hist_sum, hist_count, decode_sum):
    """One scrape shaped like real vLLM /metrics output."""
    return {"gauges": {}, "counters": {
        "vllm:generation_tokens_total": {"value": gen_total, "labels": {}},
        "vllm:prompt_tokens_total": {"value": prompt_total, "labels": {}},
    }, "histograms": {
        "vllm:request_generation_tokens": {
            "buckets": [], "sum": hist_sum, "count": hist_count},
        "vllm:request_decode_time_seconds": {
            "buckets": [], "sum": decode_sum, "count": hist_count},
    }}


def _ring(pairs, step=2.0):
    """pairs of (gen, prompt, hist_sum, hist_count) -> a scrape ring."""
    t = time.time()
    return {"samples": [(t - (len(pairs) - 1 - i) * step, _sample(*p))
                        for i, p in enumerate(pairs)],
            "slot_cap": 4}


class TestLiveRateUsesTheCounter(unittest.TestCase):
    """Mid-decode, the counter is the only thing moving. Use it."""

    # A 10s window at 2s cadence: 6 samples, counter climbing ~40 tok/s,
    # histogram FROZEN (nothing has completed yet).
    DECODING = _ring([
        (1997, 1000, 1812, 7, 100.0),
        (2084, 1000, 1812, 7, 100.0),
        (2171, 1000, 1812, 7, 100.0),
        (2258, 1000, 1812, 7, 100.0),
        (2345, 1000, 1812, 7, 100.0),
        (2432, 1000, 1812, 7, 100.0),
    ])

    def test_live_output_rate_tracks_the_counter(self):
        s = D._window_stats(self.DECODING, 900)
        now = s["output_per_s_now"]
        self.assertIsNotNone(now, "live output rate must be measured")
        # counter moved 435 tokens over ~10s => ~43 tok/s
        self.assertTrue(35 <= now <= 50, f"expected ~43 tok/s, got {now}")

    def test_live_rate_is_not_the_window_average(self):
        """The whole bug in one assertion: the card must not show a long
        window average. Build the REAL shape of the defect -- a long idle
        window with a short busy burst at the end of it, sampled at the real
        2s poll cadence. A 10s decode inside a 900s window reads as ~0.5
        tok/s as a window average; the live rate must still show ~40."""
        t0 = time.time()
        samples = []
        # 450 idle polls at 2s = 900s of nothing, counter parked at 100.
        for i in range(450):
            samples.append((t0 - 900.0 + i * 2.0,
                            _sample(100, 1000, 0, 0, 0.0)))
        # then a 10s burst: the counter climbs at ~40 tok/s.
        g = 100
        for i in range(6):
            g += 80
            samples.append((t0 - 10.0 + i * 2.0,
                            _sample(g, 1000, 0, 0, 0.0)))
        wide = {"samples": samples, "slot_cap": 4}
        s = D._window_stats(wide, 900)
        win = s["output_per_s"]
        now = s["output_per_s_now"]
        self.assertLess(win, 5.0,
                        f"window average is diluted ({win}) -- that is the bug")
        self.assertIsNotNone(now, "live rate must still be measured")
        self.assertTrue(35 <= now <= 50, f"expected ~40 tok/s, got {now}")
        self.assertGreater(now, 10 * max(win, 0.1),
                           f"live rate ({now}) must dominate window ({win})")

    def test_live_total_rate_present_on_decode_only_tick(self):
        """A decode-only tick has no prompt movement; tokens_per_s_now must
        still report the real output rate rather than None."""
        s = D._window_stats(self.DECODING, 900)
        self.assertIsNotNone(s["tokens_per_s_now"])
        self.assertAlmostEqual(s["tokens_per_s_now"],
                               s["output_per_s_now"], delta=0.01)

    def test_idle_lane_reports_zero_not_a_stale_value(self):
        """Counter frozen for the whole window => measured 0, and the live
        keys must not carry a rate from a previous busy period."""
        idle = _ring([(2432, 1000, 2412, 8, 115.0)] * 6)
        s = D._window_stats(idle, 900)
        self.assertEqual(s["output_per_s_now"], 0.0)

    def test_restart_counter_reset_does_not_go_negative(self):
        """A counter reset (engine restart) must not report a negative rate."""
        restart = _ring([
            (2432, 1000, 2412, 8, 115.0),
            (10, 1000, 2412, 8, 115.0),
            (95, 1000, 2412, 8, 115.0),
            (180, 1000, 2412, 8, 115.0),
            (265, 1000, 2412, 8, 115.0),
            (350, 1000, 2412, 8, 115.0),
        ])
        s = D._window_stats(restart, 900)
        self.assertIsNone(s["output_per_s_now"],
                          "a reset must read as unmeasurable, not negative")
        self.assertIsNone(s["tokens_per_s_now"])

    def test_too_few_samples_yields_no_live_rate(self):
        """A ring younger than the live window has no span to divide by, so
        _window_stats returns before the live keys are even initialised."""
        young = _ring([(1997, 1000, 1812, 7, 100.0)])
        s = D._window_stats(young, 900)
        # Either the key is absent (early return) or explicitly None.
        self.assertIsNone(s.get("output_per_s_now"),
                          "must not invent a rate from a 1-point ring")


class TestPerRequestRateIsSeparate(unittest.TestCase):
    """The histogram keeps its own job: per-request speed, not aggregate."""

    DECODING = TestLiveRateUsesTheCounter.DECODING

    def test_seat_rate_uses_histogram_not_counter(self):
        """Once requests DO complete, per-request tok/s comes from the
        histogram. The aggregate live rate comes from the counter. They are
        different measurements and must not be conflated."""
        st = _ring([
            (1997, 1000, 1812, 7, 100.0),
            (2084, 1000, 1812, 7, 100.0),
            (2171, 1000, 1812, 7, 100.0),
            (2258, 1000, 1812, 7, 100.0),
            (2345, 1000, 1812, 7, 100.0),
            (2412, 1000, 2412, 8, 115.0),   # request completed: +600 gen
        ])
        rate, n = D._vllm_measured_seat_rate(st)
        self.assertIsNotNone(rate, "per-request rate must be measurable")
        # 600 output tokens over 15s of decode time
        self.assertAlmostEqual(rate, 600 / 15.0, delta=0.5)
        self.assertEqual(n, 1)

    def test_no_completions_yields_no_seat_rate(self):
        """Mid-decode with nothing finished: the per-REQUEST number is
        genuinely unmeasurable, and must be None rather than the aggregate."""
        rate, n = D._vllm_measured_seat_rate(self.DECODING)
        self.assertIsNone(rate)
        self.assertEqual(n, 0)

    def test_vllm_never_claims_a_seat_identity(self):
        """Per-request rate is real, but vLLM still has no seat axis."""
        st = self.DECODING
        L = D._live_slot_state(st)
        self.assertNotEqual(L.get("src"), "slot")


class TestMeasuredAgainstTheEngine(unittest.TestCase):
    """Live-lane check. Skipped unless the brain lane is actually serving."""

    # The live rate is a 10s trailing window. It starts from whatever the
    # previous (idle) state was, so the first ~2 polls of a new burst
    # legitimately under-read while the window fills. Measured: polls 0-1
    # read 16.0 and 28.3 against a true 47; poll 2 onward tracks to 2-10%.
    WARMUP_POLLS = 3

    @unittest.skipUnless(os.environ.get("DASH_LIVE_TPS") == "1",
                         "set DASH_LIVE_TPS=1 to run against the live lane")
    def test_live_rate_matches_engine(self):
        import json
        import subprocess
        import urllib.request

        def gen_total():
            with urllib.request.urlopen(
                    "http://127.0.0.1:8001/metrics", timeout=10) as r:
                for line in r.read().decode().splitlines():
                    if line.startswith("vllm:generation_tokens_total{"):
                        return float(line.rsplit(" ", 1)[1])
            return None

        def ui():
            with urllib.request.urlopen(
                    "http://127.0.0.1:9000/api/metrics", timeout=20) as r:
                m = json.loads(r.read().decode())
            for e in m["engines"]:
                if e["port"] == 8001:
                    s = e.get("stats") or {}
                    return (s.get("output_per_s_now"),
                            (e.get("slot_live") or {}).get("run"))
            return None, None

        proc = subprocess.Popen(
            [sys.executable, "-c",
             'import json,urllib.request\n'
             'b=json.dumps({"model":"Qwen/Qwen3.8-Flash-Next-Uncensored",'
             '"prompt":"Write a detailed essay on transistor physics.",'
             '"max_tokens":600,"temperature":0.7,"stream":False}).encode()\n'
             'r=urllib.request.Request("http://127.0.0.1:8001/v1/completions",'
             'data=b,headers={"Content-Type":"application/json"})\n'
             'print(json.loads(urllib.request.urlopen(r,timeout=300)'
             '.read())["usage"]["completion_tokens"])\n'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        time.sleep(2)
        g0, t0 = gen_total(), time.time()
        errs = []
        for i in range(24):
            time.sleep(2)
            g1, t1 = gen_total(), time.time()
            now, run = ui()
            dt = t1 - t0
            true = (g1 - g0) / dt if dt else 0
            # Score only the STEADY state of a decode:
            #   - true > 5            : the engine is actually generating
            #   - run > 0             : the request is still in flight
            #   - i >= WARMUP_POLLS   : the 10s live window has filled (it
            #     starts from the previous idle state, so the first couple of
            #     polls legitimately under-read while it fills)
            # A trailing-window rate decaying after the request ENDS is
            # correct behaviour, not divergence.
            if (true > 5 and i >= self.WARMUP_POLLS and (run or 0) > 0
                    and now is not None):
                errs.append(abs(now - true) / true)
            g0, t0 = g1, t1
            if proc.poll() is not None and i > 2:
                break
        proc.stdout.read()
        self.assertTrue(errs, "no scored samples — lane never went busy")
        self.assertLess(max(errs), 0.30,
                        f"UI diverges from the engine: {errs}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
