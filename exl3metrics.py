"""EXL3 lane (TabbyAPI + exllamav3) metrics bridge for the gx10 dashboard.

TabbyAPI exposes no /metrics endpoint (verified live + upstream; the rich
Live console display never starts without a tty). What it DOES emit with
log_live_status=true is a per-request INFO line PAIR in the journal:

  start : #<id> chat/completions (stream): <ctx> prompt tokens · max_tokens: …
  finish: #<id> chat/completions (stream): <gen> tokens generated at <t> T/s ·
          prompt <p> tokens, <pct>% cached|none cached, <new> new [in <s> s
          (<r> T/s)] · [queued <q> s, ]first token <f> s, total <t> s ·
          [draft <acc>/<tot> accepted (<nn>%)] [· <finish clause>]

Rich wraps lines at 80 chars and journald stores each fragment as its own
entry; fetch() reassembles by the leading-whitespace continuation rule
(verified live -o json: continuations begin with spaces, MESSAGE carries
only human text — the `extra` dict does NOT land as journal fields).

build_sample() turns the store into the exact dict shape promparse.parse()
returns ({counters,gauges,histograms}), so the dashboard's generic vLLM
pipeline (_window_stats/_engine_series/_vllm_live_rates/_to_db_row/ledger)
consumes it with no per-engine branches — same trick as the sglang/ds4
alias shims; only the "scrape" reads journald instead of HTTP.

Honesty ledger (exact vs derived — mirrors the UI expectation):
  EXACT   prompt/gen token totals, request counts, per-request TTFT and
          E2E (histogram values), cached tokens, new/computed tokens,
          draft accepted/drafted sums, queue seconds WHEN logged.
  DERIVED TPOT = 1/logged decode T/s (the MTP generator times EMITTED
          tokens, so this is real per-token latency, not a spec artifact).
  ABSENT  KV occupancy (no per-request or aggregate readout exists —
          never estimated from cache_size), live queue depth, finish
          reasons the log doesn't state (partial split by design: normal
          stops log nothing; unlabeled remainder marked '?'), HTTP rates,
          preemptions, per-position spec acceptance.

Fetch is incremental through journalctl --cursor-file (one tiny subprocess
per engine, rate-limited). Engine restart: the dashboard calls reset() on
pid change, zeroing lifetime counters — the same monotonic-reset contract
a real vLLM restart gives the pipeline (deltas read None, ring refills).
"""
import json
import os
import re
import subprocess
import threading
import time

# Sample signature — present as a counter in every synthetic sample so
# backend detection can tell an EXL3 lane from llama/vllm/sglang/ds4 by
# metric namespace alone (it is NOT aliased to a vllm name).
SIGNATURE = "exl3:decoded_tokens_total"

_NUM = r"\d[\d,]*(?:\.\d+)?"

_RE_CANCEL = re.compile(
    r"#(?P<id>\d+)\s+\S*completions\b.*client disconnected")
_RE_START = re.compile(
    r"#(?P<id>\d+)\s+\S*completions\b(?:\s*\(stream\))?:?\s+"
    r"(?P<tok>" + _NUM + r")\s+prompt tokens\b")
_RE_FIN = re.compile(
    r"#(?P<id>\d+)\s+\S*completions\b.*?"
    r"(?P<gen>" + _NUM + r")\s+tokens generated at\s+(?P<tps>" + _NUM + r")\s*T/s.*?"
    r"prompt\s+(?P<tok>" + _NUM + r")\s+tokens,\s+"
    r"(?:(?P<pct>\d{1,3})%\s+cached|none cached),\s+(?P<new>" + _NUM + r")\s+new"
    r"(?:,?\s+cached\s+(?P<cached>" + _NUM + r"))?"          # variant B only
    r"(?:\s+in\s+(?P<pf_s>" + _NUM + r")\s+s(?:\s+\((?P<pf_tps>" + _NUM + r")\s*T/s\))?)?"
    r"\s*·\s*(?:queued\s+(?P<queue>" + _NUM + r")\s+s,\s*)?"
    r"first token\s+(?P<ttft>" + _NUM + r")\s+s,\s*total\s+(?P<total>" + _NUM + r")\s+s"
    r"(?:.*?draft\s+(?P<acc>" + _NUM + r")/(?P<dr>" + _NUM + r"))?")
