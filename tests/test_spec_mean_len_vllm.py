"""vLLM spec-decode mean accepted length (tok/step) derived from counters.

WHY: the SPEC card's acceptance % is accepted/drafted, which distorts with
draft depth — a dynamic-depth engine (v5.2 recipe: depth 3..7) drafts more
low-survival positions and reads a LOWER % while emitting the same tokens
per verify step. mean accepted length is the number that maps to decode
speedup, and vLLM logs exactly that ("Mean acceptance length: 2.58") — but
publishes no gauge for it on /metrics.

DERIVATION: every verify step emits at least the bonus token from the
target model's own forward pass, so
    mean_len = 1 + accepted_tokens / verify_steps
and vllm:spec_decode_num_drafts_total IS the step count. Verified against
the live lane 2026-10-05: counters accepted 9367 / drafts 4977 -> 2.88,
journal reported 2.5-3.1 over the same period.

HONESTY (seat-honesty rule, 2026-09-27): no drafts in the window or a
counter reset yields None, never 0 — an idle lane must not read as
"mean_len 1.0" (that would claim a measured step that never happened).

Run: python3 tests/test_spec_mean_len_vllm.py
"""
import sys, time
sys.path.insert(0, __file__.rsplit("/", 1)[0].rsplit("/", 1)[0])
import _bootstrap  # noqa: F401  (must precede dashboard: isolates log + DB)
from _bootstrap import D
NOW = time.time()


def _spec_sample(drafts, draft_toks, accepted):
    """A vLLM-shaped parsed sample carrying only the three spec counters
    (plus the running gauge _window_stats reads unconditionally)."""
    return {
        "gauges": {"vllm:num_requests_running": {"value": 0.0, "labels": {}},
                   "vllm:num_requests_waiting": {"value": 0.0, "labels": {}}},
        "counters": {
            "vllm:spec_decode_num_drafts_total": {"value": float(drafts), "labels": {}},
            "vllm:spec_decode_num_draft_tokens_total": {"value": float(draft_toks), "labels": {}},
            "vllm:spec_decode_num_accepted_tokens_total": {"value": float(accepted), "labels": {}},
        },
        "histograms": {},
    }


def _st(pairs):
    return {"samples": sorted([(NOW - off, p) for off, p in pairs],
                              key=lambda x: x[0]),
            "slot_cap": 4}


def test_mean_len_is_one_plus_accepted_over_steps():
    """accepted +900 over 360 steps -> 1 + 2.5 = 3.5 tok/step.
    Drafted 2520 tokens -> acceptance 35.7% — note how the % and the
    tok/step tell different stories about the SAME window; only tok/step
    is depth-invariant."""
    ring = _st([(18.0, _spec_sample(1000, 5000, 1500)),
                (2.0,  _spec_sample(1360, 7520, 2400))])
    res = D._window_stats(ring, 60)
    assert res["spec_mean_len"] == 3.5, res["spec_mean_len"]
    assert res["spec_acceptance"] == 35.7, res["spec_acceptance"]


def test_idle_window_is_none_not_zero():
    """Flat counters = no verify step happened. None, never 0 and never
    1.0 (1.0 would claim every step rejected everything — a measurement
    the window never made)."""
    s = _spec_sample(5000, 25000, 9000)
    ring = _st([(18.0, s), (10.0, s), (2.0, _spec_sample(5000, 25000, 9000))])
    res = D._window_stats(ring, 60)
    assert res.get("spec_mean_len") is None, res.get("spec_mean_len")


def test_counter_reset_is_none():
    """Engine restart mid-window: counters go DOWN. _delta returns None on
    reset, so mean_len must be None rather than a negative nonsense."""
    ring = _st([(18.0, _spec_sample(9000, 45000, 18000)),
                (2.0,  _spec_sample(120, 600, 250))])   # restarted: lower
    res = D._window_stats(ring, 60)
    assert res.get("spec_mean_len") is None, res.get("spec_mean_len")


def test_engine_spec_carries_mean_len_on_metrics_path():
    """_engine_spec's vLLM branch must forward stats.spec_mean_len into
    spec.mean_len — the frontend renders tok/step from that field for the
    journal path already; the metrics path was the only one dropping it."""
    import re
    src = open(D.__file__.replace(".pyc", ".py")).read()
    m = re.search(r'return \{"acceptance": acc, "tech": m\.group\(1\).*?'
                  r'"mean_len": ([^,]+),', src, re.S)
    assert m, "vLLM branch of _engine_spec: mean_len field not found"
    assert "spec_mean_len" in m.group(1), (
        "vLLM branch must forward the derived value, got: %r" % m.group(1))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("all spec-mean-len tests passed")
