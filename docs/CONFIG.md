# config.json reference

`config.json` lives next to the code (`/opt/gx10-dashboard/config.json`).
It is the source of truth for **which** engines are surfaced, which model
directories get catalogued, and how costs are priced. The dashboard hot-reloads
it by file mtime — edits apply on the next 2 s poll, **no restart needed**.

Two safe ways to edit it:

1. **The UI** (`/settings`, or `/setup` on first run) — every write goes
   through a diff preview and writes a `config.json.bak.<timestamp>` first.
2. **By hand** — keep it valid JSON; on parse failure the dashboard keeps the
   last good config in memory and logs the error.

Unknown keys are silently dropped by the normalizer on save (they're stripped,
not preserved), and invalid entries (bad unit names, out-of-range ports) are
skipped, not fatal.

## Top-level shape

```json
{
  "model_roots": [ ... ],
  "engines":     [ ... ],
  "cost":        { ... }
}
```

## `model_roots` — where models live

| Field | Type | Notes |
|-------|------|-------|
| `path` | string | Directory to catalogue. Must exist to show up with stats. |
| `kind` | `"gguf"` \| `"hf"` | `gguf` = flat dir of `.gguf` files; `hf` = a HuggingFace hub cache (`models--org--name/snapshots/...`). |
| `enabled` | bool | Disabled roots stay listed but aren't scanned. |

Multi-part GGUF shards (`model-00001-of-00004.gguf`) collapse into a single
catalog entry only when all parts are present.

## `engines` — the fleet

Each entry:

| Field | Type | Notes |
|-------|------|-------|
| `kind` | `"unit"` \| `"docker"` \| `"port"` | Deployment type. `unit` = systemd service, `docker` = container, `port` = a plain local port with no unit/container around it. |
| `name` | string | Unit basename (`llama-server.service`, `.service` auto-appended) / container name / free label for `port`. Only engines in this list are polled and shown. |
| `port` | int 1–65535 or null | The engine's HTTP port. For `unit`, the real `--port` parsed from the unit's ExecStart wins when config leaves it null. |
| `label` | string or null | Display name override. |
| `model` | string or null | Model hint; normally auto-discovered from the process cmdline / `/metrics` labels — leave null. |
| `enabled` | bool | Toggle without removing the entry. |

Discovery (`/api/engines/discover`) finds candidate units/containers, filters
to inference servers (`llama-server`, `vllm`, `sglang`, `ds4-server`,
`ollama`, …), and annotates each with `configured` / `suggested` — you import
them from the UI instead of hand-writing entries.

Engines are considered **up** if `/metrics` *or* `/health` answers 200 — a
llama.cpp server without `--metrics` (which 501s `/metrics`) still reads up
and renders graceful empty panels where metrics don't exist.

## `cost` — pricing block (optional)

Absent or `{"enabled": false}` → cost meters are no-ops.

| Field | Type | Notes |
|-------|------|-------|
| `enabled` | bool | Master switch for all cost UI. |
| `currency` | `"USD"` \| `"EUR"` | Display-only symbol. **No FX conversion** — numbers stay exactly as entered. |
| `usd_per_kwh` | float | Your energy tariff per kWh (name kept as `usd_per_kwh` regardless of display currency). Energy $ = integrated GPU power × this. |
| `skus` | array | Cloud token-price comparators, priced per **million tokens**. |
| `skus[].name` | string | e.g. `"Ultra cloud"`. |
| `skus[].in_price` | float | $/M input (prompt) tokens. |
| `skus[].out_price` | float | $/M output (generated) tokens. |

What each number means is deliberate and non-negotiable:

- **TODAY SKU tiles** — hypothetical: what today's tokens would have cost on
  that cloud tier. Context only.
- **ALL-TIME COST** — real electricity: ledger kWh × tariff. Token SKUs are
  never summed into it.
- Power is integrated only over sampled intervals — hours with the box off
  (no samples) are not billed.

## Full example

See [`examples/config.example.json`](../examples/config.example.json).

## Reset interaction

`POST /api/data/reset` (the settings page "Data" card) mutates the **ledger
tables**, never `config.json`. Reset tiers are documented in the README;
every tier ≥ T2 requires typing exactly `RESET`, and every mutating tier
snapshots the DB (`metrics.db.bak-…`) first.
