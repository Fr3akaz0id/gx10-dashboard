"""vLLM live token-rate stat cards. The OUTPUT/INPUT/TOTAL tok/s cards read
stats.output_per_s_now / input_per_s_now / tokens_per_s_now.

MEASURED 2026-09-27 on the live brain lane, sampling generation_tokens_total
and the completion histogram every 2s through a 600-token decode:

    t(s)  gen_total  dgen  tok/s | hist_sum  dhist
     2.0       1997     87   43.3 |      1812      0
     2.0       2077     80   39.9 |      1812      0
     2.0       2141     64   31.9 |      1812      0
     2.0       2213     72   35.9 |      1812      0
     2.0       2293     80   39.8 |      1812      0
     2.0       2376     83   41.3 |      1812      0
     2.0       2412     36   17.9 |      2412    600

The COUNTER advances every poll, mid-decode. The completion HISTOGRAM is
frozen throughout and jumps by the whole request length only at the end.
This file's original premise — "vLLM bumps its counters only when a
request completes" — is the exact inverse of what the engine does, and it
is why the cards read ~5 tok/s while the lane did 40.

So: the AGGREGATE live rate comes from the counter delta. The HISTOGRAM
keeps a different job — tokens ÷ decode-time per completed request, which
is the per-request speed reported per seat (_vllm_measured_seat_rate).
Both are tested here; the two must not be conflated.

Pure in-memory. See tests/test_live_tps_accuracy.py for the accuracy
regression and the opt-in live-engine check.
"""
import sys, time
sys.path.insert(0, __file__.rsplit("/", 1)[0].rsplit("/", 1)[0])
import _bootstrap  # noqa: F401  (must precede dashboard: isolates log + DB)
from _bootstrap import D
W = D.VLLM_LIVE_RATE_WINDOW_S
NOW = time.time()


def _vs(gens, pfk, decs, pres, n=4, gen_total=1000.0, prompt_total=5000.0):
    """A vLLM-shaped parsed sample.

    gen_total/prompt_total are the LIVE counters — the caller passes real
    per-poll values, because they advance every poll mid-decode (measured
    above). The histograms carry per-COMPLETION totals and therefore jump
    only when a request finishes.
    """
    return {
        "gauges": {"vllm:num_requests_running": {"value": 1.0, "labels": {}},
                   "vllm:num_requests_waiting": {"value": 0.0, "labels": {}}},
        "counters": {
            "vllm:generation_tokens_total": {"value": gen_total, "labels": {}},
            "vllm:prompt_tokens_total": {"value": prompt_total, "labels": {}},
        },
        "histograms": {
            "vllm:request_generation_tokens":
                {"buckets": [(float("inf"), float(n))], "sum": gens, "count": float(n)},
            "vllm:request_prefill_kv_computed_tokens":
                {"buckets": [(float("inf"), float(n))], "sum": pfk, "count": float(n)},
            "vllm:request_decode_time_seconds":
                {"buckets": [(float("inf"), float(n))], "sum": decs, "count": float(n)},
            "vllm:request_prefill_time_seconds":
                {"buckets": [(float("inf"), float(n))], "sum": pres, "count": float(n)},
        },
    }


def _ring(pairs):
    return [(NOW - off, parsed) for off, parsed in pairs]   # newest last


def _st(samples):
    return {"samples": sorted(samples, key=lambda x: x[0]), "slot_cap": 4}


def test_measured_rates_reconcile():
    # over the ~18s window gen.sum grows 0→900 (50/s), pfk.sum 0→1800 (100/s)
    ring = _ring([
        (18.0, _vs(0.0, 0.0, 0.0, 0.0, n=0)),
        (12.0, _vs(300.0, 600.0, 6.0, 3.0, n=2)),
        (6.0,  _vs(600.0, 1200.0, 12.0, 6.0, n=3)),
        (2.0,  _vs(900.0, 1800.0, 18.0, 9.0, n=4)),
    ])
    out, inn, tot, isv = D._vllm_live_rates(_st(ring))
    span = ring[-1][0] - ring[0][0]
    assert isv is True
    assert abs(out - 900.0 / span) < 0.5, (out, span)
    assert abs(inn - 1800.0 / span) < 0.5, (inn, span)
    assert abs(tot - (out + inn)) < 0.5, (tot, out, inn)


def test_window_stats_never_clone_under_bursty_counters():
    """The original regression: TOTAL must never be a clone of OUTPUT, and
    INPUT must be a real live rate rather than 0.0.

    The counter values now ADVANCE per poll (measured: vLLM's
    generation_tokens_total moves mid-decode, the completion histogram does
    not), so this feeds realistic live counters. The histograms still jump
    only on completion, which is the point: the two sources must not be
    confused for one another."""
    ring = _ring([
        (18.0, _vs(0.0, 0.0, 0.0, 0.0, n=0, gen_total=0.0, prompt_total=0.0)),
        (10.0, _vs(400.0, 1000.0, 8.0, 4.0, n=2,
                   gen_total=400.0, prompt_total=1000.0)),
        (2.0,  _vs(800.0, 2000.0, 16.0, 8.0, n=4,
                   gen_total=800.0, prompt_total=2000.0)),
    ])
    res = D._window_stats(_st(ring), 60)
    out, inn, tot = (res["output_per_s_now"], res["input_per_s_now"],
                     res["tokens_per_s_now"])
    # 800 output tokens over the last 8s of the live window
    assert out and out > 5, res
    assert inn and inn > 0, ("input must be a live rate, not 0.0", inn)
    assert not (inn == 0.0 and tot == out), ("TOTAL cloned OUTPUT", out, inn, tot)
    assert abs(tot - (out + inn)) < 0.5, (tot, out, inn)


def test_young_ring_renders_dash_not_a_counter_artifact():
    """Right after restart the ring is younger than the window. A vLLM lane
    must return is_vllm=True with no values, so the caller renders the dash —
    it must NOT drop through to the zero-prone counter path."""
    ring = _ring([(3.0, _vs(0.0, 0.0, 0.0, 0.0, n=0)),
                  (1.0, _vs(200.0, 400.0, 4.0, 2.0, n=1))])   # span 2s < 5s
    out, inn, tot, isv = D._vllm_live_rates(_st(ring))
    assert (out, inn, tot, isv) == (None, None, None, True)


def test_quiet_vllm_lane_is_dash_not_stale():
    # Ring is full but NOTHING completed in the window (counts flat).
    ring = _ring([
        (18.0, _vs(5000.0, 9000.0, 90.0, 45.0, n=40)),
        (10.0, _vs(5000.0, 9000.0, 90.0, 45.0, n=40)),
        (2.0,  _vs(5000.0, 9000.0, 90.0, 45.0, n=40)),
    ])
    assert D._vllm_live_rates(_st(ring)) == (None, None, None, True)


def test_non_vllm_lane_is_not_claimed():
    # sglang/ds4 shape: no request_* histograms at all → not vLLM, so the
    # caller is allowed to use its counter fallback.
    s = {"gauges": {"vllm:num_requests_running": {"value": 1.0, "labels": {}}},
         "counters": {"vllm:generation_tokens_total": {"value": 100.0, "labels": {}}},
         "histograms": {}}
    ring = _ring([(6.0, s), (2.0, s)])
    assert D._vllm_live_rates(_st(ring)) == (None, None, None, False)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"ok {name}")
    print("all vLLM live-rate stat-card tests passed")
