# GX10 Dashboard

Self-contained metrics dashboard for an NVIDIA GB10 (DGX Spark) box running
local LLM inference engines. Pure Python stdlib — no pip dependencies, no
external JS libraries, no build step. Serves a dark single-page metrics UI
plus a JSON API on port 9000.

It answers one question in one glance: **what is my box doing, and what is it
costing me?** GPU thermals and throttle state, per-engine token throughput,
KV-cache occupancy, spec-decode acceptance, prefix-cache
hits, and three cost views — today's cloud-price equivalent per token SKU,
today's electricity, and an all-time meter that survives restarts.

```
  ┌──────────────┐     scrape      ┌─────────────────────────────┐
  │  dashboard.py│ ◄────────────── │ llama.cpp / vLLM / SGLang   │
  │   (:9000)    │   /metrics      │ (systemd units, docker, or  │
  └──────┬───────┘   /slots        │  bare ports)                │
         │         + nvidia-smi    └─────────────────────────────┘
         │         + hwmon + /proc
    ┌────▼─────┐
    │ SQLite   │  WAL mode · 14d samples · never-pruned all-time ledger
    │ (WAL)    │
    └──────────┘
```

## Requirements

- Linux with systemd (for the service install; `--no-start` works without)
- Python ≥ 3.10, stdlib only
- `nvidia-smi` on PATH for GPU metrics (optional — everything else degrades
  gracefully without it)
- Any local inference engine exposing Prometheus `/metrics`: llama.cpp,
  vLLM, SGLang, DSv4, ollama-style servers

## Quick start

```bash
git clone <this repo> && cd gx10-dashboard
sudo ./install.sh
# open http://<host>:9000 → the onboarding wizard scans your model
# directories and inference engines and writes config.json for you
```

That's it. The installer creates a dedicated `dashboard` system user,
installs to `/opt/gx10-dashboard`, writes `gx10-dashboard.service`, enables
it, and health-checks the API. Re-running it refreshes the code and never
touches your `config.json` or `metrics.db`.

Prefer to look first?

```bash
sudo ./install.sh --no-start
sudo -u dashboard python3 /opt/gx10-dashboard/dashboard.py
```

With no `config.json` present the server forces `/setup` — a 3-step wizard
(scan model dirs → pick engines with live port-conflict flags → confirm). You
can also hand-write config; see [`examples/config.example.json`](examples/config.example.json)
and [docs/CONFIG.md](docs/CONFIG.md).

## Pages

| Path | What it shows |
|------|---------------|
| `/` | Metrics landing page: GB10 gauges (SM/temp/power/util/throttle), per-engine stat cards with live tok/s, spec-decode card, today + all-time cost meters, TOKENS BY MODEL grid |
| `/engines` | Fleet view: systemd units + docker containers, discovery, per-unit flag editor (surgical, byte-stable unit-file writes with diff preview) |
| `/settings` | Engine fleet editor, model roots, cost config (energy tariff + token SKUs, USD/EUR display), data reset tiers |
| `/setup` | Onboarding wizard: scans for model dirs and inference engines, writes config.json. Forced when config.json is missing; revisit anytime (current settings pre-selected) |

## What makes it tick

- **2 s poll loop** scrapes every configured engine's `/metrics` (Prometheus
  text format), plus `/slots` for llama.cpp KV/prefix-cache data, plus
  `nvidia-smi`, hwmon/thermal zones, and `/proc`. In-memory ring buffers
  serve all live views, so a DB hiccup never degrades the page.
- **SQLite WAL** writer flushes every 30 s: `samples` + `gpu_hw` with 14-day
  retention, and a **never-pruned cumulative ledger** (`ledger`,
  `model_ledger`) driven by max-ever watermarks per port/model — the
  all-time meter. A negative counter delta (engine restart) rebases instead
  of double-counting.
- **Config hot-reload by mtime**: changes made in `/settings` take effect on
  the next poll, no restart. Every save writes a timestamped `.bak` first and
  always goes through a diff preview.
- **Engine fleet is config-driven, discovery-assisted.** `/api/engines/discover`
  scans systemd units + docker containers, filters to inference servers, and
  suggests importable entries. `kind` supports `unit`, `docker`, and `port`
  (a bare host:port engine).

## Cost meters

- **TODAY**: cloud token-tier pricing per SKU (Ultra/Mid/Low, $/M-tokens)
  from midnight local time.
- **ALL-TIME COST**: electricity only — integrated GPU power × tariff.
  Token-tier prices are context, never summed into it.
- GPU power is only billed while samples exist — box-off gaps cost nothing.
- `cost.currency` (`USD`|`EUR`) is a display-only symbol switch: your numbers
  are kept as entered, no FX conversion.

## Data reset tiers

| Tier | Scope | Guard |
|------|-------|-------|
| T1 | nothing (unconfirmed) | rejected |
| T2 | tokens / models / single model / windows | exact `RESET` typed |
| T3 | models | backup + watermark rebase |
| T4 | everything — all-time counters rebase to the live engine readings | rolling `.bak` snapshots (3 kept) |

Snapshots land next to the DB: `metrics.db.bak-YYYYMMDD_HHMMSS`. Nuking never
loses data you didn't consent to losing.

## Docs

- [docs/CONFIG.md](docs/CONFIG.md) — every config.json field, cost block,
  engine kinds, hot-reload semantics
- [docs/API.md](docs/API.md) — all `/api/*` endpoints with example payloads
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — poll loop, storage schema,
  watermark math, how to add a metric

## Operations

```bash
systemctl status gx10-dashboard               # health
journalctl -u gx10-dashboard -f               # logs
tail /opt/gx10-dashboard/logs/dbwriter.err    # DB writer errors
curl -s localhost:9000/api/metrics | python3 -m json.tool | head   # smoke test
python3 tests/test_promparse.py && python3 tests/test_metadb_decimate.py \
  && python3 tests/test_ledger.py && python3 tests/test_sglang.py   # test suite
```

## Security posture

Read-mostly by design: the dashboard reads unit files, `/proc`, hwmon and
engine endpoints. It writes only to `config.json` (whitelisted paths,
`.bak`-first) and surgical edits to whitelisted unit files behind a diff
preview. It binds `0.0.0.0:9000` with **no authentication** — this is a LAN
box tool. Put it behind a firewall or reverse-auth if your network is not
trusted. Do not expose it to the internet.

## Known issues (accepted)

- Port conflicts are surfaced in the UI banner; both sides stay in config
  until you pick winners.
- Engine lifecycle buttons on `/engines` are incomplete — start/stop was
  investigated and deferred (security review first). The dashboard reports
  fleet state; it does not restart your inference services.
- Spec-decode per-position acceptance needs vLLM's per-position counters;
  SGLang exports only last-batch gauges, so its card shows live acceptance %
  instead. llama.cpp reads acceptance from the unit journal (fork-agnostic).

## License

MIT — see [LICENSE](LICENSE).
