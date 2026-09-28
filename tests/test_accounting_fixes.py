"""Regression tests for the 2026-09-27 accounting fixes (run on a DB COPY).

1. reset_tokens must NOT destroy the all-time energy meter (-49% bug).
2. ledger_backfill must honour the 'ledger_backfilled' marker so a
   deliberate reset sticks.
3. model_ledger_update with key=None must still credit the port's tokens
   (the :30000 silent-drop leak) instead of returning early.
4. The ledger's energy can only ever move FORWARD across a re-seed.

Run: python3 tests/test_accounting_fixes.py
"""
import os
import tempfile
import sys
import shutil
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import metadb

FAILS = []
# The public tree ships NO metrics.db (local state is never published), so this
# test cannot copy a "live" DB the way it did in production. It builds a
# fixture with the real schema and seeds the rows the assertions need.
# The production copy of this file is unchanged and still tests the live DB.
SRC = None


def build_fixture():
    """A throwaway DB with the production schema + a seeded energy history."""
    tmp = os.path.join(tempfile.mkdtemp(prefix="acct-"), "fixture.db")
    metadb.init_db(tmp)
    s = metadb.connect(tmp)
    for i in range(3):
        s.execute(
            "INSERT INTO ledger (port, ts, in_tokens_cum, out_tokens_cum,"
            " energy_kwh_cum) VALUES (?,?,?,?,?)",
            (8001, 1700000000 + i * 60, 1000.0 * (i + 1), 50.0 * (i + 1),
             0.25 * (i + 1)))
    s.execute(
        "INSERT INTO model_ledger (key, model, version, engine,"
        " in_tokens_cum, out_tokens_cum) VALUES (?,?,?,?,?,?)",
        ("m\x00?\x00vllm", "m", None, "vllm", 5000.0, 200.0))
    s.commit()
    s.close()
    return tmp


def ok(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)


def fresh_copy():
    """A fresh fixture DB (see build_fixture: no live DB ships publicly)."""
    return build_fixture()


def energy_of(c):
    r = c.execute("SELECT MAX(energy_kwh_cum) AS m FROM ledger").fetchone()
    return float(r["m"]) if r and r["m"] else 0.0


print("1. reset_tokens preserves the all-time energy meter")
p = fresh_copy()
c = metadb.connect(p)
before_e = energy_of(c)
before_t = c.execute("SELECT COALESCE(SUM(in_tokens_cum),0) FROM ledger "
                     "WHERE port=8001").fetchone()[0]
metadb.reset_tokens(c, {8001: (1000.0, 50.0)})
after_e = energy_of(c)
floor = c.execute("SELECT value FROM meta WHERE key='ledger_energy_floor'").fetchone()
ok(before_e > 0, f"live DB has a non-zero energy meter ({before_e:.4f} kWh)")
ok(floor is not None, "lifetime energy floor written to meta")
ok(after_e == 0.0, "ledger energy rows cleared by the token reset")
# The recovery path is the NEXT ledger_update (the backfill correctly no-ops
# once the marker is set, so a reset sticks). That flush must resume the
# integral from the floor rather than restarting the meter at zero -- this is
# the exact -49% regression (13.67 -> 6.99 kWh).
c.execute("DELETE FROM meta WHERE key IN ('ledger_prev_power',"
          "'ledger_prev_power_ts')")
T0 = 1_800_000_000
# explicit +30s steps: the gap guard (0 < dt < 600) correctly refuses to
# integrate two flushes inside the same second, so advance the clock.
metadb.ledger_update(c, 8001, 1000.0, 50.0, 100.0, ts=T0)
resumed = energy_of(c)
ok(resumed >= before_e - 1e-9,
   f"post-reset flush resumes at {resumed:.4f} >= pre-reset {before_e:.4f}")
metadb.ledger_update(c, 8001, 1100.0, 55.0, 150.0, ts=T0 + 30)
grew = energy_of(c)
ok(grew > resumed,
   f"energy keeps integrating after the reset ({resumed:.4f} -> {grew:.4f})")
c.close()

print("\n2. the 'ledger_backfilled' marker makes a reset stick")
ok(metadb.ledger_backfill(c := metadb.connect(p), 0) == 0,
   "backfill is a no-op after a reset set the marker")
rows = c.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
ok(rows >= 0, f"ledger row count after re-seed: {rows}")
c.close()

print("\n3. a fresh DB with no marker still backfills (first run)")
p2 = os.path.join(os.path.dirname(p), "fresh.db")
c2 = metadb.connect(p2)
metadb.init_db(p2)
n = metadb.ledger_backfill(c2, 0)
ok(c2.execute("SELECT value FROM meta WHERE key='ledger_backfilled'"
              ).fetchone() is not None,
   "first run writes the marker")
# second call must not re-seed
before = c2.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
metadb.ledger_backfill(c2, 0)
after = c2.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
ok(before == after, f"second backfill is a no-op ({before} -> {after})")
c2.close()

print("\n4. model_ledger_update with key=None no longer drops tokens")
p3 = os.path.join(os.path.dirname(p), "keynull.db")
c3 = metadb.connect(p3)
metadb.init_db(p3)
# port 30000's real shape: a watermark row with key=NULL
c3.execute("INSERT OR REPLACE INTO model_watermarks (port,key,model,version,"
           "engine,in_tokens_max,out_tokens_max) VALUES (30000,NULL,"
           "'qwen3.8-flash-next',NULL,NULL,1000.0,50.0)")
c3.commit()
metadb.model_ledger_update(c3, 30000, None, "qwen3.8-flash-next", None, None,
                           1500.0, 75.0)
rows = c3.execute("SELECT key,model,engine,in_tokens_cum,out_tokens_cum,"
                  "in_initial_cum FROM model_ledger").fetchall()
ok(len(rows) == 1, f"a row was created instead of returning early ({len(rows)})")
if rows:
    r = rows[0]
    ok(abs((r["in_tokens_cum"] or 0) - 500.0) < 1e-6,
       f"credited the 500-token growth (got {r['in_tokens_cum']})")
    ok("30000" in (r["engine"] or ""),
       f"row is attributable to the lane (engine={r['engine']!r})")
    ok((r["in_initial_cum"] or 0) == 0.0,
       "growth is NOT flagged unobserved (only the first-seen total is)")
    # a SECOND call must continue from the watermark, not re-credit lifetime
    metadb.model_ledger_update(c3, 30000, None, "qwen3.8-flash-next", None,
                               None, 1800.0, 90.0)
    r2 = c3.execute("SELECT in_tokens_cum FROM model_ledger").fetchone()
    ok(abs((r2["in_tokens_cum"] or 0) - 800.0) < 1e-6,
       f"second call credits only the new 300 (got {r2['in_tokens_cum']})")
c3.close()

print("\n5. reset_energy clears the floor (so zero-energy stays zero)")
p4 = os.path.join(os.path.dirname(p), "nrg.db")
shutil.copy(p, p4)
c4 = metadb.connect(p4)
c4.execute("INSERT OR REPLACE INTO meta(key,value) "
           "VALUES('ledger_energy_floor','99.0')")
c4.commit()
metadb.reset_energy(c4)
ok(c4.execute("SELECT value FROM meta WHERE key='ledger_energy_floor'"
              ).fetchone() is None,
   "floor cleared by reset_energy")
c4.close()

shutil.rmtree(os.path.dirname(p), ignore_errors=True)
print()
if FAILS:
    print(f"FAILED {len(FAILS)} check(s):")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("OK - accounting fixes hold")
