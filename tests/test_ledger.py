"""Ledger tests: watermarks, restart resets, backfill baseline adoption,
series reconstruction. In-memory DB, no dashboard imports needed."""
import os, sys, time
sys.path.insert(0, "/opt/gx10-dashboard")
import metadb

c = metadb.connect(":memory:")
c.executescript(metadb.SCHEMA)

# 1. first flush: counter adopted as-is (no watermark, no prior row)
metadb.ledger_update(c, 8888, 1000.0, 500.0, 100.0, ts=1000)
r = metadb.ledger_latest(c)
assert (r["in_tokens"], r["out_tokens"]) == (1000, 500), r

# 2. second flush: delta credited
metadb.ledger_update(c, 8888, 1500.0, 900.0, 120.0, ts=1030)
r = metadb.ledger_latest(c)
assert (r["in_tokens"], r["out_tokens"]) == (1500, 900), r

# 3. engine restart: counter drops below watermark -> new reading is the delta
metadb.ledger_update(c, 8888, 30.0, 10.0, 110.0, ts=1060)
r = metadb.ledger_latest(c)
assert (r["in_tokens"], r["out_tokens"]) == (1530, 910), r
# and continues from the new counter
metadb.ledger_update(c, 8888, 80.0, 40.0, 105.0, ts=1090)
r = metadb.ledger_latest(c)
assert (r["in_tokens"], r["out_tokens"]) == (1580, 940), r

# 4. second port accumulates independently; energy is fleet-wide
metadb.ledger_update(c, 30000, 200.0, 50.0, 130.0, ts=1120)
r = metadb.ledger_latest(c)
assert (r["in_tokens"], r["out_tokens"]) == (1780, 990), r

# 5. down engine (None counters): tokens frozen, energy keeps integrating
e_before = r["energy_kwh"]
metadb.ledger_update(c, -1, None, None, 140.0, ts=1150)
r = metadb.ledger_latest(c)
assert (r["in_tokens"], r["out_tokens"]) == (1780, 990), r
assert r["energy_kwh"] > e_before, (e_before, r["energy_kwh"])

# 6. backfill baseline adoption: seeded cum must NOT double-count the counter
c2 = metadb.connect(":memory:")
c2.executescript(metadb.SCHEMA)
t = int(time.time())
c2.execute("INSERT INTO samples (ts,port,in_tokens,out_tokens) VALUES (?,?,?,?)",
           (t - 60, 8888, 5.0, 3.0))
metadb.ledger_backfill(c2, 0)
n = metadb.ledger_latest(c2)
assert n["in_tokens"] == 5 and n["out_tokens"] == 3, n
# first live flush with lifetime counter=1_000_000: adopt as baseline, credit 0
metadb.ledger_update(c2, 8888, 1_000_000.0, 600_000.0, None, ts=t + 30)
n = metadb.ledger_latest(c2)
assert n["in_tokens"] == 5 and n["out_tokens"] == 3, ("double count!", n)
# next flush credits only the delta
metadb.ledger_update(c2, 8888, 1_000_100.0, 600_050.0, None, ts=t + 60)
n = metadb.ledger_latest(c2)
assert n["in_tokens"] == 105 and n["out_tokens"] == 53, n

# 7. series: monotone, per-port sums reconstruct fleet totals
s = metadb.ledger_series(c2)
assert s["in_cum"][0] == 5 and s["in_cum"][-1] == 105, s

print("LEDGER_TESTS_OK")