# Finish clause: tabby logs WHY only for unremarkable endings (max_tokens,
# stop string, loop detected, context truncated). Normal stop logs nothing.
_RE_FIN_CLAUSE = re.compile(
    r"(?:max_tokens reached|\((?:\d+) hit stop string .+|loop detected|\(context truncated\))\s*$")

_EVENTS_MAX = 4096          # per-request records kept for histograms (cap)
_OPEN_MAX_AGE = 600.0       # drop open requests older than this (leaked on
                            # client abort with no terminal log line). 10 min
                            # bounds the lie; inflight must not hang on.
_BOOTSTRAP_SINCE = "3 days ago"


def _n(s):
    """'1,048,576' / '26.8' -> float; None-safe."""
    if s is None or s == "":
        return None
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


def _log_clause_reason(msg):
    """The stated finish clause -> short label, or None (normal stop logs nothing)."""
    tail = msg[-60:]
    if "max_tokens reached" in tail:
        return "max_tokens"
    if "hit stop string" in tail:
        return "stop_string"
    if "loop detected" in tail:
        return "loop"
    if "(context truncated)" in tail:
        return "truncated"
    return None


class Exl3Store:
    """Per-engine store: parse journal finish/start lines into per-request
    records + lifetime sums; emit promparse-shaped samples on demand."""

    def __init__(self):
        self.lock = threading.Lock()
        self.reset()
        self._last_fetch = 0.0

    def reset(self):
        self.open = {}         # seq -> {ts, in_tok, stream}
        self.events = []       # finished per-request records (chronological, capped)
        self.sums = {"in": 0.0, "out": 0.0, "cached": 0.0, "acc": 0.0, "dr": 0.0}
        self.reason = {}       # stated finish clause -> count (partial split)
        self.last_tps = None   # (decode, prefill) T/s of the newest finish
        self.last_mode = None
        self._fin_ids = {}     # seq -> newest finish ts (dedupe additive refetch)
        self._max_id = -1
        self._dirty = True     # hist caches invalid
        self._hc = {}          # hist cache keyed by field name

    # ------------------------------------------------------------------
    def feed(self, entries):
        """Ingest [(ts, logical_message)] chronological → True if anything new."""
        got = False
        for ts, msg in entries:
            if "tokens generated at" in msg:
                if self._parse_finish(ts, msg):
                    got = True
            elif _RE_CANCEL.search(msg):
                # client hung up mid-generation: no finish line will EVER
                # arrive for this id. Close the open entry now — leaving it
                # inflates running/slot occupancy until the TTL expires.
                seq = int(_RE_CANCEL.search(msg).group("id"))
                if self.open.pop(seq, None) is not None:
                    self.reason["client disconnected"] = \
                        self.reason.get("client disconnected", 0) + 1
                    got = True
            elif "prompt tokens" in msg and "#" in msg:
                self._parse_start(ts, msg)
        if got:
            self._dirty = True
        return got

    def _parse_start(self, ts, msg):
        m = _RE_START.search(msg)
        if not m:
            return
        seq = int(m.group("id"))
        if seq < self._max_id - 64:
            # engine restarted and reuses low ids: derived live state is stale
            self.open.clear()
        self._max_id = max(self._max_id, seq)
        self.open[seq] = {"ts": ts, "in_tok": _n(m.group("tok")) or 0.0,
                          "stream": "(stream" in msg}

    def _parse_finish(self, ts, msg):
        m = _RE_FIN.search(msg)
        if not m:
            return False
        seq = int(m.group("id"))
        prev_fin = self._fin_ids.get(seq)
        if prev_fin is not None and abs(prev_fin - ts) < 0.001:
            return False                     # same record, repeated fetch
        tok = _n(m.group("tok"))
        if tok is None:
            tok = (self.open.get(seq) or {}).get("in_tok") or 0.0
        new = _n(m.group("new")) or 0.0
        # The log surfaces the cache split only as 'NN% cached'/'none cached'
        # (real capture, 24 h, 224 finishes) — never a raw hit count. `new`
        # IS the non-cached remainder, so cached = tok - new is EXACT, not
        # an estimate (cross-checked: 33,330 tok / 1,586 new -> 95.2% ==
        # logged 95%).
        cached = max(0.0, tok - new)
        ttft = _n(m.group("ttft")) or 0.0
        total = _n(m.group("total")) or 0.0
        tps = _n(m.group("tps")) or 0.0
        rec = {
            "ts": ts,
            "in": tok,
            "out": _n(m.group("gen")) or 0.0,
            "cached": cached,
            "computed": max(0.0, tok - cached),
            "ttft": ttft,
            "e2e": total,
            "queue": _n(m.group("queue")),            # None unless logged
            "tpop": (1.0 / tps) if tps else 0.0,      # EXACT emitted-token ITL
            "decode": max(0.0, total - ttft),
            "prefill": _n(m.group("pf_s")),           # None when new < rate-min
            "pf_tps": _n(m.group("pf_tps")),
            "acc": _n(m.group("acc")) or 0.0,
            "dr": _n(m.group("dr")) or 0.0,
            "reason": _log_clause_reason(msg),        # None = unremarkable stop
            "stream": (self.open.get(seq) or {}).get("stream"),
        }
        self.open.pop(seq, None)
        self._fin_ids[seq] = ts
        self.events.append(rec)
        self._max_id = max(self._max_id, seq)
        if len(self._fin_ids) > 65536:
            self._fin_ids.clear()
        self.sums["in"] += rec["in"]
        self.sums["out"] += rec["out"]
        self.sums["cached"] += rec["cached"]
        self.sums["acc"] += rec["acc"]
        self.sums["dr"] += rec["dr"]
        key = rec["reason"] or "?"
        self.reason[key] = self.reason.get(key, 0) + 1
        if tps:
            self.last_tps = (tps, rec["pf_tps"] or (self.last_tps or (None, None))[1])
        mm = re.search(r"mode\s+(mtp|draft|promptlookup|ngram|suffix|sa|chain|typical|eagle)", msg)
        if mm:
            self.last_mode = mm.group(1)
        # prune + prune-consistency: hist cap uses newest records, seqs too
        drop = len(self.events) - _EVENTS_MAX
        if drop > 0:
            del self.events[:drop]
        # expire leaked opens (client aborted without a finish line)
        cutoff = ts - _OPEN_MAX_AGE
        for s in [s for s, v in self.open.items() if v["ts"] < cutoff]:
            self.open.pop(s, None)
        return True

    # ------------------------------------------------------------------
    def _hist(self, field):
        """Cumulative histogram from the exact per-request values of `field`.

        Buckets ARE the observed values (le=value) + a +Inf top bucket, so
        _hist_delta between two samples gives exact window observations and
        promparse.percentile interpolates with zero error inside a band.
        Cached until the next finish (only true on monotonic growth: the
        events ring prunes oldest only, so bucket counts for fixed le are
        stable — pruning also moves the newest list's start up, which is
        fine: _hist_delta returns None on any non-monotonic bucket, i.e.
        ring-reset semantics).
        """
        hit = self._hc.get(field)
        if hit is not None and not self._dirty:
            return hit
        vals = [r[field] for r in self.events if r.get(field)]
        counts = {}
        for v in vals:
            counts[v] = counts.get(v, 0) + 1
        buckets, cum = [], 0
        for v in sorted(counts):
            cum += counts[v]
            buckets.append((v, cum))
        h = {"buckets": buckets, "sum": float(sum(vals)), "count": len(vals)}
        self._hc[field] = h
        return h

    def build_sample(self, now=None, live=True):
        """promparse-shaped sample of the engine's current state.

        Only exl3:* names — the dashboard's _with_exl3_aliases() maps them
        onto the vLLM shapes; nothing here fabricates a vLLM series."""
        now = now or time.time()
        with self.lock:
            # closing live state snapshot under lock
            if not live or not self.open:
                n_open = 0
            else:
                cutoff = now - _OPEN_MAX_AGE
                n_open = sum(1 for v in self.open.values() if v["ts"] >= cutoff)

            def C(name, val):
                return {"value": val, "labels": {},
                        "series": [{"value": val, "labels": {}}]}

            ctr = {
                "exl3:prompt_tokens_total": C("exl3:prompt_tokens_total", self.sums["in"]),
                SIGNATURE: C(SIGNATURE, self.sums["out"]),
                "exl3:requests_completed_total": C("exl3:requests_completed_total", len(self.events)),
                "exl3:prefix_cache_hits_total": C("exl3:prefix_cache_hits_total", self.sums["cached"]),
                "exl3:prefix_cache_queries_total": C("exl3:prefix_cache_queries_total", self.sums["in"]),
                "exl3:spec_decode_accepted_total": C("exl3:spec_decode_accepted_total", self.sums["acc"]),
                "exl3:spec_decode_draft_total": C("exl3:spec_decode_draft_total", self.sums["dr"]),
            }
            # finish-reason split: only clauses the log states, + '?' bucket for
            # everything else (partial split by design — never backfilled 'stop')
            lbl = max(0, len(self.events) - sum(
                v for k, v in self.reason.items() if k != "?")) if "?" not in self.reason else 0
            series = [{"value": float(v), "labels": {"finished_reason": k}}
                      for k, v in self.reason.items()]
            unlabelled = len(self.events) - sum(int(s["value"]) for s in series)
            if unlabelled > 0:
                series.insert(0, {"value": float(unlabelled), "labels": {}})
            ctr["exl3:request_success_total"] = {"value": float(len(self.events)),
                                                 "labels": {}, "series": series}
            # prompt source split (exact: cached vs computed prompt tokens)
            ctr["exl3:prompt_tokens_by_source_total"] = {
                "value": self.sums["in"], "labels": {},
                "series": [
                    {"value": self.sums["cached"],
                     "labels": {"source": "local_cache_hit"}},
                    {"value": self.sums["in"] - self.sums["cached"],
                     "labels": {"source": "local_compute"}},
                ]}

            g = {}

            def G(name, val):
                g[name] = {"value": val, "labels": {},
                           "series": [{"value": val, "labels": {}}]}

            lt = self.last_tps or (None, None)
            G("exl3:predict_seconds", (1.0 / lt[0]) if lt and lt[0] else None)
            G("exl3:prompt_seconds", (1.0 / lt[1]) if lt and lt[1] else None)
            G("exl3:num_requests_running", float(n_open) if live else None)
            # no live queue-depth readout exists in the log source — None
            # (rendered as a dash), never 0-fabricated
            queue_val = None
            g["exl3:num_requests_waiting"] = {"value": queue_val, "labels": {},
                                              "series": [{"value": queue_val,
                                                          "labels": {}}]}
            # No honest KV occupancy surface exists (capacity is not usage) —
            # absent, per the ds4 missing-KV precedent.

            hh = {}
            for name, field in (
                    ("exl3:time_to_first_token_seconds", "ttft"),
                    ("exl3:e2e_request_latency_seconds", "e2e"),
                    ("exl3:inter_token_latency_seconds", "tpop"),
                    ("exl3:request_queue_time_seconds", "queue"),
                    ):
                hh[name] = self._hist(field)
            # per-request token/time histograms feeding _vllm_live_rates and
            # seat-rate math (same event-based completion semantics as vLLM)
            hh["exl3:request_generation_tokens"] = self._hist("out")
            hh["exl3:request_prefill_kv_computed_tokens"] = self._hist("computed")
            hh["exl3:request_decode_time_seconds"] = self._hist("decode")
            hh["exl3:request_prefill_time_seconds"] = self._hist("prefill")
            self._dirty = False
            return {"counters": ctr, "gauges": g, "histograms": hh}


