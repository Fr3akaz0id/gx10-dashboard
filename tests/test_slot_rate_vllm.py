"""vLLM measured per-seat rate: the SLOTS card stamps a MEASURED per-request
tok/s on busy seats (from the request completion histograms diffed over a
trailing window) instead of the aggregate÷running estimate, while a quiet
vLLM lane (or sglang/ds4) keeps the estimate path. Pure in-memory: no
network, no DB, no side effects."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dashboard as D

W = D.VLLM_SLOT_RATE_WINDOW_S


def _hist(gen_sum, dec_sum, n=3, running=1.0):
    """A vLLM-shaped sample: the two completion histograms + running/queue."""
    return {"gauges": {
                "vllm:num_requests_running": {"value": running, "labels": {}},
                "vllm:num_requests_waiting": {"value": 0.0, "labels": {}},
              },
              "counters": {},
              "histograms": {
                "vllm:request_generation_tokens":
                    {"buckets": [(float("inf"), float(n))], "sum": gen_sum,
                     "count": float(n)},
                "vllm:request_decode_time_seconds":
                    {"buckets": [(float("inf"), float(n))], "sum": dec_sum,
                     "count": float(n)},
              }}


def _ring(samples):
    """samples = [(ts, parsed), ...] oldest→newest (a scrape-ring shape)."""
    return list(samples)


def _st(samples, cap=4):
    return {"samples": samples, "slot_cap": cap, "slot_tps": {}}


def _steady_ring(start_gen, start_dec, per_tick, ticks=10, step=2.0, running=1.0):
    """Ring where each tick completes requests adding `per_tick` (gen, dec)."""
    out, g, d = [], start_gen, start_dec
    for i in range(ticks):
        if i:
            g += per_tick[0]; d += per_tick[1]
        out.append((1000.0 + i * step, _hist(g, d, n=3, running=running)))
    return out


def test_measured_over_completed_requests():
    # window spans ~18s; 600 tok over 20s decode across the window = 30/s.
    ring = _ring([
        (1000.0, _hist(1000.0, 30.0, n=1)),          # window start
        (1004.0, _hist(1300.0, 40.0, n=2)),
        (1009.0, _hist(1600.0, 50.0, n=3)),          # newest
    ])
    tps, n = D._vllm_measured_seat_rate(_st(ring))
    assert tps == 30.0 and n == 2, (tps, n)
    ls = D._live_slot_state(_st(ring))
    assert ls["src"] == "req" and ls["busy"] == [30.0], ls


def test_stays_lit_under_sustained_decode():
    # The regressed behaviour: completions are sparse per-tick but continuous
    # over the window. A tick with no NEW completion must STILL show the
    # measured rate (not drop to None/estimate), so the card doesn't strobe.
    ring = _ring([
        (1000.0, _hist(1000.0, 40.0, n=2)),          # window start, gen/dec=25
        (1002.0, _hist(1000.0, 40.0, n=2)),          # no completion this tick
        (1004.0, _hist(1000.0, 40.0, n=2)),          # still none
        (1008.0, _hist(1200.0, 48.0, n=4)),          # newest: +200 / +8 = 25
    ])
    tps, n = D._vllm_measured_seat_rate(_st(ring))
    assert tps == 25.0 and n == 2, (tps, n)


def test_truly_quiet_lane_falls_back_to_estimate():
    # No completions anywhere across the whole window -> nothing to stamp.
    ring = _ring([
        (1000.0, _hist(5000.0, 150.0, n=9)),
        (1003.0, _hist(5000.0, 150.0, n=9)),
        (1006.0, _hist(5000.0, 150.0, n=9)),
    ])
    assert D._vllm_measured_seat_rate(_st(ring)) == (None, 0)
    ls = D._live_slot_state(_st(ring))
    assert ls["src"] is None and ls["busy"] == [None], ls


def test_too_thin_window_rejected():
    # Only one sample inside the window (span < 1s) is not trustworthy.
    ring = _ring([(1000.0, _hist(1000.0, 40.0)),
                  (1000.5, _hist(1200.0, 48.0))])
    assert D._vllm_measured_seat_rate(_st(ring)) == (None, 0)


def test_restart_reset_rejected():
    # counters went backwards across the window (engine restart).
    ring = _ring([
        (1000.0, _hist(9000.0, 300.0, n=12)),
        (1004.0, _hist(300.0, 9.0, n=1)),
        (1008.0, _hist(360.0, 11.0, n=2)),
    ])
    assert D._vllm_measured_seat_rate(_st(ring)) == (None, 0)


def test_no_histograms_is_not_vllm():
    # sglang/ds4-style sample with no request_* histograms -> estimate path.
    s = {"gauges": {"vllm:num_requests_running": {"value": 1.0, "labels": {}}},
         "counters": {}, "histograms": {}}
    ring = _ring([(1000.0, s), (1004.0, s), (1008.0, s)])
    assert D._vllm_measured_seat_rate(_st(ring)) == (None, 0)
    assert D._live_slot_state(_st(ring))["src"] is None


def test_llama_per_slot_wins():
    # a real /slots rate is authoritative even alongside completion histograms.
    ring = _ring([
        (1000.0, _hist(0.0, 0.0, n=0, running=2.0)),
        (1004.0, _hist(0.0, 0.0, n=0, running=2.0)),
        (1008.0, _hist(200.0, 8.0, n=2, running=2.0)),   # would imply 25
    ])
    st = _st(ring, cap=4)
    st["slot_tps"] = {"0": 41.0, "1": 38.0}
    ls = D._live_slot_state(st)
    assert ls["src"] == "slot" and ls["busy"] == [41.0, 38.0], ls


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"ok {name}")
    print("all vLLM per-seat rate tests passed")
