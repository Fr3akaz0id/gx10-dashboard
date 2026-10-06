"""Native serve_native.py EXL3 lanes - JSON journal grammar.

Sibling of exl3metrics.py (TabbyAPI/prose grammar), NOT an extension of it: the
TabbyAPI regex family cannot match a JSON line, and bolting a second dialect onto
one store means every feed() branches on line shape. Both emit the SAME exl3:*
namespace so the alias layer, _window_stats, the model ledger and the sparklines
stay grammar-agnostic.

Authoritative field contract read from the server source
(recipe/server/serve_native.py:62-68, :213-214):

    {"request_id": str, "finish_reason": str, "usage": {
        "prompt_tokens": int, "completion_tokens": int, "total_tokens": int,
        "decode_tok_s": float, "draft_accept": float|None,
        "time_prefill": float, "time_generate": float, "time_enqueued": float}}

draft_accept is None when no drafter ran - that is a fact about the request, not
a zero acceptance rate, and must not be counted as one.

FIELD -> CARD HONESTY (see skills reference json-grammar-exl3-lanes.md):
  decode_tok_s   -> TPOT = 1/rate -> inter_token_latency_seconds histogram + live tok/s
  draft_accept   -> spec acceptance ONLY. It is a RATIO; a ratio cannot be
                    inverted into counts, so spec_decode_num_draft_tokens_total
                    is deliberately NOT synthesized.
  time_prefill   -> prefill compute-time series
  time_generate  -> decode compute-time series
  time_enqueued  -> request_queue_time_seconds histogram
  finish_reason  -> request_success_total{finished_reason} - COMPLETE here, every
                    row is labelled (unlike TabbyAPI's partial split)
  ABSENT         -> no cached/new split, so prefix-cache hit rate is UNMEASURABLE.
                    Omit BOTH prefix_cache_hits_total and prefix_cache_queries_total:
                    emitting queries-only renders a fictitious 0% hit rate that
                    reads as "cache never hits" instead of "we cannot tell".
  ABSENT         -> no KV occupancy readout; KV card stays dark.
  ABSENT         -> TTFT and e2e-total have no source; those cards stay dark.
"""

import json
import os
import re
import time

SIGNATURE = "exl3:decoded_tokens_total"

# The serve_native.py completion line, embedded in a journal MESSAGE.
_REQ = re.compile(
    r'\{"request_id":\s*"(?P<rid>[^"]+)",\s*"finish_reason":\s*"(?P<fin>[^"]*)",\s*'
    r'"usage":\s*\{(?P<u>.*?)\}\s*\}\s*$'
)

_EVENTS_MAX = 20000
_OPEN_MAX_AGE = 1800.0  # seconds; unclosed rows older than this are decayed


def _f(d, k):
    """float from a usage dict, tolerating None/missing/str."""
    v = d.get(k)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(d, k):
    v = _f(d, k)
    return int(v) if v is not None else 0


def _c(name, value, labels=None, series=None):
    """Bare metric dict. Must NOT wrap in {name: {...}} - callers wrap."""
    out = {"value": value, "labels": labels or {}}
    if series is not None:
        out["series"] = series
    return out


def _g(name, value):
    return {"value": value, "labels": {}}


def _bucket(v, edges):
    for e in edges:
        if v <= e:
            return e
    return edges[-1]


