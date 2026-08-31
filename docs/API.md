# API reference

All endpoints are plain HTTP, JSON in/out, no auth (LAN trust model — see
README). Field shapes below were captured from a live server; non-exhaustive
keys are possible as the UI evolves.

## Pages

| Path | Serves |
|------|--------|
| `/`, `/metrics` | metrics page (landing; any unknown non-API path falls through here) |
| `/engines` | fleet page |
| `/settings` | settings page |
| `/setup` | onboarding wizard (also forced when `config.json` is missing) |
| `/favicon.ico` | square GX10 mark |

## GET `/api/metrics`

The main feed. Params: `?window=<seconds>` (history window for rates/cost),
`?live=1` (live-only, skip DB reads).

Top-level keys: `window_s`, `refresh_s`, `engines`, `host`, `gpu_hw`,
`gpu_series`, `cost`, `cost_today`, `alltime`.

```jsonc
{
  "engines": [
    { "port": 30000, "kind": "unit", "backend": "sglang", "up": true,
      "has_metrics": true, "model": "...", "label": null,
      "stats": { /* instant rates: out_tps, in_tps, kv_pct, ttft_p50/95/99,
                    running, waiting, req_per_s, spec_acceptance, ... */ },
      "series": { "ts": [...], /* window series keyed as history below */ } }
  ],
  "host":   { "cpu_pct": ..., "load": ..., "mem": {...}, "disk": {...},
              "gpu_util": ..., "gpu_mem_mib": ... },
  "gpu_hw": { "sm_clock_mhz": ..., "sm_clock_max_mhz": ...,
              "throttle_reasons": [...], "throttle_counters_us": [...],
              "net_link_mbps": ..., "nvme_c": ... },
  "cost":       { /* window-scoped SKU equivalents */ },
  "cost_today": { "in_tokens": ..., "out_tokens": ..., "energy_kwh": ...,
                  "usd_per_kwh": 0.30, "currency": "EUR", "energy_usd": ...,
                  "skus": [ {"name":"Ultra cloud","in_price":5.0,
                             "out_price":25.0,"usd":0.85} ] },
  "alltime":    { "since_ts": ..., "in_tokens": ..., "out_tokens": ...,
                  "energy_kwh": ..., "energy_usd": ..., "skus": [...],
                  "currency": "EUR",
                  "models": [ {"model":"...","in_tokens":...,"out_tokens":...,
                               "first_ts":...,"last_ts":...} ] }
}
```

⚠ Series-key duality is load-bearing: live stats use `output_per_s` /
`input_per_s`, history series use `out_tps` / `in_tps`.

## GET `/api/metrics/history`

Params: `?port=<int>`, `?span_s=<3600|86400|604800>` (other values snap to
the nearest). The full span is fetched, then decimated server-side.

```jsonc
{ "port": 30000, "model": "...", "span_s": 3600,
  "points": 1250,              // a COUNT, not the array!
  "series": { "ts": [...], "out_tps": [...], "in_tps": [...],
              "kv_pct": [...], "ttft_p50": [...], "ttft_p95": [...],
              "ttft_p99": [...], "tpot_p95": [...], "queue_p95": [...],
              "running": [...], "waiting": [...], "req_per_s": [...],
              "prefix_hit_rate": [...], "spec_acceptance": [...],
              "preempt_per_min": [...], "total_tokens": [...],
              "in_tokens": [...], "out_tokens": [...], /* ... */ },
  "gpu": {...}, "gpu_hw": {...}, "spec": {...},
  "tokens": {...}, "cost": {...}, "cost_today": {...}, "alltime": {...} }
```

Arrays under `series.<key>` align index-for-index with `series.ts`.

## GET `/api/engines`

```jsonc
{ "fleet": [ { "kind": "unit|docker|port", "name": "...", "label": null,
               "image": "...", "active": "active|inactive|failed",
               "enabled": "yes|no", "status": "...", "port": 8080,
               "engine": "llama|vllm|sglang", "model": "...",
               "model_short": "...", "gpu_mem_util": ..., "rss_gib": ... } ],
  "gpu_used_gib": ...,
  "port_conflicts": { "8889": ["a.service", "b.service"] } }
```

## GET `/api/engines/discover`

systemd units + docker containers, filtered to inference engines. Each
candidate: `configured` (bool), `suggested` (ready-to-import config entry),
`port_conflict` (bool).

## GET `/api/engines/models`

Model catalog from enabled roots: `{"models":[{"path","name","gib","kind","source"}]}`.
GGUF shard sets collapse into one entry (only when all parts exist).

## GET `/api/engines/containers`

Docker container list. GET `/api/engines/logs/<unit>` — unit journal tail.

## GET `/api/config`

Current normalized config + per-root stats. `?force=1` bypasses the 60 s cache.

## POST `/api/config/preview` → `/api/config/save`

Two-step write. Body carries the candidate config as a **JSON string** under
`raw` (an object → 400). Preview returns the exact diff the save would apply;
save writes a `config.json.bak.<ts>` first, then hot-reloads.

## POST `/api/setup/probe` / `/api/setup/apply`

Wizard backend. **Probe is POST-only** (`GET /api/setup/probe` falls through
to the system-metrics payload — don't GET it). Probe scans common model dirs
+ discovers engines; apply runs the same atomic preview→save path.

## POST `/api/data/reset`

```jsonc
// body: {"action": "windows|tokens|models|model|energy|all", ...}
{"action": "model", "model": "some-model"}          // single model
{"action": "all", "confirm": "RESET"}               // tiers ≥ T2 need exact "RESET"
```

Response: `{"ok": true, "backup": "/opt/gx10-dashboard/metrics.db.bak-..."}`.
T4 (`all`) rebases all-time counters to the live engine readings; it never
touches `config.json`. Rolling backups keep the newest 3.

## Errors

Handlers return `{"error": "..."}` with 400 (validation) / 404 / 500
(unexpected). No HTTP HEAD support (501) — health-check with GET.
