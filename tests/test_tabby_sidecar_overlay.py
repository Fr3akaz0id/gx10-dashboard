#!/usr/bin/env python3
"""Overlay synthetics must be sum_all-readable (ledger crediting guard).

Regression: synthetic counter entries built with {value, labels} only are
invisible to promparse.sum_all (it iterates the 'series' list), so
_lifetime_token_counters returned (None, None) and TOKENS BY MODEL froze
while the sidecar counters grew. This test pins the contract.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dashboard
import promparse

SIDE = {"counters": {
    "tabby_tokens_total": {"value": 100.0, "labels": {}, "series": [
        {"value": 30.0, "labels": {"type": "prompt"}},
        {"value": 70.0, "labels": {"type": "completion"}}]},
    "tabby_cache_tokens_total": {"value": 5.0, "labels": {}},
    "tabby_requests_total": {"value": 9.0, "labels": {}},
    "tabby_spec_draft_tokens_total": {"value": 12.0, "labels": {}},
    "tabby_spec_accepted_tokens_total": {"value": 11.0, "labels": {}},
}, "gauges": {
    "tabby_active_requests": {"value": 2.0, "labels": {}},
    "tabby_tps_gauge": {"value": 42.0, "labels": {}},
}, "histograms": {}}

parsed = dashboard._overlay_tabby_sidecar(
    dashboard._with_exl3_aliases(None), SIDE)

for name in ("vllm:prompt_tokens_total", "vllm:generation_tokens_total",
             "vllm:prefix_cache_hits_total", "vllm:prefix_cache_queries_total",
             "vllm:num_requests_total", "vllm:spec_decode_num_draft_tokens_total",
             "vllm:spec_decode_num_accepted_tokens_total"):
    v = promparse.sum_all(parsed, "counters", name)
    assert v is not None, f"{name} unreadable by sum_all (ledger would freeze)"
assert promparse.sum_all(parsed, "gauges", "vllm:num_requests_running") == 2.0
assert promparse.sum_all(parsed, "gauges", "exl3:live_tps_gauge") == 42.0

# the exact ledger read path
st = {"samples": [(0.0, parsed)]}
it, ot = dashboard._lifetime_token_counters(st)
assert it == 30.0 and ot == 70.0, f"ledger counters wrong: {it} {ot}"
print("OK")