class NativeExl3Store:
    """One per (unit, port) lane. Feeds JSON request lines, emits a
    promparse-shaped sample per poll."""

    def __init__(self, unit, port, slot_cap=None):
        self.unit = unit
        self.port = port
        self.slot_cap = slot_cap
        self.seen = set()          # request ids already folded in
        self.open_ids = {}         # rid -> first-seen ts
        self.totals = {
            "prompt": 0, "completion": 0, "requests": 0,
            "spec_accepted": 0.0, "spec_queries": 0,
        }
        self.rows = []             # recent completed rows for histograms
        self.spec_seen = 0         # requests that actually ran a drafter

    # ---- parsing -------------------------------------------------------
    @staticmethod
    def parse_line(msg):
        """Return (rid, finish, usage_dict) or None. Never raises."""
        if not msg:
            return None
        m = _REQ.search(msg.strip())
        if not m:
            return None
        try:
            u = json.loads("{" + m.group("u") + "}")
        except (ValueError, TypeError):
            return None
        return m.group("rid"), m.group("fin"), u

    def feed(self, msg, now=None):
        """Fold one journal MESSAGE. Returns True if it was a request line."""
        now = now or time.time()
        p = self.parse_line(msg)
        if p is None:
            return False
        rid, fin, u = p
        if rid in self.seen:
            return True
        self.seen.add(rid)

        ptok = _i(u, "prompt_tokens")
        ctok = _i(u, "completion_tokens")
        gen = _f(u, "time_generate")
        rate = _f(u, "decode_tok_s")
        acc = _f(u, "draft_accept")

        self.totals["prompt"] += ptok
        self.totals["completion"] += ctok
        self.totals["requests"] += 1

        # spec acceptance: a RATIO only. Count queries, never back out tokens.
        if acc is not None:
            self.totals["spec_queries"] += 1
            self.totals["spec_accepted"] += acc
            self.spec_seen += 1

        self.rows.append({
            "fin": fin or "?", "ptok": ptok, "ctok": ctok,
            "tpot": (1.0 / rate) if rate else None,
            "prefill": _f(u, "time_prefill"),
            "decode": gen,
            "queued": _f(u, "time_enqueued"),
            "ts": now,
        })
        if len(self.rows) > _EVENTS_MAX:
            self.rows = self.rows[-_EVENTS_MAX:]
        return True

    # ---- sample synthesis ---------------------------------------------
    def sample(self, up=True):
        """promparse-shaped sample in the exl3:* namespace."""
        ctr, gau, hist = {}, {}, {}

        ctr["exl3:prompt_tokens_total"] = _c(
            "exl3:prompt_tokens_total", self.totals["prompt"])
        ctr[SIGNATURE] = _c(SIGNATURE, self.totals["completion"])
        ctr["exl3:requests_completed_total"] = _c(
            "exl3:requests_completed_total", self.totals["requests"])

        # spec: acceptance as a ratio, queries as a denominator. NO token totals.
        if self.spec_seen:
            ctr["exl3:spec_acceptance_ratio"] = _c(
                "exl3:spec_acceptance_ratio",
                self.totals["spec_accepted"] / self.spec_seen)

        # finish split - COMPLETE, every row is labelled by the server
        split = {}
        for r in self.rows:
            split[r["fin"]] = split.get(r["fin"], 0) + 1
        if split:
            ctr["exl3:request_success_total"] = {
                "value": sum(split.values()),
                "labels": {},
                "series": [{"labels": {"finished_reason": k}, "value": v}
                           for k, v in sorted(split.items())],
            }

        gau["exl3:num_requests_running"] = _g(
            "exl3:num_requests_running", len(self.open_ids))
        if self.slot_cap:
            gau["exl3:slot_cap"] = _g("exl3:slot_cap", self.slot_cap)

        # compute-time series
        pf = [r["prefill"] for r in self.rows if r["prefill"] is not None]
        dc = [r["decode"] for r in self.rows if r["decode"] is not None]
        if pf:
            gau["exl3:prompt_seconds_sum"] = _g("exl3:prompt_seconds_sum", sum(pf))
            gau["exl3:prompt_seconds_count"] = _g("exl3:prompt_seconds_count", len(pf))
        if dc:
            gau["exl3:predict_seconds_sum"] = _g("exl3:predict_seconds_sum", sum(dc))
            gau["exl3:predict_seconds_count"] = _g("exl3:predict_seconds_count", len(dc))

        # histograms: aggregate DUPLICATE values first, then cumulate.
        def mk(name, values, edges):
            counts = {}
            for v in values:
                counts[_bucket(v, edges)] = counts.get(_bucket(v, edges), 0) + 1
            cum, out = 0, {}
            for le in sorted(counts):
                cum += counts[le]
                out[f"{name}_bucket{{{le}}}"] = _c(f"{name}_bucket{{{le}}}", cum)
            out[f"{name}_count"] = _c(f"{name}_count", len(values))
            if values:
                out[f"{name}_sum"] = _c(f"{name}_sum", sum(values))
            return out

        tpot = [r["tpot"] for r in self.rows if r["tpot"]]
        q = [r["queued"] for r in self.rows if r["queued"] is not None]
        if tpot:
            hist.update(mk("exl3:inter_token_latency_seconds", tpot,
                           [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0]))
        if q:
            hist.update(mk("exl3:request_queue_time_seconds", q,
                           [0.001, 0.01, 0.1, 0.5, 1.0, 5.0, 30.0]))

        return {"counters": ctr, "gauges": gau, "histograms": hist}


