"""SQLite history for the /metrics page.

Stdlib only. WAL mode: the collect thread is the single writer; API handlers
open short-lived read-only handles, which never block on the writer.
Retention is pruned by the writer once per 24 h.
"""
import glob
import os
import sqlite3
import time

SAMPLE_COLS = ["ts", "port", "model", "kv_pct", "running", "waiting",
               "out_tps", "in_tps", "total_tps", "req_per_s",
               "ttft_p50", "ttft_p95", "ttft_p99", "tpot_p50", "tpot_p95",
               "queue_p50", "queue_p95", "e2e_p95",
               "prefix_hit_rate", "spec_acceptance", "preempt_per_min",
               "total_tokens", "finish_per_min", "http_2xx_per_min",
               "http_4xx_per_min", "prompt_cached_pct",
               "in_tokens", "out_tokens", "slot_cap", "slot_running"]

GPU_COLS = ["ts", "sm_clock_mhz", "sm_clock_max_mhz", "throttle_active",
            "throttle_sw_thermal_us", "throttle_hw_thermal_us",
            "throttle_hw_brake_us", "nvme_c", "temp_c", "power_w", "util_pct"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  ts INTEGER NOT NULL,
  port INTEGER NOT NULL,
  model TEXT,
  kv_pct REAL, running REAL, waiting REAL,
  out_tps REAL, in_tps REAL, total_tps REAL, req_per_s REAL,
  ttft_p50 REAL, ttft_p95 REAL, ttft_p99 REAL, tpot_p50 REAL, tpot_p95 REAL,
  queue_p50 REAL, queue_p95 REAL, e2e_p95 REAL,
  prefix_hit_rate REAL, spec_acceptance REAL, preempt_per_min REAL,
  total_tokens REAL, finish_per_min REAL,
  http_2xx_per_min REAL, http_4xx_per_min REAL, prompt_cached_pct REAL,
  in_tokens REAL, out_tokens REAL,
  PRIMARY KEY (ts, port)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_samples_port_ts ON samples (port, ts);
CREATE TABLE IF NOT EXISTS gpu_hw (
  ts INTEGER PRIMARY KEY,
  sm_clock_mhz INTEGER, sm_clock_max_mhz INTEGER,
  throttle_active INTEGER, throttle_sw_thermal_us INTEGER,
  throttle_hw_thermal_us INTEGER, throttle_hw_brake_us INTEGER,
  nvme_c REAL, temp_c REAL, power_w REAL, util_pct REAL
);
CREATE TABLE IF NOT EXISTS ledger (
  ts INTEGER NOT NULL,
  port INTEGER NOT NULL,
  in_tokens_cum REAL,
  out_tokens_cum REAL,
  energy_kwh_cum REAL,
  PRIMARY KEY (ts, port)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_ledger_port_ts ON ledger (port, ts);
CREATE TABLE IF NOT EXISTS counter_watermarks (
  port INTEGER PRIMARY KEY,
  in_tokens_max REAL,
  out_tokens_max REAL
);
CREATE TABLE IF NOT EXISTS model_ledger (
  key TEXT NOT NULL PRIMARY KEY,
  model TEXT NOT NULL,
  version TEXT,
  engine TEXT,
  in_tokens_cum REAL NOT NULL DEFAULT 0,
  out_tokens_cum REAL NOT NULL DEFAULT 0,
  in_initial_cum REAL NOT NULL DEFAULT 0,
  out_initial_cum REAL NOT NULL DEFAULT 0,
  first_ts INTEGER,
  last_ts INTEGER
);
CREATE TABLE IF NOT EXISTS model_watermarks (
  port INTEGER PRIMARY KEY,
  key TEXT,
  model TEXT,
  version TEXT,
  engine TEXT,
  in_tokens_max REAL,
  out_tokens_max REAL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

MIGRATE_V2 = [
    ("samples", "total_tokens", "REAL"),
    ("samples", "finish_per_min", "REAL"),
    ("samples", "http_2xx_per_min", "REAL"),
    ("samples", "http_4xx_per_min", "REAL"),
    ("samples", "prompt_cached_pct", "REAL"),
    ("samples", "in_tokens", "REAL"),
    ("samples", "out_tokens", "REAL"),
    ("samples", "ttft_p99", "REAL"),
    ("samples", "slot_cap", "REAL"),
    ("samples", "slot_running", "REAL"),
]
SCHEMA_VERSION = 4


def connect(path, readonly=False):
    # URI mode only accepts ro/rw|memory; "rw" is the plain default, so
    # writable handles just use the bare path.
    uri = f"file:{path}?mode=ro" if readonly else path
    c = sqlite3.connect(uri, uri=readonly, timeout=5)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=3000")
    return c


def init_db(path):
    c = connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.executescript(SCHEMA)
    # forward migration for pre-v2 DBs (ADD COLUMN is idempotent-gated)
    have = {r[1] for r in c.execute("PRAGMA table_info(samples)")}
    for table, col, typ in MIGRATE_V2:
        if col not in have:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    _migrate_model_ledger_v4(c)
    cur_v = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if not cur_v or int(cur_v["value"]) < SCHEMA_VERSION:
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",
                  (str(SCHEMA_VERSION),))
    c.commit()
    c.close()


def _migrate_model_ledger_v4(c):
    """v4: model_ledger gains (key, version, engine, *_initial_cum).

    Old rows are keyed on the bare model string; the key format is
    '<model>\\u0000<version>\\u0000<engine>', so a pre-migration model string
    can never collide with a post-migration key. Rebuild the table in place,
    carrying every old row over verbatim with version/engine left NULL and a
    NULL watermark-model marker: on the next credit the model is treated as
    new (fresh lifetime credit, flagged initial)."""
    have = {r[1] for r in c.execute("PRAGMA table_info(model_ledger)")}
    if "key" in have:
        return
    c.execute("ALTER TABLE model_ledger RENAME TO model_ledger_v3")
    c.execute("""CREATE TABLE model_ledger (
      key TEXT NOT NULL PRIMARY KEY,
      model TEXT NOT NULL,
      version TEXT,
      engine TEXT,
      in_tokens_cum REAL NOT NULL DEFAULT 0,
      out_tokens_cum REAL NOT NULL DEFAULT 0,
      in_initial_cum REAL NOT NULL DEFAULT 0,
      out_initial_cum REAL NOT NULL DEFAULT 0,
      first_ts INTEGER,
      last_ts INTEGER)""")
    c.execute("""INSERT INTO model_ledger (key, model, version, engine,
                   in_tokens_cum, out_tokens_cum, in_initial_cum, out_initial_cum,
                   first_ts, last_ts)
                 SELECT model, model, NULL, NULL,
                        in_tokens_cum, out_tokens_cum,
                        in_tokens_cum, out_tokens_cum,
                        first_ts, last_ts FROM model_ledger_v3""")
    # legacy rows are themselves the unobserved history — count them as
    # initial credit so the observed-vs-unobserved split stays honest
    c.execute("DROP TABLE model_ledger_v3")
    # model_watermarks gains (key, version, engine) — in place, ADD COLUMN.
    # We KEEP the existing watermark rows on purpose: their in/out_max are
    # each port's lifetime counters at the last flush. On the first flush
    # after migration the new composite key differs from the legacy key, so
    # model_ledger_update credits only the growth since that watermark —
    # NOT the whole lifetime counter — so the carried-over legacy row and
    # the fresh composite row never double-count the same tokens.
    wm_have = {r[1] for r in c.execute("PRAGMA table_info(model_watermarks)")}
    for col, typ in (("key", "TEXT"), ("version", "TEXT"), ("engine", "TEXT")):
        if col not in wm_have:
            c.execute(f"ALTER TABLE model_watermarks ADD COLUMN {col} {typ}")


def write_sample(c, row):
    """Upsert one engine sample row (row: dict with SAMPLE_COLS keys)."""
    c.execute(
        f"INSERT OR REPLACE INTO samples ({','.join(SAMPLE_COLS)}) "
        f"VALUES ({','.join('?' * len(SAMPLE_COLS))})",
        [row.get(k) for k in SAMPLE_COLS])


def write_gpu(c, row):
    """Upsert one gpu_hw row (row: dict with GPU_COLS keys)."""
    c.execute(
        f"INSERT OR REPLACE INTO gpu_hw ({','.join(GPU_COLS)}) "
        f"VALUES ({','.join('?' * len(GPU_COLS))})",
        [row.get(k) for k in GPU_COLS])


def _decimate(rows, limit):
    """Stride newest-first so the latest point is always kept, then reverse to
    chronological. rows must be in ASC (oldest->newest) order. Output is <=
    limit and spans the full window (ceil stride, not floor — floor would keep
    far more than limit points for large windows)."""
    n = len(rows)
    if n <= limit:
        return [dict(r) for r in rows]
    stride = -(-n // limit)  # ceil(n/limit)
    out = [dict(r) for r in rows[::-1][::stride]]
    out.reverse()
    return out


def query_range(c, port, since_ts, limit=1440):
    """Full window oldest->newest, decimated to <= limit points.
    The whole span is fetched (never SQL-LIMITed) so a 7d view still covers
    all 7 days; decimation happens in Python over the full row set."""
    rows = c.execute(
        "SELECT * FROM samples WHERE port=? AND ts>=? ORDER BY ts",
        (port, since_ts)).fetchall()
    if not rows:
        return []
    return _decimate(rows, limit)


def query_gpu(c, since_ts, limit=1440):
    """Full window oldest->newest, decimated to <= limit points (see
    query_range for why the fetch must not be SQL-LIMITed)."""
    rows = c.execute("SELECT * FROM gpu_hw WHERE ts>=? ORDER BY ts",
                     (since_ts,)).fetchall()
    if not rows:
        return []
    return _decimate(rows, limit)


def query_token_sums(c, port, since_ts):
    """Sum in/out tokens over the window (all engines, or one port)."""
    if port is None:
        r = c.execute("SELECT COALESCE(SUM(in_tokens),0) AS i, COALESCE(SUM(out_tokens),0) AS o "
                      "FROM samples WHERE ts>=?", (since_ts,)).fetchone()
    else:
        r = c.execute("SELECT COALESCE(SUM(in_tokens),0) AS i, COALESCE(SUM(out_tokens),0) AS o "
                      "FROM samples WHERE port=? AND ts>=?", (port, since_ts)).fetchone()
    return {"in_tokens": int(r["i"] or 0), "out_tokens": int(r["o"] or 0)}


def query_energy_kwh(c, since_ts):
    """Integrate GPU power draw over the window: trapezoid over power_w (W)
    -> joules -> kWh. power_w is sampled at ~30s cadence; trapezoid is
    accurate to the sampling interval."""
    rows = c.execute("SELECT ts, power_w FROM gpu_hw WHERE ts>=? ORDER BY ts",
                     (since_ts,)).fetchall()
    joules = 0.0
    prev = None
    for r in rows:
        pw = r["power_w"]
        if pw is None:
            continue
        if prev is not None:
            dt = r["ts"] - prev[0]
            # gap sanity (mirrors ledger_update): don't bill a stale gap —
            # service downtime / missing samples would otherwise be bridged
            # at the surrounding power level and inflate the meter
            if 0 < dt < 600:
                joules += 0.5 * (pw + prev[1]) * dt   # W * s = J
        prev = (r["ts"], pw)
    return round(joules / 3.6e6, 6)  # J / 3.6e6 = kWh


def last_prune_ts(c):
    r = c.execute("SELECT value FROM meta WHERE key='last_prune'").fetchone()
    return int(r["value"]) if r else 0


def prune(c, retention_days):
    """Delete rows older than retention_days; record when we last ran.
    The ledger is NEVER pruned — it is the all-time meter."""
    cutoff = int(time.time()) - int(retention_days * 86400)
    r1 = c.execute("DELETE FROM samples WHERE ts < ?", (cutoff,)).rowcount
    r2 = c.execute("DELETE FROM gpu_hw WHERE ts < ?", (cutoff,)).rowcount
    c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_prune',?)",
              (str(int(time.time())),))
    c.commit()
    return r1 + r2


# ── all-time ledger ─────────────────────────────────────────────────────
# Counters on the engines reset on every server restart, so "all time" can
# never be read from a live counter. Instead: keep a per-port high-water
# mark of each token counter; each flush credits the positive delta since
# the mark into a monotone cumulative total. A counter reset (new value
# below the mark) means the engine restarted — the new reading itself is
# the delta. The result survives engine restarts, dashboard restarts and
# the 14d samples prune. Energy is integrated per flush from the power
# ring the caller passes in (fleet-wide, whole GPU).

LEDGER_FLUSH_S = 30  # min seconds between ledger updates


def ledger_update(c, port, in_total, out_total, power_w, ts=None):
    """Credit one engine's counter reading + one fleet power sample into the
    ledger. in_total/out_total are the engine's LIFETIME /metrics counters
    (None = engine has no counters / is down — skip tokens, keep energy).
    power_w is the whole-GPU draw at this instant (None skips energy).
    Returns nothing; commits are the caller's business."""
    now = int(ts or time.time())
    wm = c.execute("SELECT in_tokens_max, out_tokens_max FROM counter_watermarks "
                   "WHERE port=?", (port,)).fetchone()
    w_in = wm["in_tokens_max"] if wm else None
    w_out = wm["out_tokens_max"] if wm else None
    last = c.execute("SELECT in_tokens_cum, out_tokens_cum FROM ledger "
                     "WHERE port=? ORDER BY ts DESC LIMIT 1", (port,)).fetchone()
    c_in = (last["in_tokens_cum"] or 0.0) if last else 0.0
    c_out = (last["out_tokens_cum"] or 0.0) if last else 0.0
    if in_total is not None:
        if w_in is None and last is not None:
            # post-backfill baseline adoption: the seeded cum already covers
            # retained history — do NOT credit the full lifetime counter on top
            w_in = in_total
        else:
            d_in = in_total - w_in if w_in is not None else in_total
            if d_in < 0:
                # counter reset (engine restarted): the new reading itself
                # is what was served since the restart
                d_in = in_total
            if d_in > 0:
                c_in += d_in
            w_in = in_total
    if out_total is not None:
        if w_out is None and last is not None:
            w_out = out_total
        else:
            d_out = out_total - w_out if w_out is not None else out_total
            if d_out < 0:
                d_out = out_total
            if d_out > 0:
                c_out += d_out
            w_out = out_total
    # energy: trapezoid against the previous ledger row's power reading,
    # carried in meta so it survives port churn
    prev_p = c.execute("SELECT value FROM meta WHERE key='ledger_prev_power'").fetchone()
    prev_t = c.execute("SELECT value FROM meta WHERE key='ledger_prev_power_ts'").fetchone()
    e_cum = c.execute("SELECT energy_kwh_cum FROM ledger ORDER BY ts DESC LIMIT 1").fetchone()
    e_cum = (e_cum["energy_kwh_cum"] or 0.0) if e_cum and e_cum["energy_kwh_cum"] else 0.0
    if power_w is not None and prev_p is not None and prev_t is not None:
        dt = now - int(prev_t["value"])
        if dt > 0 and dt < 600:  # gap sanity: don't bill a stale gap
            e_cum += 0.5 * (power_w + float(prev_p["value"])) * dt / 3.6e6
    if power_w is not None:
        c.execute("INSERT OR REPLACE INTO meta(key,value) "
                  "VALUES('ledger_prev_power',?)", (str(power_w),))
        c.execute("INSERT OR REPLACE INTO meta(key,value) "
                  "VALUES('ledger_prev_power_ts',?)", (str(now),))
    c.execute("INSERT OR REPLACE INTO ledger (ts, port, in_tokens_cum, "
              "out_tokens_cum, energy_kwh_cum) VALUES (?,?,?,?,?)",
              (now, port, c_in, c_out, round(e_cum, 6)))
    c.execute("INSERT OR REPLACE INTO counter_watermarks (port, in_tokens_max, "
              "out_tokens_max) VALUES (?,?,?)", (port, w_in, w_out))


def model_ledger_update(c, port, key, model, version, engine, in_total, out_total, ts=None):
    """Credit one engine's counter reading into the PER-MODEL ledger.

    Attribution = (model, version, engine): the composite `key` — two lanes
    serving the same model base (or two quants of it) are separate rows.

    Credit rules (the engine counter is ONE lifetime counter per port, so
    every credit is a reading against the previous port watermark):
      * same key as last observation: delta vs watermark (normal credit).
      * key changed on the port, counter grew since: only the growth
        (reading - previous watermark) is credited — NOT the lifetime
        counter. The pre-switch history stays on the old key's row.
      * counter reset (reading below previous watermark): the new reading
        is credited; if the key also changed, the pre-restart counter can't
        be attributed, so it is marked INITIAL (unobserved history).
      * first-ever observation on the port (no prior watermark): the
        lifetime counter is credited and marked INITIAL — the engine really
        served those tokens, the dashboard just wasn't watching.

    `*_initial_cum` accumulates the initial/unobserved part so the UI can
    show 'incl. N unobserved' instead of letting it read as fresh usage.
    None counters skip token work entirely."""
    now = int(ts or time.time())
    if not key:
        return
    wm = c.execute("SELECT key, in_tokens_max, out_tokens_max FROM "
                   "model_watermarks WHERE port=?", (port,)).fetchone()
    same = wm is not None and wm["key"] == key
    w_in = wm["in_tokens_max"] if wm else None
    w_out = wm["out_tokens_max"] if wm else None
    row = c.execute("SELECT in_tokens_cum, out_tokens_cum, in_initial_cum, "
                    "out_initial_cum, first_ts FROM model_ledger WHERE key=?",
                    (key,)).fetchone()
    c_in = row["in_tokens_cum"] if row else 0.0
    c_out = row["out_tokens_cum"] if row else 0.0
    c_ini = row["in_initial_cum"] if row else 0.0
    c_ini_o = row["out_initial_cum"] if row else 0.0
    first = row["first_ts"] if row else now
    if in_total is not None:
        if w_in is None:
            # first-ever observation on this port: lifetime counter, flagged
            c_in += in_total
            c_ini += in_total
        elif in_total < w_in:
            # counter reset (engine restart)
            c_in += in_total
            if not same:
                c_ini += in_total  # pre-restart counter is unattributable
        else:
            # normal growth (incl. key change without reset: only the growth
            # since the last scrape is credited, not the whole lifetime)
            c_in += in_total - w_in
        w_in = in_total
    if out_total is not None:
        if w_out is None:
            c_out += out_total
            c_ini_o += out_total
        elif out_total < w_out:
            c_out += out_total
            if not same:
                c_ini_o += out_total
        else:
            c_out += out_total - w_out
        w_out = out_total
    c.execute("INSERT OR REPLACE INTO model_ledger (key, model, version, engine, "
              "in_tokens_cum, out_tokens_cum, in_initial_cum, out_initial_cum, "
              "first_ts, last_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
              (key, model, version, engine, c_in, c_out, c_ini, c_ini_o, first, now))
    c.execute("INSERT OR REPLACE INTO model_watermarks (port, key, model, "
              "version, engine, in_tokens_max, out_tokens_max) VALUES (?,?,?,?,?,?,?)",
              (port, key, model, version, engine, w_in, w_out))


def model_ledger_all(c):
    """Per-(model, version, engine) lifetime totals, heaviest first.
    observed_in/out = cum minus the initial (unobserved) part."""
    rows = c.execute("SELECT key, model, version, engine, in_tokens_cum, "
                     "out_tokens_cum, in_initial_cum, out_initial_cum, first_ts, "
                     "last_ts FROM model_ledger ORDER BY "
                     "(in_tokens_cum+out_tokens_cum) DESC").fetchall()
    return [{"key": r["key"], "model": r["model"], "version": r["version"],
             "engine": r["engine"],
             "in_tokens": int(r["in_tokens_cum"] or 0),
             "out_tokens": int(r["out_tokens_cum"] or 0),
             "in_initial": int(r["in_initial_cum"] or 0),
             "out_initial": int(r["out_initial_cum"] or 0),
             "observed_in": int((r["in_tokens_cum"] or 0) - (r["in_initial_cum"] or 0)),
             "observed_out": int((r["out_tokens_cum"] or 0) - (r["out_initial_cum"] or 0)),
             "first_ts": r["first_ts"], "last_ts": r["last_ts"]} for r in rows]


def ledger_latest(c):
    """Newest ledger row (fleet totals live on every port row — all rows in
    one flush share the same energy cumulative; token cumulatives are
    per-port and must be SUMmed over distinct ports)."""
    ports = c.execute("SELECT DISTINCT port FROM ledger").fetchall()
    tot_in = tot_out = 0.0
    for p in ports:
        r = c.execute("SELECT in_tokens_cum, out_tokens_cum FROM ledger "
                      "WHERE port=? ORDER BY ts DESC LIMIT 1",
                      (p["port"],)).fetchone()
        if r:
            tot_in += r["in_tokens_cum"] or 0.0
            tot_out += r["out_tokens_cum"] or 0.0
    e = c.execute("SELECT energy_kwh_cum, ts FROM ledger "
                  "ORDER BY ts DESC LIMIT 1").fetchone()
    since = c.execute("SELECT MIN(ts) AS t0 FROM ledger").fetchone()
    return {
        "in_tokens": int(tot_in),
        "out_tokens": int(tot_out),
        "energy_kwh": round(e["energy_kwh_cum"], 6) if e else 0.0,
        "since_ts": since["t0"] if since else None,
    }


def ledger_series(c, limit=1440):
    """Fleet cumulative series for the all-time chart: per ts bucket, sum of
    each port's latest-at-or-before-ts cumulative + fleet energy cumulative."""
    rows = c.execute("SELECT ts, port, in_tokens_cum, out_tokens_cum, "
                     "energy_kwh_cum FROM ledger ORDER BY ts").fetchall()
    if not rows:
        return {"ts": [], "in_cum": [], "out_cum": [], "kwh_cum": []}
    # decimate by stride over distinct flush timestamps
    flush_ts = sorted({r["ts"] for r in rows})
    if len(flush_ts) > limit:
        stride = -(-len(flush_ts) // limit)
        keep = set(flush_ts[::stride])
        keep.add(flush_ts[-1])
    else:
        keep = set(flush_ts)
    ts_out, in_out, out_out, e_out = [], [], [], []
    # walk rows once, maintaining per-port running cums
    per_port = {}
    e_last = 0.0
    for r in rows:
        per_port[r["port"]] = (r["in_tokens_cum"] or 0.0, r["out_tokens_cum"] or 0.0)
        e_last = r["energy_kwh_cum"] or 0.0
        if r["ts"] in keep:
            ts_out.append(r["ts"])
            in_out.append(int(sum(v[0] for v in per_port.values())))
            out_out.append(int(sum(v[1] for v in per_port.values())))
            e_out.append(round(e_last, 6))
    return {"ts": ts_out, "in_cum": in_out, "out_cum": out_out, "kwh_cum": e_out}


def ledger_backfill(c, since_ts):
    """One-time seed: rebuild the ledger from the samples table (which holds
    in-window 30s token deltas). Sums all retained history per port into an
    initial cumulative row. Called only when the ledger table is empty."""
    n = c.execute("SELECT COUNT(*) AS n FROM ledger").fetchone()["n"]
    if n:
        return 0
    rows = c.execute("SELECT port, SUM(in_tokens) AS i, SUM(out_tokens) AS o, "
                     "MIN(ts) AS t0 FROM samples WHERE ts>=? "
                     "GROUP BY port", (since_ts,)).fetchall()
    t0 = int(time.time())
    e0 = query_energy_kwh(c, 0)  # all retained gpu_hw history
    for r in rows:
        c.execute("INSERT OR REPLACE INTO ledger (ts, port, in_tokens_cum, "
                  "out_tokens_cum, energy_kwh_cum) VALUES (?,?,?,?,?)",
                  (t0, r["port"], float(r["i"] or 0), float(r["o"] or 0), e0))
    # seed fleet energy from retained gpu_hw history
    c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('ledger_backfilled',?)",
              (str(t0),))
    c.commit()
    return len(rows)


# ── data reset (settings → DATA MANAGEMENT) ─────────────────────────────
# Every destructive action snapshots metrics.db first (rolling .bak window)
# and rebases watermarks so the next flush cannot credit the engine's
# lifetime counters as fresh tokens. Actions run on the writer connection
# from the API thread; the collect thread's next flush simply continues
# from the new (empty/rebased) state.

BAK_KEEP = 3


def db_backup(path):
    """Snapshot metrics.db to metrics.db.bak-<ts> (WAL-safe via the backup
    API), keep the newest BAK_KEEP snapshots. Returns the backup path."""
    dest = path + ".bak-" + time.strftime("%Y%m%d_%H%M%S")
    src = sqlite3.connect(path)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    baks = sorted(glob.glob(path + ".bak-*"))
    for old in baks[:-BAK_KEEP]:
        try:
            os.remove(old)
        except OSError:
            pass
    return dest


def _zero_ledger(c, live_counters):
    """Shared T2 core: zero token cumulatives, rebase watermarks to the
    live counter readings (baseline adoption on next flush)."""
    for port, (it, ot) in (live_counters or {}).items():
        c.execute("INSERT OR REPLACE INTO counter_watermarks (port, "
                  "in_tokens_max, out_tokens_max) VALUES (?,?,?)",
                  (port, it, ot))


def reset_windows(c):
    """T1: wipe samples + gpu_hw (chart history). Ledger + watermarks kept."""
    c.execute("DELETE FROM samples")
    c.execute("DELETE FROM gpu_hw")
    c.commit()


def reset_tokens(c, live_counters):
    """T2: zero ALL-TIME token totals (ledger), keep energy + watermarks
    rebased to live counters so uptime isn't double-credited."""
    c.execute("DELETE FROM ledger")
    _zero_ledger(c, live_counters)
    # force re-backfill skip: ledger now empty but backfill would reseed
    # from samples — mark it done so the empty state sticks
    c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('ledger_backfilled',?)",
              (str(int(time.time())),))
    c.commit()


def reset_models(c, live_models):
    """T2b: wipe per-model attribution (model_ledger + model_watermarks),
    rebase each port's model watermark to its live counters so the next
    flush adopts the baseline instead of re-crediting lifetime counters."""
    c.execute("DELETE FROM model_ledger")
    c.execute("DELETE FROM model_watermarks")
    for port, (key, model, version, engine, it, ot) in (live_models or {}).items():
        c.execute("INSERT OR REPLACE INTO model_watermarks (port, key, model, "
                  "version, engine, in_tokens_max, out_tokens_max) VALUES (?,?,?,?,?,?)",
                  (port, key, model, version, engine, it, ot))
    c.commit()


def reset_model(c, key, live_models):
    """Remove ONE (model, version, engine) card. If it's the currently
    running model on some port, rebase that port's watermark so the next
    flush doesn't credit the full lifetime counter."""
    c.execute("DELETE FROM model_ledger WHERE key=?", (key,))
    rows = c.execute("SELECT port FROM model_watermarks WHERE key=?",
                     (key,)).fetchall()
    c.execute("DELETE FROM model_watermarks WHERE key=?", (key,))
    for r in rows:
        if r["port"] in (live_models or {}):
            k, m, v, e, it, ot = live_models[r["port"]]
            c.execute("INSERT OR REPLACE INTO model_watermarks (port, key, model, "
                      "version, engine, in_tokens_max, out_tokens_max) "
                      "VALUES (?,?,?,?,?,?,?)",
                      (r["port"], k, m, v, e, it, ot))
    c.commit()


def reset_energy(c):
    """T3: zero energy + electricity cost, keep tokens. Energy continuity
    anchors (meta) cleared so the next flush starts a fresh integral."""
    c.execute("UPDATE ledger SET energy_kwh_cum=0")
    c.execute("DELETE FROM meta WHERE key IN ('ledger_prev_power','ledger_prev_power_ts')")
    c.commit()


def reset_all(c, live_counters, live_models):
    """T4: nuke everything — samples, gpu_hw, ledger, model_ledger, both
    watermark tables, energy anchors. Backfill marker set so the empty
    state sticks (no reseed from samples)."""
    c.execute("DELETE FROM samples")
    c.execute("DELETE FROM gpu_hw")
    c.execute("DELETE FROM ledger")
    c.execute("DELETE FROM model_ledger")
    c.execute("DELETE FROM counter_watermarks")
    c.execute("DELETE FROM model_watermarks")
    c.execute("DELETE FROM meta WHERE key LIKE 'ledger_%'")
    _zero_ledger(c, live_counters)
    for port, (key, model, version, engine, it, ot) in (live_models or {}).items():
        c.execute("INSERT OR REPLACE INTO model_watermarks (port, key, model, "
                  "version, engine, in_tokens_max, out_tokens_max) "
                  "VALUES (?,?,?,?,?,?,?)",
                  (port, key, model, version, engine, it, ot))
    c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('ledger_backfilled',?)",
              (str(int(time.time())),))
    c.commit()
