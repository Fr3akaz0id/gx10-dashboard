# Architecture

## The loop

```
 every 2 s (POLL_S)                    every 30 s (DB_FLUSH_EVERY=15 polls)
┌───────────────────────┐              ┌────────────────────────────┐
│ nvidia-smi + hwmon    │              │ SQLite WAL flush           │
│ /proc (cpu/mem/disk)  │              │  samples, gpu_hw  (14 d)   │
│ GET /metrics per engine│──► rings ──► │  ledger, model_ledger (∞)  │
│ GET /slots (llama.cpp) │  (memory)   │  prune once/day            │
│ journalctl (spec dec.) │              └────────────────────────────┘
└───────────────────────┘
```

Rings: 240 host samples (~8 min), 450 per-engine samples (~15 min). All live
views read rings, so DB problems never blank the page. Long windows
(1 h/24 h/7 d) read SQLite and decimate in Python (`_decimate()` keeps ≤ ~1250
points while preserving token sums exactly).

## Files

| File | Role |
|------|------|
| `dashboard.py` | stdlib HTTP server (`ThreadingHTTPServer`, :9000), poll loop, all `/api/*`, spec-decode journal reader, `/slots` KV scraper |
| `catalog.py` | config load + normalize + mtime hot-reload, model roots, engine discovery annotations, docker recipes |
| `engines.py` | read-only systemd unit parser (ExecStart flags, env, ports) + vLLM recipe import |
| `engines_write.py` | byte-stable surgical unit-file writer (set/add/remove flag, set/remove env); no-op round-trips are byte-identical |
| `promparse.py` | Prometheus text-exposition parser: counters/gauges/histograms, percentile-from-buckets |
| `metadb.py` | schema, writers, decimated range queries, ledger math, pruning, reset tiers, rolling DB backups |
| `*.html` | 4 pages, one shared Grafana-dark design system, inline CSS/JS, native `<canvas>` charts — no frameworks |

## Storage schema (SQLite, WAL)

| Table | Contents | Retention |
|-------|----------|-----------|
| `samples` | per-port token/stat rows per flush | 14 d (pruned daily) |
| `gpu_hw` | host GPU series per flush | 14 d |
| `ledger` | cumulative `(in, out, energy_kwh)` per port + fleet row (`port=-1`) | **never pruned** |
| `model_ledger` | cumulative `(in, out)` per model | never pruned |
| `counter_watermarks` / `model_watermarks` | max-ever engine counter per port/model | — |
| `meta` | backfill/prune markers, reset timestamp | — |

## The watermark math (why the all-time meter is restart-proof)

Engine `/metrics` counters are lifetime-since-engine-start. To accumulate an
all-time total without double-counting restarts, the flush stores `max-ever`
of each lifetime counter per port (and per model). Each flush credits only
`current − watermark` when positive; when an engine restarts (counter <
watermark) the watermark resets to the new smaller reading. Ledger row =
running sum of credits. Data-reset tiers rewrite watermarks/ledger and rebase
to the live reading, so "start counting from today" is exact, not approximate.

SGLang quirk: its counters arrive under `sglang:*` names; `promparse` aliases
them to the `vllm:*` keys the ledger expects — an alias must carry a `series`
list or `sum_all()` silently returns None (there's a regression test for it).

## Threading model

One background poll/writer thread owns `_db_conn`. HTTP handlers run on
request threads: any handler that needs the DB opens its own short-lived
connection (WAL + `busy_timeout=3000` serialize writers). Do not share sqlite
handles across threads — this bit every `/api/data/reset` tier once.

## Adding a metric, end to end

1. Scrape it in the poll loop (`dashboard.py` `_scrape_engine` or host block).
2. For window/history support add the column to `metadb.py` (`SCHEMA`,
   `write_sample`, `_decimate` handling — cumulative sums must survive
   decimation) and to `_engine_series`.
3. Render it: `metrics.html` `setLegend(id, rows, fmt)` — per-row `fmt` wins
   over the outer fmt; any value feeding a 0–100 gauge must already be 0–100.
4. Tests: extend `tests/` (plain scripts with asserts, run directly —
   `python3 tests/test_promparse.py`).

## Constraints (they're load-bearing)

- **stdlib only.** No pip installs, ever — it's why this survives on boxes
  where nothing else installs cleanly.
- **No `<select>`** on dark UI (native popup can't be themed) — segmented
  buttons or custom div dropdowns.
- **Unit files are the source of truth** for a unit's process spec; config
  only decides which engines are surfaced.
- **The dashboard never starts/stops inference services.** Read + report.
