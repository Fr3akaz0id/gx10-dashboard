"""Regression tests for the 2026-09-27 correctness fixes.

1. percentile() must not return None for the +Inf band (it silently deleted
   p50/p95/p99 on the live :8001 vLLM lane).
2. percentile() must not raise TypeError on a None bucket count.
3. api_metrics_history must project total_tps (the frontend reads it).

Run: python3 tests/test_percentile_inf.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import promparse

FAILS = []


def ok(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)


INF = float("inf")

print("1. +Inf band returns the top finite bound (was None)")
# every observation above the largest finite edge -- the real :8001 shape
b = [(1.0, 0.0), (100.0, 0.0), (200000.0, 0.0), (INF, 39.0)]
for p in (50, 95, 99):
    got = promparse.percentile(b, p)
    ok(got == 200000.0, f"p{p} -> top finite 200000.0 (got {got})")

print("\n2. no finite buckets at all -> None, not a crash")
b2 = [(INF, 5.0)]
ok(promparse.percentile(b2, 50) is None, "all-+Inf histogram -> None")

print("\n3. normal interpolation still exact")
# total=10, so p50 rank=5.0 lands exactly on the le=2.0 bucket edge,
# and p95 rank=9.5 interpolates inside the [2,4] band: 2+(9.5-5)/5*2 = 3.8
b3 = [(1.0, 0.0), (2.0, 5.0), (4.0, 10.0), (INF, 10.0)]
ok(abs(promparse.percentile(b3, 50) - 2.0) < 1e-9,
   f"p50 -> 2.0 (got {promparse.percentile(b3, 50)})")
ok(abs(promparse.percentile(b3, 95) - 3.8) < 1e-6,
   f"p95 -> 3.8 (got {promparse.percentile(b3, 95)})")

print("\n4. a rank inside the top finite band still interpolates")
# total=2, p99 rank=1.98 sits INSIDE [1,2] -> 1+(1.98-1)/1*1 = 1.98.
# Only a rank past the last finite edge falls through to the +Inf branch.
b4 = [(1.0, 1.0), (2.0, 2.0), (INF, 2.0)]
ok(abs(promparse.percentile(b4, 99) - 1.98) < 1e-9,
   f"p99 -> 1.98 (got {promparse.percentile(b4, 99)})")
b4b = [(1.0, 1.0), (2.0, 1.0), (INF, 2.0)]
ok(promparse.percentile(b4b, 99) == 2.0,
   f"p99 past top edge -> 2.0 (got {promparse.percentile(b4b, 99)})")

print("\n5. None bucket count must not raise TypeError")
b5 = [(1.0, None), (2.0, 4.0), (INF, 4.0)]
try:
    v = promparse.percentile(b5, 50)
    ok(isinstance(v, (int, float)) or v is None, f"no raise (got {v!r})")
except TypeError as e:
    ok(False, f"raised TypeError: {e}")

print("\n6. empty / zero-count still None")
ok(promparse.percentile([], 50) is None, "empty -> None")
ok(promparse.percentile([(1.0, 0.0), (INF, 0.0)], 50) is None, "zero total -> None")

print("\n7. total_tps is projected into the history series")
import _bootstrap  # noqa: F401  (must precede dashboard: isolates log + DB)
from _bootstrap import D
src = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "dashboard.py")).read()
ok('series["total_tps"]' in src,
   "api_metrics_history projects series['total_tps']")

print()
if FAILS:
    print(f"FAILED {len(FAILS)} check(s):")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("OK - percentile + history series fixes hold")
