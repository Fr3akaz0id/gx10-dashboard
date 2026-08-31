# GX10 Dashboard

Self-contained metrics dashboard for the NVIDIA DGX Spark (GB10) box.
Pure Python stdlib — no pip dependencies, no external JS libraries. Serves
three pages plus a JSON API on port 9000.

- Live: http://<host>:9000 (systemd unit `gx10-dashboard.service`)
- Install dir: `/opt/gx10-dashboard/`
- Runs as a dedicated non-root user, bound to `0.0.0.0` (LAN only)

## Pages

| Path        | What it shows |
|-------------|---------------|
| `/`         | Redirects to `/metrics` — landing page: GB10 gauges (SM/temp/power/util/throttle), per-engine stat cards with live tok/s, spec-decode card, today + all-time cost meters, TOKENS BY MODEL grid |
| `/engines`  | Fleet view: systemd units + docker containers, discovery, lifecycle status |
| `/settings` | Engine fleet editor, model roots, cost config (energy tariff + token SKUs, USD/EUR display), data reset tiers |
| `/setup`    | First-run onboarding wizard: scans for model dirs and inference engines, writes config.json. Also forced when config.json is missing; revisit anytime to reconfigure (current settings pre-selected) |

## Architecture

```
dashboard.py    HTTP server (:9000) + 2s poll loop + API          (~3000 lines)
engines.py      engine discovery (systemctl/docker probing)
engines_write.py  surgical unit-file writer + recipe apply
catalog.py      unit parsing / annotations / recipes
promparse.py    Prometheus-format /metrics scraping (vLLM, llama.cpp, sglang)
metadb.py       SQLite writer/reader: samples, gpu_hw, ledger, pruning
metrics.html / engines.html / settings.html / setup.html   UI (vanilla JS + canvas)
favicon.ico / favicon.png   square GX10 mark, green on dark grey (16/32/48 ICO)
setup wizard backend: GET/POST /api/setup/probe, POST /api/setup/apply (in dashboard.py)
config.json     live config (engines, model roots, cost) — hot-reloads by mtime
recipes/        docker recreate recipes (qwen38-4bit.json, vllm-server.json)
tests/          pytest suite (ledger, decimation, promparse, sglang)
logs/           dbwriter.err and friends
```

### Data flow

1. Poll loop every 2 s (`POLL_S`): nvidia-smi, hwmon/thermal, /proc, plus a
   Prometheus scrape of each enabled engine's port.
2. In-memory ring buffers (240 samples ≈ 8 min host history; 450 samples
   = 15 min per-engine) serve all fast/live views. A DB hiccup never
   degrades the live page.
3. Every 30 s (`DB_FLUSH_EVERY=15` polls) a flush writes to SQLite WAL:
   - `samples` — per-port tokens/stats, **14-day retention** (pruned once/day)
   - `gpu_hw` — host GPU series, 14-day retention
   - `model_ledger` + `ledger` — cumulative per-model / per-port token
     watermarks and fleet energy. **NEVER pruned** — this is the all-time meter.

Long windows (1h/24h/7d) are served from SQLite; the full span is fetched
then decimated in Python (`_decimate()`).

## API

| Endpoint | Notes |
|----------|-------|
| GET `/api/metrics` | per-engine stats+series, host block, `gpu_hw`, `gpu_series`, cost block. Params: `?window`, `?live=1` |
| GET `/api/metrics/history` | `?port`, `?span_s` — SQLite series + live gpu_hw |
| GET `/api/engines` | fleet + `port_conflicts` |
| GET `/api/engines/discover` | systemd units + docker containers |
| GET `/api/engines/models` | model catalog from configured roots |
| GET `/api/engines/containers` | docker container list |
| GET `/api/config` | current config.json (`?force=1` bypasses 60s cache) |
| POST `/api/config/preview` | diff preview before save |
| POST `/api/config/save` | writes `.bak` backup first, hot-reload |
| POST `/api/data/reset` | tiered reset T1-T4; T2+ require exact body `RESET` |

## Cost meters

- **TODAY**: cloud token-tier pricing per SKU (Ultra/Mid/Low) from midnight
  local time, computed at render time from current config rates.
- **ALL-TIME COST headline**: electricity only (`query_energy_kwh(0)` ×
  `usd_per_kwh`). Token-tier prices are context only, never summed into it.
- Token counters use max-ever watermarks per port; a negative delta after a
  restart credits the new reading instead of double-counting.
- `cost.currency` (USD|EUR) is a display-only symbol switch — prices keep
  the numbers you enter, only the symbol changes. No FX conversion.

## Data reset tiers

| Tier | Scope | Guard |
|------|-------|-------|
| T1 | nothing (unconfirmed) | rejected |
| T2 | tokens / models / single model / windows | exact `RESET` typed |
| T3 | models | backup + watermark rebase |
| T4 | everything (all-time rebase: counters restart from the live engine readings) | 3 rolling `.bak` snapshots |

Backups land next to the DB: `metrics.db.bak-YYYYMMDD_HHMMSS`.

## Operations

```bash
systemctl status gx10-dashboard      # health
journalctl -u gx10-dashboard -f      # logs
tail /opt/gx10-dashboard/logs/dbwriter.err   # DB writer errors
curl -s localhost:9000/api/metrics | python3 -m json.tool | head   # smoke test
python3 -m pytest /opt/gx10-dashboard/tests/ -q    # test suite
```

Config edits via the settings UI or by editing `config.json` directly —
mtime-based hot reload picks changes up on the next 2 s poll, no restart.

## Known issues (accepted)

- Port conflicts are surfaced in the UI banner; both sides exist in config
  until the user picks winners.
- Lifecycle buttons (part 16) investigated but broken — investigated but not fixed.
  Security gap (unit-name whitelist, 0.0.0.0 bind) intentionally deferred;
  personal LAN box only.
- settings_save() reads config twice (minor).

## Rules (don't break these)

- Never touch your model directories; don't restart inference services unless asked.
- Retirement = move file to `.removed/`, never delete.
- metrics pages: stdlib only, native canvas, NO `<select>` (segmented buttons).
- Series key duality is load-bearing: `/api/metrics` → `output_per_s/input_per_s`,
  `/api/metrics/history` → `out_tps/in_tps`. Keep the `||fallback` bindings paired.
- config.json saves always write a `.bak` timestamped backup first.