# ---------------------------------------------------------------- lane glue
_LANE_STORES = {}


def _lane_store(unit, port):
    key = (unit, int(port))
    st = _LANE_STORES.get(key)
    if st is None:
        st = NativeExl3Store(unit, int(port))
        _LANE_STORES[key] = st
    return st


def slot_cap_from_cmdline(port):
    """Live read of -ambs / --max-active-requests off the serving process cmdline.
    Profiles differ (2 x 262144 vs 16 x 65536), so NEVER hardcode. Match the
    process actually bound to THIS port."""
    try:
        import subprocess
        out = subprocess.run(
            ["pgrep", "-af", "serve_native.py"],
            capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if not re.search(rf"(?:--port[= ]|:){re.escape(str(port))}\b", line):
            continue
        m = re.search(r"(?:--max-active-requests|-ambs)[= ](\d+)", line)
        if m:
            return int(m.group(1))
    return None


def port_in(unit):
    m = re.search(r":(\d{4,5})", unit or "")
    return m.group(1) if m else ""


def fetch(store, unit, cursor_file, min_interval=15.0, timeout=10.0):
    """Incremental journalctl fetch. Returns True if new rows landed."""
    import subprocess
    now = time.time()
    last = getattr(store, "_last_fetch", 0.0)
    if now - last < min_interval:
        return False
    store._last_fetch = now
    cmd = ["journalctl", "-u", unit, "-o", "json", "--no-pager"]
    if cursor_file and os.path.exists(cursor_file):
        cmd.append(f"--cursor-file={cursor_file}")
    else:
        cmd.append("--since=-2 days")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return False
    if r.returncode != 0:
        return False
    got = False
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        msg = row.get("MESSAGE", "")
        if isinstance(msg, list):  # journald can hand back arrays
            msg = " ".join(str(x) for x in msg)
        if store.feed(msg, now):
            got = True
    return got


_HEALTH_FLAP_GATE = 3


def scrape_lane(port, unit, st, cursor_path):
    """One poll: liveness via /health, metrics via the journal read.
    Owns ONLY st['up'] / st['has_metrics'] - the caller keeps st['samples']."""
    import urllib.request
    store = _lane_store(unit, port)
    up = False
    try:
        urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=10.0).read()
        up = True
    except Exception:
        up = False
    if store.slot_cap is None:
        store.slot_cap = slot_cap_from_cmdline(unit)
    if up:
        fetch(store, unit, cursor_path)
    # Debounced liveness — same contract as exl3metrics: one probe blip
    # (event-loop stall under load) must not blank the lane.
    fails = st.get("_health_fails", 0)
    if up:
        fails = 0
    else:
        fails += 1
        up = fails < _HEALTH_FLAP_GATE
    st["_health_fails"] = fails
    st["up"] = up
    st["has_metrics"] = up and store.totals["requests"] > 0
    return store.sample(up)
