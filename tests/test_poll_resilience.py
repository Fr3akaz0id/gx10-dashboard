"""Regression tests for the poll-loop resilience fixes (2026-09-27).

1. One engine's derive failure must NOT starve the engines after it.
   Previously _model_identity/_update_slots/_slot_capacity ran unguarded
   inside the fleet-wide loop, so the first throw aborted every LATER engine
   and collect() swallowed it -- tiles just kept serving stale values.
2. A ledger flush failure must be RETRIED, not skipped: _last_ledger_flush
   used to be advanced BEFORE the work, so a throw meant the next attempt
   waited another 30s and a repeat offender froze the token meters forever.

Runs in-process against the real module with a synthetic config; never
touches the live metrics.db or any real engine.

Run: python3 tests/test_poll_resilience.py
"""
import os
import sys
import time
import types

import _bootstrap  # noqa: F401  (must precede dashboard: isolates log + DB)
from _bootstrap import D


FAILS = []


def ok(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)


# ---------------------------------------------------------------- test 1
print("1. one engine's derive failure does not starve the others")


def run_scrape_with_boom(boom_port, ports):
    """Drive scrape_engines' per-engine derive block with an injected
    failure on boom_port, and report which ports got a sample."""
    got = {}

    def fake_identity(port, st):
        if port == boom_port:
            raise RuntimeError("simulated derive failure")
        return {"key": f"m\x00?\x00vllm:{port}", "model": "m",
                "version": None, "engine": f"vllm:{port}"}

    orig_ident = D._model_identity
    orig_scrape_http = D.urllib.request.urlopen

    class FakeResp:
        def __init__(self, body):
            self._b = body

        def read(self):
            return self._b

    def fake_urlopen(url, timeout=None):
        u = str(url)
        for p in ports:
            if f":{p}/metrics" in u:
                return FakeResp(
                    b"# TYPE llamacpp:prompt_tokens_total counter\n"
                    b"llamacpp:prompt_tokens_total 100\n"
                    b"# TYPE vllm:generation_tokens_total counter\n"
                    b"vllm:generation_tokens_total 10\n")
        raise OSError("no route")

    D._model_identity = fake_identity
    D.urllib.request.urlopen = fake_urlopen
    try:
        D.scrape_engines()
    finally:
        D._model_identity = orig_ident
        D.urllib.request.urlopen = orig_scrape_http
    for p in ports:
        st = D.eng_metrics["engines"].get(p, {})
        got[p] = len(st.get("samples", []))
    return got


PORTS = [9101, 9102, 9103]   # boom first, like a leading config entry
saved = {p: D.eng_metrics["engines"].get(p) for p in PORTS}
for p in PORTS:
    D.eng_metrics["engines"].pop(p, None)

# scrape_engines iterates the CONFIGURED engines, so point it at a synthetic
# config. Everything else (the lock, the loop, the derive block) is the real
# code path -- only the engine list is faked.
import catalog
SYNTH_CFG = {"engines": [{"kind": "unit", "name": f"synth{p}.service",
                          "port": p, "enabled": True} for p in PORTS]}
_orig_read_config = catalog.read_config
catalog.read_config = lambda: ({}, SYNTH_CFG, None)
D.catalog = catalog
# _engine_proc does a full /proc scan per port; stub it out.
_orig_proc = D._engine_proc
D._engine_proc = lambda port: None

try:
    before = run_scrape_with_boom(9101, PORTS)
    ok(before.get(9101, 0) >= 1, f"the failing engine still got its sample ({before})")
    ok(before.get(9102, 0) >= 1,
       f"engine AFTER the failure still got a sample ({before})")
    ok(before.get(9103, 0) >= 1,
       f"last engine still got a sample ({before})")
    err = D.eng_metrics["engines"][9101].get("derive_error")
    ok(err is not None and "simulated" in str(err),
       f"the failure is recorded, not swallowed ({err!r})")
    ok(D.eng_metrics["engines"][9102].get("derive_error") is None,
       "healthy engines carry no derive_error")

    # and it must RECOVER once the underlying fault clears
    after = run_scrape_with_boom(99999, PORTS)   # nothing throws now
    ok(D.eng_metrics["engines"][9101].get("derive_error") is None,
       "derive_error clears once the fault is gone")
    ok(after.get(9103, 0) > before.get(9103, 0), "samples keep accumulating")