# ----------------------------------------------------------------------
# journald fetch: cursor-file incremental + line reassembly
# ----------------------------------------------------------------------

def reassemble(rows):
    """journalctl -o json rows → [(ts, logical_message)]. Continuation
    entries (leading whitespace, verified live) join the previous line."""
    out = []
    for r in rows:
        msg = r.get("MESSAGE", "")
        try:
            ts = int(r.get("__REALTIME_TIMESTAMP", "0")) / 1e6
        except (TypeError, ValueError):
            continue
        if msg[:1] in (" ", "\t") and out:
            out[-1] = (out[-1][0], out[-1][1] + " " + msg.strip())
        else:
            out.append((ts, msg))
    return out


def fetch(store, unit, cursor_file, min_interval=2.0, timeout=10):
    """Incremental journalctl fetch for `unit` into `store`. Returns True if
    new finish lines landed. Rate-limited per store; bootstrap (missing
    cursor file) back-logs the last days of history once."""
    now = time.time()
    if now - store._last_fetch < min_interval:
        return False
    store._last_fetch = now
    cmd = ["journalctl", "-u", unit, "--no-pager", "-q", "-o", "json",
           f"--cursor-file={cursor_file}"]
    if not os.path.isfile(cursor_file):
        cmd += ["--since", _BOOTSTRAP_SINCE]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return False
    if proc.returncode != 0:
        return False
    rows = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    if not rows:
        return False
    return store.feed(reassemble(rows))


