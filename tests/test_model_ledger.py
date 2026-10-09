"""Model-ledger credit-semantics tests (v4 composite key).

Covers the attribution rules: delta on same key, growth-only on key change
without reset, full-credit+initial-flag on reset+key change and on first
observation, and the observed/initial split in model_ledger_all.
In-memory DB, no dashboard imports needed for the metadb half; the
dashboard identity helpers are tested with faked proc/args.
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import metadb


def fresh():
    c = metadb.connect(":memory:")
    c.executescript(metadb.SCHEMA)
    return c


K = lambda model, version, engine: "\u0000".join([model, version or "?", engine])


# 1. first-ever observation: lifetime counter CREDITED, NOT flagged initial
#    (rule changed 2026-10-06 by 4a6e06c: the engine counter is a PROCESS
#    lifetime and one process serves one model, so the pre-dashboard tokens
#    ARE attributable — initial marks only unattributable history, i.e. a
#    counter reset coinciding with a key change).
c = fresh()
metadb.model_ledger_update(c, 8003, K("qwen3.8-27b", "recipe", "sglang:8003"),
                           "qwen3.8-27b", "recipe", "sglang:8003",
                           7_000_000.0, 100_000.0, ts=1000)
rows = metadb.model_ledger_all(c)
assert len(rows) == 1, rows
r = rows[0]
assert (r["in_tokens"], r["out_tokens"]) == (7_000_000, 100_000), r
assert (r["in_initial"], r["out_initial"]) == (0, 0), r
assert (r["observed_in"], r["observed_out"]) == (7_000_000, 100_000), r
assert r["version"] == "recipe" and r["engine"] == "sglang:8003", r
c.close()

# 2. same key: only the delta is credited, NOT flagged
c = fresh()
k1 = K("m", "v1", "sglang:8003")
metadb.model_ledger_update(c, 8003, k1, "m", "v1", "sglang:8003", 1000.0, 50.0, ts=1000)
metadb.model_ledger_update(c, 8003, k1, "m", "v1", "sglang:8003", 1500.0, 90.0, ts=1030)
r = metadb.model_ledger_all(c)[0]
# first observation is observed (not initial) since 4a6e06c, so initial=0
# and observed covers the whole credited history.
assert (r["in_tokens"], r["out_tokens"]) == (1500, 90), r
assert (r["in_initial"], r["out_initial"]) == (0, 0), r
assert (r["observed_in"], r["observed_out"]) == (1500, 90), r
c.close()

# 3. KEY CHANGE without reset: only growth since previous watermark credited
#    to the new key — NOT the whole lifetime counter. This is the bug fix.
c = fresh()
k_old = K("qwen3.8-27b", "old-recipe", "sglang:8003")
k_new = K("qwen3.8-27b", "new-recipe", "sglang:8003")
metadb.model_ledger_update(c, 8003, k_old, "qwen3.8-27b", "old-recipe", "sglang:8003",
                           5_000_000.0, 50_000.0, ts=1000)
metadb.model_ledger_update(c, 8003, k_new, "qwen3.8-27b", "new-recipe", "sglang:8003",
                           5_600_000.0, 56_000.0, ts=1030)
bykey = {r["key"]: r for r in metadb.model_ledger_all(c)}
assert len(bykey) == 2, bykey.keys()
assert (bykey[k_old]["in_tokens"], bykey[k_old]["out_tokens"]) == (5_000_000, 50_000)
# new key got the growth only: 600k in / 6k out — not 5.6M
assert (bykey[k_new]["in_tokens"], bykey[k_new]["out_tokens"]) == (600_000, 6_000), bykey[k_new]
assert bykey[k_new]["in_initial"] == 0 and bykey[k_new]["out_initial"] == 0
assert bykey[k_new]["observed_in"] == 600_000
c.close()

# 4. reset WITH key change: new reading credited, flagged initial
#    (pre-restart counter can't be attributed to the new key)
c = fresh()
k_old = K("a", "v1", "llama:8888")
k_new = K("b", "v2", "llama:8888")
metadb.model_ledger_update(c, 8888, k_old, "a", "v1", "llama:8888", 9_000_000.0, 100.0, ts=1000)
metadb.model_ledger_update(c, 8888, k_new, "b", "v2", "llama:8888", 300_000.0, 30.0, ts=1030)
bykey = {r["key"]: r for r in metadb.model_ledger_all(c)}
assert (bykey[k_new]["in_tokens"], bykey[k_new]["out_tokens"]) == (300_000, 30)
assert (bykey[k_new]["in_initial"], bykey[k_new]["out_initial"]) == (300_000, 30)
assert (bykey[k_new]["observed_in"], bykey[k_new]["observed_out"]) == (0, 0)
# old key untouched by the restart
assert bykey[k_old]["in_tokens"] == 9_000_000
c.close()

# 5. reset WITHOUT key change: new reading credited, NOT flagged
#    (initial is 0: same key + attributable first observation since 4a6e06c)
c = fresh()
k = K("m", "v1", "ds4:8000")
metadb.model_ledger_update(c, 8000, k, "m", "v1", "ds4:8000", 500_000.0, 10.0, ts=1000)
metadb.model_ledger_update(c, 8000, k, "m", "v1", "ds4:8000", 20_000.0, 2.0, ts=1030)
r = metadb.model_ledger_all(c)[0]
assert (r["in_tokens"], r["out_tokens"]) == (520_000, 12), r
assert (r["in_initial"], r["out_initial"]) == (0, 0), r
assert (r["observed_in"], r["observed_out"]) == (520_000, 12), r
c.close()

# 6. two engines, same model base: separate rows (the whole point)
c = fresh()
k_s = K("qwen3.8-27b", None, "sglang:8003")
k_l = K("qwen3.8-27b", None, "llama:8888")
metadb.model_ledger_update(c, 8003, k_s, "qwen3.8-27b", None, "sglang:8003", 1_000_000.0, 1.0, ts=1000)
metadb.model_ledger_update(c, 8888, k_l, "qwen3.8-27b", None, "llama:8888", 2_000_000.0, 2.0, ts=1000)
rows = metadb.model_ledger_all(c)
assert len(rows) == 2 and {r["engine"] for r in rows} == {"sglang:8003", "llama:8888"}
# per-port watermarks are independent
wm = {r["port"]: (r["key"], r["in_tokens_max"])
      for r in c.execute("SELECT port, key, in_tokens_max FROM model_watermarks")}
assert wm[8003][1] == 1_000_000 and wm[8888][1] == 2_000_000, wm
c.close()

# 7. first_ts sticks; last_ts advances
c = fresh()
k = K("m", "v", "sglang:8003")
metadb.model_ledger_update(c, 8003, k, "m", "v", "sglang:8003", 10.0, 1.0, ts=1000)
metadb.model_ledger_update(c, 8003, k, "m", "v", "sglang:8003", 15.0, 2.0, ts=9000)
r = metadb.model_ledger_all(c)[0]
assert (r["first_ts"], r["last_ts"]) == (1000, 9000), r
c.close()

# 8. None counters: no-op, state untouched (incl. last_ts — an idle/down
#    lane's row must NOT heartbeat, or the statistics page's 30-min
#    "on metrics" pill lies for a frozen-total lane)
c = fresh()
k = K("m", "v", "sglang:8003")
metadb.model_ledger_update(c, 8003, k, "m", "v", "sglang:8003", 10.0, 1.0, ts=1000)
metadb.model_ledger_update(c, 8003, k, "m", "v", "sglang:8003", None, None, ts=1030)
assert metadb.model_ledger_all(c)[0]["in_tokens"] == 10
assert metadb.model_ledger_all(c)[0]["last_ts"] == 1000, "None counters advanced last_ts"
c.close()

# 9. ZERO-Delta credit (an overlay fronting a dead engine serves /metrics
#    counters with zero traffic): tokens unchanged AND last_ts frozen.
#    last_ts may only advance when >0 tokens were actually credited —
#    otherwise every zero-flush stamps now and the 30-min "on metrics"
#    pill shows a frozen lane as live forever.
c = fresh()
k = K("frost", "v", "exl3:8899")
metadb.model_ledger_update(c, 8899, k, "frost", "v", "exl3:8899", 5_000_000.0, 5_000.0, ts=1000)
metadb.model_ledger_update(c, 8899, k, "frost", "v", "exl3:8899", 5_000_000.0, 5_000.0, ts=1030)
metadb.model_ledger_update(c, 8899, k, "frost", "v", "exl3:8899", 5_000_000.0, 5_000.0, ts=9000)
r = metadb.model_ledger_all(c)[0]
assert (r["in_tokens"], r["out_tokens"]) == (5_000_000, 5_000), r
assert (r["first_ts"], r["last_ts"]) == (1000, 1000), r
# traffic resumes -> last_ts advances again on the next credit
metadb.model_ledger_update(c, 8899, k, "frost", "v", "exl3:8899", 5_000_100.0, 5_000.0, ts=9100)
r = metadb.model_ledger_all(c)[0]
assert (r["first_ts"], r["last_ts"]) == (1000, 9100), r
# counter RESET that credits nothing (reading 0 after restart): no advance.
# A reset whose reading > 0 DID serve tokens -> advance (case 5's reset path).
metadb.model_ledger_update(c, 8899, k, "frost", "v", "exl3:8899", 0.0, 0.0, ts=9200)
r = metadb.model_ledger_all(c)[0]
assert (r["in_tokens"], r["out_tokens"]) == (5_000_100, 5_000), r
assert (r["first_ts"], r["last_ts"]) == (1000, 9100), r
c.close()

# ── dashboard identity helpers ────────────────────────────────
import _bootstrap  # noqa: F401  (must precede dashboard: isolates log + DB)
from _bootstrap import dashboard
# shard stripping
assert dashboard._strip_shards("Qwen3.8-Flash-Next-UD-Q4_K_XL-00001-of-00004") == "Qwen3.8-Flash-Next-UD-Q4_K_XL"
assert dashboard._strip_shards("plain-name") == "plain-name"
assert dashboard._strip_shards("x-00001-of-00004-00002-of-00004") == "x"

# backend detection depends on the live process/proc scan — pin it so the
# key assertions are environment-invariant
_orig_lb = dashboard._live_backend
try:
    dashboard._live_backend = lambda port, st, cfg: ("sglang", None)
    # composite key structure
    ident = dashboard._model_identity(8003, {"model_live": "qwen3.8-27b", "port": 8003})
    assert ident["model"] == "qwen3.8-27b", ident
    # The engine label is the FAMILY, not "family:port" -- the port is not
    # part of a model's identity (see tests/test_ledger_key_no_port.py). The
    # endpoint is tracked per-port in model_watermarks/ledger/samples.
    assert ident["engine"] == "sglang", ident
    assert ident["key"] == K("qwen3.8-27b", ident["version"], "sglang"), ident
    assert "8003" not in ident["key"], ident
    # llama.cpp: no model_live -> model_cmdline basename drives the base
    dashboard._live_backend = lambda port, st, cfg: ("llama", None)
    ident = dashboard._model_identity(8888, {"model_cmdline": "SomeModel-Q4_K_M.gguf", "port": 8888})
    assert ident["model"] == "SomeModel-Q4_K_M", ident
    assert ident["engine"] == "llama", ident
    assert "8888" not in ident["key"], ident
finally:
    dashboard._live_backend = _orig_lb

print("MODEL_LEDGER_TESTS_OK")