finally:
    catalog.read_config = _orig_read_config
    D._engine_proc = _orig_proc
    for p in PORTS:
        D.eng_metrics["engines"].pop(p, None)
    for p, v in saved.items():
        if v is not None:
            D.eng_metrics["engines"][p] = v

# ---------------------------------------------------------------- test 2
print("\n2. a failed ledger flush is retried, not skipped 30s later")


class FakeConn:
    """Stands in for the sqlite connection. The metadb write helpers are
    patched out, so this only needs to exist as an object."""


_LEDGER_STATE = {"ledger": 0, "fail": True}


def run_flush():
    """Drive one _db_maybe_write cycle. The ledger_update stub throws the
    FIRST time it does real work and succeeds after that -- the state is
    module-level so it survives across calls, which is what makes the retry
    observable."""
    state = {"samples": 0}

    def fake_write_sample(c, row):
        state["samples"] += 1

    def fake_write_gpu(c, row):
        pass

    def fake_backfill(c, ts):
        pass

    def fake_ledger_update(c, port, it, ot, pw, ts=None):
        if _LEDGER_STATE["fail"] and port != -1:
            _LEDGER_STATE["fail"] = False
            raise RuntimeError("simulated ledger failure")
        _LEDGER_STATE["ledger"] += 1

    orig = (D.metadb.write_sample, D.metadb.write_gpu,
            D.metadb.ledger_backfill, D.metadb.ledger_update)
    D.metadb.write_sample = fake_write_sample
    D.metadb.write_gpu = fake_write_gpu
    D.metadb.ledger_backfill = fake_backfill
    D.metadb.ledger_update = fake_ledger_update
    try:
        D._db_maybe_write({"power_w": 100.0})
    finally:
        (D.metadb.write_sample, D.metadb.write_gpu,
         D.metadb.ledger_backfill, D.metadb.ledger_update) = orig
    return state["samples"], _LEDGER_STATE["ledger"]


saved_conn, saved_flush = D._db_conn, D._last_ledger_flush
D._db_conn = FakeConn()
D._last_ledger_flush = None
try:
    # one engine with metrics, so the ledger block has work to do
    mk = lambda v: {"value": v, "series": [{"value": v, "labels": {}}]}
    st = {"has_metrics": True,
          "samples": [(time.time(), {
              "counters": {"vllm:prompt_tokens_total": mk(100.0),
                           "vllm:generation_tokens_total": mk(10.0)},
              "gauges": {}, "histograms": {}})],
          "model_identity": {"key": "m\x00?\x00vllm:1", "model": "m",
                             "version": None, "engine": "vllm:1"}}
    old = D.eng_metrics["engines"].get(1)
    D.eng_metrics["engines"][1] = st
    try:
        s1, l1 = run_flush()
        ok(s1 == 1, f"write_sample ran before the ledger block (got {s1})")
        ok(l1 == 0, f"the failing attempt credited nothing (got {l1})")
        ok(D._last_ledger_flush is None,
           "flush timestamp NOT advanced by the failed attempt")
        # the very next cycle must retry immediately rather than wait 30s
        s2, l2 = run_flush()
        ok(l2 >= 1, f"the retry actually credited the ledger ({l2} calls)")
        ok(D._last_ledger_flush is not None,
           "timestamp advances only after a successful flush")
    finally:
        if old is not None:
            D.eng_metrics["engines"][1] = old
        else:
            D.eng_metrics["engines"].pop(1, None)
finally:
    D._db_conn, D._last_ledger_flush = saved_conn, saved_flush

print()
if FAILS:
    print(f"FAILED {len(FAILS)} check(s):")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("OK - poll-loop resilience holds")