# ----------------------------------------------------------------------
# draft/spec config: tabby's YAML is the source of truth (the CLI loads
# drop draft_* keys entirely), so read the minimal keys by line scan —
# stdlib only, no yaml package.
# ----------------------------------------------------------------------

_DRAFT_CACHE = {"ts": 0.0, "cfg_path": None, "mode": None, "n": None}
_DRAFT_TTL = 300.0


def draft_spec(cfg_path):
    """(draft_mode, draft_num_tokens) from tabby-config.yml's draft_model
    section, or (None, None). Cached 5 min per config path."""
    now = time.time()
    if (now - _DRAFT_CACHE["ts"] < _DRAFT_TTL
            and _DRAFT_CACHE["cfg_path"] == cfg_path and cfg_path):
        return _DRAFT_CACHE["mode"], _DRAFT_CACHE["n"]
    mode = num = None
    try:
        with open(cfg_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(65536)
        m = re.search(r"^\s*draft_mode:\s*(\S+)", text, re.M)
        if m:
            mode = m.group(1)
        m = re.search(r"^\s*draft_num_tokens:\s*(\d+)", text, re.M)
        if m:
            num = int(m.group(1))
    except OSError:
        pass
    if cfg_path:
        _DRAFT_CACHE.update({"ts": now, "cfg_path": cfg_path,
                             "mode": mode, "n": num})
    return mode, num


# ---------------------------------------------------------------------------
# Dashboard scrape glue (called from dashboard.scrape_engines per poll).
# ---------------------------------------------------------------------------

_HEALTH_TIMEOUT = 10.0  # TabbyAPI serializes /health behind active
                        # generation: measured 4.0 s worst case under 4-way
                        # batched decode. A short timeout flaps the lane.
# Consecutive probe misses tolerated before the lane is marked down.
# Real outages surface after ~3 polls; single blips never blank the UI.
_HEALTH_FLAP_GATE = 3

# unit name -> Exl3Store: one rolling store per lane, monotonic counters live
# here across scrapes (journal reads are incremental).
_LANE_STORES = {}


def _lane_store(unit):
    st = _LANE_STORES.get(unit)
    if st is None:
        st = _LANE_STORES.setdefault(unit, Exl3Store())
    return st


def scrape_lane(port, unit, st, cursor_path):
    """One poll of an EXL3 lane. Liveness via GET /health (the only HTTP
    surface tabby serves); metrics via the incremental journal read. Appends
    a promparse-shaped sample into st["samples"], maintains st["up"] /
    st["has_metrics"] exactly like an HTTP scrape would. Returns the parsed
    sample or None when the lane is down. Never raises on journal/IO errors:
    a missing cursor dir or a journald hiccup degrades to no-metrics."""
    import urllib.request
    store = _lane_store(unit)
    up = False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health",
                                    timeout=_HEALTH_TIMEOUT):
            up = True
    except Exception:
        up = False
    # Debounced liveness: TabbyAPI's event loop stalls 0.6-1.0 s under
    # batched generation, so a single-shot probe flaps the lane down on
    # busy boxes (measured: 14/45 polls down under 4-way load). Mark down
    # only after _HEALTH_FLAP_GATE consecutive misses; transient blips
    # keep the lane up and its last data on screen.
    fails = st.get("_health_fails", 0)
    if up:
        fails = 0
    else:
        fails += 1
        up = fails < _HEALTH_FLAP_GATE
    st["_health_fails"] = fails
    st["up"] = up
    if not up:
        st["has_metrics"] = False
        return None
    try:
        fetch(store, unit, cursor_path, min_interval=1.5)
    except Exception:
        pass  # journal unavailable right now: keep last cumulative state
    parsed = store.build_sample()
    if not parsed["counters"]:
        # boot    -  no request ever captured: the honest empty state. Still
        # report has_metrics so the lane renders as exl3, not unknown.
        st["has_metrics"] = True
        return None
    st["has_metrics"] = True
    return parsed

