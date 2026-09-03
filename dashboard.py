#!/usr/bin/env python3
"""GX10 metrics dashboard.

Stdlib only. Python HTTP server on :9000 with a self-refreshing single page.
Collects: GPU (nvidia-smi), temps (hwmon/thermal zones), CPU/load, memory,
swap, disk, and live model endpoints (model name + status per port).
"""

import json
import bisect
import difflib
import glob
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.parse
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import engines
import engines_write
import catalog
import promparse
import metadb

HOST = "0.0.0.0"
PORT = 9000
POLL_S = 2.0
HISTORY_LEN = 240  # ~8 min at 2s

# Engine fleet is now config-driven (config.json "engines"). The hard-coded
# lists below are gone; probe_endpoints() and units() read from the live config
# each poll. Config hot-reloads by mtime, so adding/removing an engine in the
# settings page takes effect on the next 2s poll with no restart.

state = {"metrics": None, "lock": threading.Lock()}
history = {
    "ts": deque(maxlen=HISTORY_LEN),
    "cpu": deque(maxlen=HISTORY_LEN),
    "gpu_temp": deque(maxlen=HISTORY_LEN),
    "power": deque(maxlen=HISTORY_LEN),
    "gpu_util": deque(maxlen=HISTORY_LEN),
    "mem_used": deque(maxlen=HISTORY_LEN),
    "load1": deque(maxlen=HISTORY_LEN),
    "soc_max": deque(maxlen=HISTORY_LEN),
}
_last_cpu = {}

# /metrics page state: per-engine scrape ring buffers (15 min at 2 s poll).
# Fast windows (2–15 min) are served from here; long views come from SQLite
# (metadb.py, written at 30 s cadence — see _db_maybe_write).
METRICS_WIN = 450  # samples per engine ring (15 min at 2s poll)
eng_metrics = {
    "engines": {},   # port -> state dict (see scrape_engines)
    "lock": threading.Lock(),
}
# Live GB10 hardware series (2s cadence) for the top "over time" charts.
# Independent of the engine ring — the box always reports its own hardware.
# Each sample: (epoch_float, {sm, temp, power, util, throttle}).
_gpu_live = deque(maxlen=METRICS_WIN)
_poll_count = 0
_db_conn = None


def run(cmd, timeout=5):
    try:
        return subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        ).stdout.strip()
    except Exception:
        return ""


def meminfo():
    d = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            d[k] = int(v.strip().split()[0])  # kB
    return d


def cpu_times():
    with open("/proc/stat") as f:
        parts = f.readline().split()[1:]
    vals = [int(x) for x in parts]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return idle, sum(vals)


def gpu_info():
    out = run(
        "nvidia-smi --query-gpu=name,temperature.gpu,power.draw,utilization.gpu"
        " --format=csv,noheader,nounits"
    )
    g = {"name": "GB10", "temp": None, "power_w": None, "util": None}
    if out:
        p = [x.strip() for x in out.split(",")]
        if len(p) >= 4:
            g["name"] = p[0]
            g["temp"] = int(float(p[1])) if p[1] else None
            g["power_w"] = round(float(p[2]), 1) if p[2] else None
            g["util"] = int(p[3]) if p[3] else None
    apps = run(
        "nvidia-smi --query-compute-apps=pid,used_memory,process_name"
        " --format=csv,noheader,nounits"
    )
    g["apps"] = []
    if apps:
        for line in apps.splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) >= 3:
                g["apps"].append({"pid": p[0], "mem_mib": int(float(p[1])), "name": p[2]})
    return g


def model_for_pid(pid):
    """Derive a human model string from a GPU process's cmdline (or parent's)."""
    for p in (pid, _parent_pid(pid)):
        if not p:
            continue
        try:
            with open(f"/proc/{p}/cmdline", "rb") as f:
                cmd = f.read().decode(errors="replace").replace("\x00", " ")
        except OSError:
            continue
        for pat in (r"--model[= ](\S+)", r"(?<=serve )(\S+)"):
            m = re.search(pat, cmd)
            if m:
                v = m.group(1)
                return v.rsplit("/", 1)[-1] if "/" in v else v
    return None


def _parent_pid(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            rest = f.read().rsplit(")", 1)[1].split()
            return int(rest[1]) or None  # field 4 = ppid (comm is field 2)
    except Exception:
        return None


def temps():
    out = run("sensors 2>/dev/null")
    t = {"soc_max": None, "soc_avg": None, "nvme": None, "zones": []}
    acpi, nvme = [], None
    section = None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith(("acpitz", "nvme", "mt7925")):
            section = s.split("-")[0].split("pci")[0].lower()
            continue
        m = re.match(r"(?:temp\d|Composite|Sensor 1|vcore):?\s*:\s*\+?([\d.]+)°C", s)
        if not m:
            m = re.match(r"(?:temp\d|Composite|Sensor 1):?\s+([\d.]+)°C", s)
        if m and section in ("acpitz", "nvme"):
            v = float(m.group(1))
            if section == "acpitz":
                acpi.append(v)
            if section == "nvme" and nvme is None:
                nvme = v
    # Fallback: raw thermal zones if sensors parsing missed
    if not acpi:
        for z in os.listdir("/sys/class/thermal"):
            try:
                acpi.append(int(open(f"/sys/class/thermal/{z}/temp").read().strip()) / 1000)
            except Exception:
                pass
    if acpi:
        t["zones"] = [round(v, 1) for v in acpi]
        t["soc_max"] = round(max(acpi), 1)
        t["soc_avg"] = round(sum(acpi) / len(acpi), 1)
    if nvme is not None:
        t["nvme"] = round(nvme, 1)
    return t


def _cfg_units():
    """Configured unit engines as {unit_name: cfg_entry}."""
    _raw, cfg, _err = catalog.read_config()
    return {
        e["name"]: e
        for e in cfg.get("engines", [])
        if e.get("kind") == "unit" and e.get("enabled", True)
    }


_UNIT_STATE_CACHE = {"ts": 0.0, "map": None}


def _unit_active_states():
    """Batch {unit: active_state} for all configured unit engines, cached for a
    sub-poll window so probe_endpoints() and units() share one systemctl call
    per poll instead of one per unit."""
    global _UNIT_STATE_CACHE
    now = time.time()
    if _UNIT_STATE_CACHE["map"] is not None and now - _UNIT_STATE_CACHE["ts"] < 1.5:
        return _UNIT_STATE_CACHE["map"]
    names = list(_cfg_units().keys())
    m = {}
    if names:
        out = run("systemctl is-active " + " ".join(names) + " 2>/dev/null").split()
        for i, n in enumerate(names):
            m[n] = out[i] if i < len(out) else "unknown"
    _UNIT_STATE_CACHE.update(ts=now, map=m)
    return m


def probe_endpoints():
    """Probe each configured unit engine's /v1/models port.

    Reads the live config, so an engine added/removed on the settings page
    appears/disappears on the next 2s poll. Port comes from config (which the
    discovery import seeds from the unit file); a unit's actual port wins when
    it's present and valid."""
    active_map = _unit_active_states()
    models = []
    for unit, entry in _cfg_units().items():
        # derive label: config label or unit basename
        label = entry.get("label") or unit.replace(".service", "")
        # port: config port, else parse the unit file
        port = entry.get("port")
        if not port:
            try:
                d = engines.parse_unit(f"/etc/systemd/system/{unit}")["derived"]
                if str(d.get("port") or "").isdigit():
                    port = int(d["port"])
            except Exception:
                port = None
        active = active_map.get(unit, "unknown")
        note = f"{entry.get('model') or ''}" if entry.get("model") else ""
        up = False
        model = None
        if port:
            try:
                req = urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/v1/models", timeout=1.5
                )
                data = json.load(req)
                up = True
                ids = [m.get("id") for m in data.get("data", [])]
                model = ids[0] if ids else None
            except Exception:
                pass
        models.append({
            "label": label,
            "port": port,
            "note": note,
            "engine": unit,
            "up": up,
            "model": model,
            "active": active,
        })
    # docker engines: include their ports in the overview probe too
    _raw, cfg, _err = catalog.read_config()
    for e in cfg.get("engines", []):
        if e.get("kind") != "docker" or not e.get("enabled", True):
            continue
        name = e["name"]
        port = e.get("port")
        up, model = False, None
        if port:
            try:
                req = urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/v1/models", timeout=1.5
                )
                data = json.load(req)
                up = True
                ids = [m.get("id") for m in data.get("data", [])]
                model = ids[0] if ids else None
            except Exception:
                pass
        models.append({
            "label": e.get("label") or name,
            "port": port,
            "note": e.get("model") or "",
            "engine": name,
            "up": up,
            "model": model,
            "active": "active" if up else "inactive",
            "kind": "docker",
        })
    # port engines (bare endpoints, e.g. an sglang/vllm server not in a unit
    # or container): name is the label, port is the handle, /v1/models owned_by
    # tells us the serving engine.
    for e in cfg.get("engines", []):
        if e.get("kind") != "port" or not e.get("enabled", True):
            continue
        name = e["name"]
        port = e.get("port")
        up, model, owned = False, None, None
        if port:
            try:
                req = urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/v1/models", timeout=1.5
                )
                data = json.load(req)
                up = True
                dd = data.get("data", [])
                ids = [m.get("id") for m in dd]
                model = ids[0] if ids else None
                owned = dd[0].get("owned_by") if dd else None
            except Exception:
                pass
        models.append({
            "label": e.get("label") or name,
            "port": port,
            "note": e.get("model") or "",
            "engine": name,
            "up": up,
            "model": model,
            "owned_by": owned,
            "active": "active" if up else "inactive",
            "kind": "port",
        })
    return models


def units():
    """State map for all configured unit engines: {unit: active_state}."""
    return _unit_active_states()


def top_rss():
    out = run("ps -eo pid,rss,comm --sort=-rss | sed 1d | head -3")
    procs = []
    for line in out.splitlines():
        p = line.split(None, 2)
        if len(p) == 3:
            procs.append({"pid": p[0], "rss_gib": round(int(p[1]) / 1048576, 2), "cmd": p[2]})
    return procs


def disk():
    st = os.statvfs("/")
    total = st.f_blocks * st.f_frsize
    used = total - st.f_bavail * st.f_frsize
    inodes_total = st.f_files
    inodes_used = inodes_total - st.f_ffree
    return {
        "total_gib": round(total / 1073741824, 0),
        "used_gib": round(used / 1073741824, 0),
        "pct": round(100 * used / total, 1) if total else 0,
        "inode_pct": round(100 * inodes_used / inodes_total, 1) if inodes_total else None,
    }


def _c(m, name):
    v = (m or {}).get("counters", {}).get(name)
    return v.get("value") if v else None


def _g(m, name):
    v = (m or {}).get("gauges", {}).get(name)
    return v.get("value") if v else None


def _c_sum(m, name):
    """Summed counter over all label sets (the sum(...) query)."""
    return promparse.sum_all(m, "counters", name)


def _g_sum(m, name):
    """Summed gauge over all label sets."""
    return promparse.sum_all(m, "gauges", name)


def _c_lab(m, name, labels):
    """One label-set of a counter (e.g. request_success_total{reason=stop})."""
    return promparse.value_by_label(m, "counters", name, labels)


def _g_lab(m, name, labels):
    """One label-set of a gauge (e.g. num_requests_waiting_by_reason{reason})."""
    return promparse.value_by_label(m, "gauges", name, labels)


def _delta(prev, cur):
    """Counter delta; None on reset (engine restart) or missing sample."""
    if prev is None or cur is None:
        return None
    d = cur - prev
    return d if d >= 0 else None


def _hist_delta(prev_h, cur_h):
    """Per-bucket deltas for a histogram between two samples. None on any reset."""
    if not prev_h or not cur_h:
        return None
    pb = dict(prev_h.get("buckets", []))
    out = []
    for le, cc in cur_h.get("buckets", []):
        d = cc - pb.get(le, 0)
        if d < 0:
            return None
        out.append((le, d))
    ds = _delta(prev_h.get("sum"), cur_h.get("sum"))
    dc = _delta(prev_h.get("count"), cur_h.get("count"))
    if ds is None or dc is None:
        return None
    return {"buckets": out, "sum": ds, "count": dc}


def _with_llamacpp_aliases(parsed):
    """Map llama.cpp --metrics (llamacpp:*) onto vLLM-shaped alias entries so
    the generic vLLM pipeline (_window_stats/_engine_series/_to_db_row) reads
    llama servers too. Original llamacpp:* entries stay in place.

    Mapping (verified against the live surface, 2026-08-19, 11 series, no
    histograms, no kv/prefix/http/spec metrics):
      llamacpp:requests_processing (gauge)      -> vllm:num_requests_running
      llamacpp:requests_deferred (gauge)        -> vllm:num_requests_waiting
      llamacpp:prompt_tokens_total (counter)    -> vllm:prompt_tokens_total
      llamacpp:tokens_predicted_total (counter) -> vllm:generation_tokens_total
      llamacpp:tokens_predicted_seconds_total
               (counter)                        -> vllm:generation_tokens_seconds_total
      llamacpp:prompt_tokens_seconds (gauge)    -> vllm:prompt_seconds
               (instantaneous per-prompt-token seconds = ttft)
      llamacpp:predicted_tokens_seconds (gauge) -> vllm:predicted_tokens_seconds
               (instantaneous per-output-token seconds = tpot)

    Not exposed by llama.cpp, stays None (page renders a dash): kv usage,
    prefix cache, spec decode, finish reasons, prompt source split, http
    status rates, preemptions, queue/e2e histograms. TTFT has no prompt
    count to pair with prompt_seconds_total; TPOT mean is derived in
    _window_stats from the time/token counters instead of a histogram.
    Note: the two *_tokens_seconds gauges are llama.cpp's AVERAGE THROUGHPUT
    in tokens/s (verified against the live HELP text, 2026-08-19: "Average
    prompt/generation throughput in tokens/s") — the pipeline wants
    SECONDS PER TOKEN, so the alias stores 1/x, not x.
    """
    ctr, g = parsed.get("counters", {}), parsed.get("gauges", {})
    if "llamacpp:prompt_tokens_total" not in ctr and "llamacpp:requests_processing" not in g:
        return parsed
    aliases = ((ctr, "llamacpp:prompt_tokens_total", "vllm:prompt_tokens_total"),
               (ctr, "llamacpp:tokens_predicted_total", "vllm:generation_tokens_total"),
               (ctr, "llamacpp:tokens_predicted_seconds_total", "vllm:generation_tokens_seconds_total"),
               (g, "llamacpp:requests_processing", "vllm:num_requests_running"),
               (g, "llamacpp:requests_deferred", "vllm:num_requests_waiting"))
    for table, src, dst in aliases:
        if src in table and dst not in table:
            table[dst] = dict(table[src])
    # throughput (tok/s) gauges -> seconds-per-token gauges (inverted)
    for src, dst in (("llamacpp:prompt_tokens_seconds", "vllm:prompt_seconds"),
                     ("llamacpp:predicted_tokens_seconds", "vllm:predicted_tokens_seconds")):
        if src in g and dst not in g:
            v = g[src].get("value")
            table = g
            table[dst] = dict(g[src])
            table[dst]["value"] = (1.0 / v) if v else None
    # These gauges are AVERAGE bucket values that the fork only refreshes on
    # prompt/decode events (reset_bucket zeros the denominator; the emitted
    # value is then 0 or stale). When no request is processing the last
    # value is a frozen artifact, not a measurement — null it so the
    # frontend shows a gap/dash instead of a fake steady state.
    if g.get("llamacpp:requests_processing", {}).get("value") == 0:
        for dst in ("vllm:prompt_seconds", "vllm:predicted_tokens_seconds"):
            if dst in g:
                g[dst]["value"] = None
    return parsed


def _sum_counter_table(parsed, name):
    """Summed value of a multi-label counter (both label sets). Returns None
    when the series is absent. SGLang's token counters come as
    is_streaming={false,true} — first label set alone would undercount."""
    v = parsed.get("counters", {}).get(name)
    if not v:
        return None
    sers = v.get("series")
    if sers:
        return sum(s["value"] for s in sers)
    return v.get("value")


def _copy_hist(parsed, src, dst):
    """Copy one histogram entry under a new name (shallow, non-mutating)."""
    h = parsed.get("histograms", {}).get(src)
    if h and dst not in parsed["histograms"]:
        parsed["histograms"][dst] = {
            "buckets": list(h.get("buckets", [])),
            "sum": h.get("sum"), "count": h.get("count")}


def _with_sglang_aliases(parsed):
    """Map SGLang /metrics (sglang:*) onto the vLLM-shaped names the generic
    pipeline reads. Verified against the live surface (:30000, 2026-08-21,
    sglang hybrid-Mamba server w/ DFLASH spec decode, --enable-metrics).

    Counters (sglang emits is_streaming={false,true} -> two series each; the
    alias stores the SUMMED value so the vLLM delta path reads the true total
    and _delta() between two summed samples stays monotonic):
      sglang:prompt_tokens_total      -> vllm:prompt_tokens_total
      sglang:generation_tokens_total  -> vllm:generation_tokens_total
      sglang:num_requests_total       -> vllm:request_success_total
    Gauges (fractions rescaled 0-1 -> 0-100 to match the vLLM convention the
    UI expects, e.g. kv_cache_usage_perc):
      sglang:num_running_reqs         -> vllm:num_requests_running
      sglang:num_queue_reqs           -> vllm:num_requests_waiting
      sglang:full_token_usage * 100   -> vllm:kv_cache_usage_perc
      sglang:cache_hit_rate   * 100   -> vllm:prefix_cache_hit_rate
    Latency histograms (multi-label bucket merge already done in
    promparse.parse) — the pipeline reads vllm: names:
      sglang:time_to_first_token_seconds   -> vllm:time_to_first_token_seconds
      sglang:inter_token_latency_seconds   -> vllm:inter_token_latency_seconds
      sglang:queue_time_seconds            -> vllm:queue_time_seconds
      sglang:e2e_request_latency_seconds   -> vllm:e2e_request_latency_seconds
    NOT aliased: num_retracted_reqs is a GAUGE (live count, not a monotonic
    counter) so it is not safe as a per-min preemption rate; spec decode is
    read from its live gauges (spec_accept_rate / spec_accept_length) in
    _engine_spec.
    """
    ctr, g = parsed.get("counters", {}), parsed.get("gauges", {})
    if not any(n in ctr or n in g for n in
               ("sglang:prompt_tokens_total", "sglang:generation_tokens_total",
                "sglang:num_running_reqs")):
        return parsed
    for src, dst in (("sglang:prompt_tokens_total", "vllm:prompt_tokens_total"),
                     ("sglang:generation_tokens_total", "vllm:generation_tokens_total"),
                     ("sglang:num_requests_total", "vllm:request_success_total")):
        if src in ctr and dst not in ctr:
            # NOTE: the alias MUST carry a "series" list — promparse.sum_all()
            # (used by _lifetime_token_counters for the all-time ledger) reads
            # only v["series"] and returns None without it, silently zeroing
            # the model ledger for sglang engines (2026-08-31 bug).
            total = _sum_counter_table(parsed, src)
            ctr[dst] = {"value": total, "labels": {},
                        "series": [{"value": total, "labels": {}}]}
    for src, dst in (("sglang:num_running_reqs", "vllm:num_requests_running"),
                     ("sglang:num_queue_reqs", "vllm:num_requests_waiting")):
        if src in g and dst not in g:
            g[dst] = dict(g[src])
    if "sglang:full_token_usage" in g and "vllm:kv_cache_usage_perc" not in g:
        v = g["sglang:full_token_usage"].get("value")
        g["vllm:kv_cache_usage_perc"] = {"value": (v * 100.0) if v is not None else None,
                                         "labels": {}}
    if "sglang:cache_hit_rate" in g:
        v = g["sglang:cache_hit_rate"].get("value")
        g["vllm:prefix_cache_hit_rate"] = {"value": (v * 100.0) if v is not None else None,
                                           "labels": {}}
    for src, dst in (("sglang:time_to_first_token_seconds", "vllm:time_to_first_token_seconds"),
                     ("sglang:inter_token_latency_seconds", "vllm:inter_token_latency_seconds"),
                     ("sglang:queue_time_seconds", "vllm:queue_time_seconds"),
                     ("sglang:e2e_request_latency_seconds", "vllm:e2e_request_latency_seconds")):
        _copy_hist(parsed, src, dst)
    return parsed


def _with_ds4_aliases(parsed):
    """Map DS4 engine /metrics (ds4:*) onto the vLLM-shaped names the generic
    pipeline reads so token/spec counters and inflight/queue gauges render for
    ds4 engines. Original ds4:* entries stay in place. Verified against the
    live surface (:8000, deepseek-v4-flash, 2026-09-02)."""
    ctr, g = parsed.get("counters", {}), parsed.get("gauges", {})
    for src, dst in (("ds4_tokens_prefilled_total", "vllm:prompt_tokens_total"),
                     ("ds4_tokens_decoded_total", "vllm:generation_tokens_total"),
                     ("ds4_spec_hits_total", "vllm:spec_decode_num_accepted_tokens_total"),
                     ("ds4_spec_drafts_total", "vllm:spec_decode_num_draft_tokens_total")):
        if src in ctr and dst not in ctr:
            total = _sum_counter_table(parsed, src)
            ctr[dst] = {"value": total, "labels": {},
                        "series": [{"value": total, "labels": {}}]}
    for src, dst in (("ds4_requests_inflight", "vllm:num_requests_running"),
                     ("ds4_queue_depth", "vllm:num_requests_waiting")):
        if src in g and dst not in g:
            g[dst] = dict(g[src])
    return parsed


def _is_ds4_sample(sample):
    return "ds4_tokens_decoded_total" in (sample or {}).get("counters", {})


def _is_sglang_sample(sample):
    return any(n in (sample or {}).get("counters", {})
               for n in ("sglang:prompt_tokens_total", "sglang:generation_tokens_total")) \
        or "sglang:num_running_reqs" in (sample or {}).get("gauges", {})


def _short_model(name):
    """Display/ledger name: strip .gguf/.gguf-ish suffix, keep the quant tag.
    'Nail-Qwen3.6-35B-A3B-MTP-UD-Q8_K_XL.gguf' -> 'Nail-Qwen3.6-35B-A3B-MTP-UD-Q8_K_XL'
    minus extension only; quant tags like Q8_K_XL / UD-Q4_K_M stay intact."""
    if not name:
        return name
    base = os.path.basename(str(name))
    for ext in (".gguf", ".GGUF"):
        if base.endswith(ext):
            base = base[:-len(ext)]
    return base


def scrape_engines():
    """Scrape /metrics from every configured engine port. Per-port
    try/except + 1.5s timeout — failures never break the main collect loop.
    State: eng_metrics['engines'][port] =
      {samples: deque[(ts, parsed)], up, has_metrics, model_live, label, kind}
    """
    _raw, cfg, _err = catalog.read_config()
    ports = {}
    for e in cfg.get("engines", []):
        if not e.get("enabled", True):
            continue
        port = e.get("port")
        if port:
            ports[int(port)] = e
    with eng_metrics["lock"]:
        for port, e in ports.items():
            st = eng_metrics["engines"].setdefault(port, {
                "samples": deque(maxlen=METRICS_WIN), "up": False,
                "has_metrics": False, "model_live": None,
                "label": e.get("label") or e.get("name"), "kind": e.get("kind", "unit"),
                "model": e.get("model"), "port": port,
            })
            st["label"] = e.get("label") or e.get("name")
            st["kind"] = e.get("kind", "unit")
            st["model"] = e.get("model")
            try:
                raw = urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/metrics", timeout=1.5).read()
                parsed = promparse.parse(raw.decode("utf-8", "replace"))
                parsed = _with_llamacpp_aliases(parsed)
                parsed = _with_sglang_aliases(parsed)
                parsed = _with_ds4_aliases(parsed)
                st["samples"].append((time.time(), parsed))
                st["up"] = True
                st["has_metrics"] = True
                for src in (parsed["gauges"], parsed["counters"]):
                    for v in src.values():
                        if "model_name" in v.get("labels", {}):
                            st["model_live"] = v["labels"]["model_name"]
                            break
                    if st["model_live"]:
                        break
            except Exception:
                st["has_metrics"] = False
                st["model_live"] = None
                parsed = None
                # /metrics failing does NOT mean the engine is down — a
                # llama-server started without --metrics answers 501 there.
                # Probe /health: llama-server and vLLM both serve it.
                try:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/health", timeout=1.5).read()
                    st["up"] = True
                except Exception:
                    st["up"] = False
            # model identity for the ledger: live metric label wins, then the
            # process cmdline (--model/-m), then config. Computed every scrape
            # so a model swap on the same port is noticed within one poll.
            st["model_key"] = _short_model(st.get("model_live") or st.get("model_cmdline")
                                           or st.get("model") or f"port {port}")
            if not st.get("model_live"):
                proc = _engine_proc(port)
                if proc:
                    m = _arg(proc["args"], "--model") or _arg(proc["args"], "-m") \
                        or _arg(proc["args"], "--model-path")
                    st["model_cmdline"] = os.path.basename(m) if m else None
                    st["model_key"] = _short_model(st.get("model_live") or st.get("model_cmdline")
                                                   or st.get("model") or f"port {port}")
            if st.get("has_metrics") and parsed and "llamacpp:prompt_tokens_total" in parsed.get("counters", {}):
                _update_slots(st)  # llama.cpp: kv occupancy + prefix reuse live in /slots
        known = set(ports)
        for p in [p for p in eng_metrics["engines"] if p not in known]:
            del eng_metrics["engines"][p]


def _update_slots(st):
    """llama.cpp /slots — the only source for KV occupancy + prefix reuse.
    /metrics (hardcoded server-side) exposes neither.

    KV gauge: Σ (prompt + decoded) over slots that still hold a sequence,
    divided by n_ctx × n_slots. An idle slot with n_prompt_tokens>0 is
    genuinely holding KV (prefix reuse across requests); prompt_clear()
    zeroes the counter AND seq_rm's the cells when KV is really released,
    so the number tracks actual occupancy. Slots with prompt==0 are
    skipped (their n_decoded is stale — reset() doesn't clear it).

    Prefix: per-task cached/(cached+processed). n_prompt_tokens_cache is
    set once at prompt start (n_past) and zeroed by reset() on completion,
    so the only honest observation window is while is_processing. We track
    in-flight tasks per slot across polls: first snapshot gives the cache
    count, the last (prompt phase is over by then) the final processed.
    Task completion → record (ts, cache, cache+processed) once per
    id_task. Tasks shorter than one poll gap are missed — they are a tiny
    share of prompt tokens, so the rate stays representative.
    """
    try:
        raw = urllib.request.urlopen(f"http://127.0.0.1:{st.get('port')}/slots", timeout=1.5).read()
        slots = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return
    if not isinstance(slots, list) or not slots:
        return
    now = time.time()
    used = total = 0
    for s in slots:
        try:
            ctx = int(s.get("n_ctx") or 0)
        except (TypeError, ValueError):
            ctx = 0
        if ctx <= 0:
            continue
        total += ctx
        nt = s.get("next_token")
        nt = nt[0] if isinstance(nt, list) and nt else {}
        prompt = s.get("n_prompt_tokens") or 0
        if prompt > 0 or s.get("is_processing"):
            used += prompt + (nt.get("n_decoded") or 0)
    if total:
        pct = round(100.0 * used / total, 4)
        st["kv"] = {"ts": now, "pct": round(100.0 * used / total, 2),
                    "used": used, "total": total}
        # Inject into the just-appended sample so the LIVE kv% series works:
        # _engine_series reads vllm:kv_cache_usage_perc from each sample's
        # gauges, and llama.cpp's /metrics never emits it. The gate at the
        # call site (has_metrics) guarantees samples[-1] IS this poll's dict.
        if st["samples"]:
            st["samples"][-1][1].setdefault("gauges", {})[
                "vllm:kv_cache_usage_perc"] = {"value": pct, "labels": {}}
    # --- decode rate: per-poll token positions from /slots ---
    # The /metrics counters (tokens_predicted_total, prompt_tokens_total)
    # only move at task events (on_prediction is called ONLY on the stop
    # path — server-context.cpp L3939/L4061 — and on_prompt_eval on the first
    # decoded token), so a counter-based rate reads ~0 while a task is
    # decoding and spikes at task completion. n_decoded and
    # n_prompt_tokens_processed advance per token and are exposed per slot,
    # which is what the journal's "eval time ... tokens per second" line
    # is computed from. Track (id_task, n_decoded, n_prompt_tokens_processed)
    # per slot: same id_task -> credit positive deltas; new id_task ->
    # baseline. An idle slot keeps its completed task's n_decoded
    # (reset() does not clear it), so a finished task's final chunk is
    # credited on the first idle poll. Lost only when a NEW task's prompt
    # completes within one poll gap of the old task's end (n_decoded is
    # reset at the DONE_PROMPT transition) — bounded by one gap x rate.
    sr = st.setdefault("slot_rate", {})
    dec_delta = 0
    pr_delta = 0
    for s in slots:
        sid = s.get("id")
        tid = s.get("id_task")
        nt = s.get("next_token")
        nt = nt[0] if isinstance(nt, list) and nt else {}
        dec = nt.get("n_decoded") or 0
        prc = s.get("n_prompt_tokens_processed") or 0
        f = sr.get(sid)
        if f and f[0] == tid:
            dec_delta += max(0, dec - f[1])
            pr_delta += max(0, prc - f[2])
        sr[sid] = (tid, dec, prc)
    if st["samples"]:
        g = st["samples"][-1][1].setdefault("gauges", {})
        g["llamacpp:gen_delta"] = {"value": dec_delta, "labels": {}}
        g["llamacpp:prompt_delta"] = {"value": pr_delta, "labels": {}}
    # --- prefix reuse: track in-flight tasks, record on completion ---
    pr = st.setdefault("prefix_tasks", {})
    fl = st.setdefault("prefix_flight", {})
    for k in [k for k, (t, *_r) in pr.items() if now - t > 3600]:
        del pr[k]
    seen_slots = set()
    for s in slots:
        sid = s.get("id")
        seen_slots.add(sid)
        tid = s.get("id_task")
        if s.get("is_processing"):
            cache = s.get("n_prompt_tokens_cache") or 0
            processed = s.get("n_prompt_tokens_processed") or 0
            f = fl.get(sid)
            # n_prompt_tokens_cache is set once at prompt start (n_past) and
            # only ever grows 0 -> n_past; n_prompt_tokens_processed grows
            # monotonically during the prompt phase. Taking the MAX across
            # in-flight snapshots is race-safe: a poll that lands before the
            # STARTED transition reads cache=0, the next reads the real value.
            if f and f[0] == tid:
                fl[sid] = (tid, max(f[1], cache), max(f[2], processed))
            else:
                fl[sid] = (tid, cache, processed)
        else:
            f = fl.pop(sid, None)
            if f:
                tid_f, cache, processed = f
                if tid_f is not None and tid_f not in pr and cache + processed > 0:
                    pr[tid_f] = (now, cache, cache + processed)
    for sid in [sid for sid in fl if sid not in seen_slots]:
        fl.pop(sid, None)


def _is_llamacpp_sample(sample):
    return "llamacpp:prompt_tokens_total" in (sample or {}).get("counters", {})


# ── per-task TTFT (llama.cpp journal) ────────────────────────────────────
# /slots and /metrics expose no per-slot prompt-processing TIME — only
# token counts — so the only honest per-task TTFT source is the journal's
# "prompt eval time" print_timing line: TOTAL prompt-processing wall time
# for the task (the user-visible wait before the first token), emitted once
# per task at completion. Same line family the spec card already scrapes;
# same 10s-TTL minute-bucket cache pattern as _spec_rows.
_PROMPT_TTFT_RE = re.compile(
    r"prompt eval time =\s+([\d.]+) ms\s*/\s*(\d+) tokens")
_ttft_cache = {}            # (port, minute-bucket) -> (fetch_ts, [(ts, sec, tokens)])


def _pctl(vals, q):
    """Nearest-rank percentile (q in 0..100) of a non-empty list."""
    if not vals:
        return None
    vs = sorted(vals)
    if len(vs) == 1:
        return vs[0]
    k = min(len(vs) - 1, max(0, round(q / 100.0 * (len(vs) - 1))))
    return vs[k]


def _fetch_ttft_rows(port, since_ts):
    """llama.cpp 'prompt eval time' journal lines since since_ts (per-task)."""
    proc = _engine_proc(port)
    if not proc:
        return []
    unit = _proc_unit(proc["pid"])
    since = datetime.fromtimestamp(since_ts).strftime("%Y-%m-%d %H:%M:%S")
    cmd = "journalctl " + (f"-u {unit} " if unit else f"_PID={proc['pid']} ") \
          + f'--since "{since}" --no-pager -o short-iso-precise -g "prompt eval time ="'
    out = run(cmd, timeout=10)
    rows = []
    for line in out.splitlines():
        m = _PROMPT_TTFT_RE.search(line)
        if not m:
            continue
        try:
            ts = datetime.fromisoformat(line.split(" ", 1)[0]).timestamp()
        except (ValueError, IndexError):
            continue
        rows.append((ts, float(m.group(1)) / 1000.0, int(m.group(2))))
    return rows


def _ttft_rows(port, since_ts):
    """Cached _fetch_ttft_rows (10s TTL, 1-min window-start bucket)."""
    key = (port, int(since_ts // 60))
    hit = _ttft_cache.get(key)
    if hit and time.time() - hit[0] < _SPEC_CACHE_TTL:
        return [r for r in hit[1] if r[0] >= since_ts]
    rows = _fetch_ttft_rows(port, since_ts)
    _ttft_cache[key] = (time.time(), rows)
    if len(_ttft_cache) > 64:
        _ttft_cache.clear()
    return rows


def _ttft_step(rows, out_ts):
    """Per-task TTFT as a step series aligned to out_ts: each ts carries the
    latest completed task's prompt-processing time at or before it (null
    before the first task in the window)."""
    if not rows:
        return [None] * len(out_ts)
    rts = [r[0] for r in rows]
    out = [None] * len(out_ts)
    last = None
    for j, t in enumerate(out_ts):
        k = bisect.bisect_right(rts, t)
        if k > 0:
            last = rows[k - 1][1]
        out[j] = last
    return out


def _ttft_step_pct(rows, out_ts):
    """Rolling [p50,p95,p99] of every task completed up to each out_ts
    (all-None before the first task in the window)."""
    if not rows:
        return [[None] * 3 for _ in out_ts]
    rts = [r[0] for r in rows]
    out = []
    for t in out_ts:
        k = bisect.bisect_right(rts, t)
        if k:
            vals = [rows[m][1] for m in range(k)]
            out.append([round(_pctl(vals, q), 4) for q in (50, 95, 99)])
        else:
            out.append([None, None, None])
    return out


def _window_stats(st, window_s):
    """Stats over the last window_s seconds for one engine.
    None-safe: any missing input yields None fields, never an exception."""
    now = time.time()
    pts = [(t, p) for (t, p) in st["samples"] if now - t <= window_s and now - t >= 0.5]
    res = {
        "tokens_per_s": None, "output_per_s": None, "input_per_s": None,
        "requests_per_s": None,
        "ttft_p50": None, "ttft_p95": None, "ttft_p99": None,
        "queue_p50": None, "queue_p95": None, "queue_p99": None,
        "tpot_p50": None, "tpot_p95": None, "tpot_p99": None,
        "e2e_p50": None, "e2e_p95": None, "e2e_p99": None,
        "kv_pct": None, "running": None, "waiting": None,
        "preemptions_per_min": None, "prefix_hit_rate": None, "spec_acceptance": None,
        # --- richer breakdowns (ported from the Grafana row) ---
        "spec_pos": [],                 # [{"pos":0,"pct":..}, ...] acceptance by position
        "finish_reasons": [],           # [{"reason":"stop","count":n}, ...]
        "finish_per_min": None,         # sum(finish)/window in req/min
        "prompt_src": [],               # [{"src":"local_compute","count":n}, ...]
        "prompt_cached_pct": None,      # % of prompt tokens served from cache
        "total_tokens": None,           # in-window token total (window-agnostic est.)
        "in_tokens": None,              # in-window input (prompt) token total
        "out_tokens": None,             # in-window output (generated) token total
        "http_2xx_per_min": None,       # api success rate
        "http_4xx_per_min": None,       # api error rate
    }
    if not pts:
        return res
    cur = pts[-1][1]
    for key, n in (("kv_pct", "vllm:kv_cache_usage_perc"),
                   ("running", "vllm:num_requests_running"),
                   ("waiting", "vllm:num_requests_waiting")):
        v = _g(cur, n)
        res[key] = round(v, 4) if v is not None else None
    if len(pts) < 2:
        return res
    first, cur = pts[0][1], pts[-1][1]
    dt = pts[-1][0] - pts[0][0]
    if dt < 1:
        return res
    # --- live (short-interval) decode rate for the stat card ---
    # output_per_s is a WINDOW average (delta / window_s): a task decoding at
    # 40 tok/s for 30s inside a 900s window renders as ~1.3 tok/s. The stat
    # card wants the real-time rate, so compute it over the last few polls
    # (up to ~10s) instead. Same source split as below: llama.cpp uses the
    # per-poll /slots gen_delta, vLLM/sglang use the counter delta.
    # fill into res via keys output_per_s_now / input_per_s_now
    now_t = pts[-1][0]
    recent = [(t, p) for (t, p) in pts if now_t - t <= 10.0]
    if len(recent) >= 2:
        r_dt = recent[-1][0] - recent[0][0]
        if r_dt >= 1:
            r_dec = sum(_g(t_p, "llamacpp:gen_delta") or 0 for _t, t_p in recent)
            r_prm = sum(_g(t_p, "llamacpp:prompt_delta") or 0 for _t, t_p in recent)
            if r_dec > 0 or r_prm > 0:
                res["output_per_s_now"] = round(r_dec / r_dt, 2)
                res["input_per_s_now"] = round(r_prm / r_dt, 2)
                res["tokens_per_s_now"] = round((r_dec + r_prm) / r_dt, 2)
            else:
                d_out_now = _delta(_c(recent[0][1], "vllm:generation_tokens_total"),
                                   _c(recent[-1][1], "vllm:generation_tokens_total"))
                d_in_now = _delta(_c(recent[0][1], "vllm:prompt_tokens_total"),
                                  _c(recent[-1][1], "vllm:prompt_tokens_total"))
                if d_out_now is not None:
                    res["output_per_s_now"] = round(d_out_now / r_dt, 2)
                if d_in_now is not None:
                    res["input_per_s_now"] = round(d_in_now / r_dt, 2)
                if d_out_now is not None and d_in_now is not None:
                    res["tokens_per_s_now"] = round((d_out_now + d_in_now) / r_dt, 2)
    # llama.cpp: the /metrics token counters only move at task events
    # (on_prediction fires on the stop path), so a first/last counter DELTA
    # as a RATE reads ~0 mid-task and spikes at completion. Rates therefore
    # use the /slots per-poll deltas (the same fields the journal's "tokens
    # per second" line is computed from). For in-window COUNTS the counter
    # delta is the exact answer: it equals the API usage total of every task
    # that completed in the window (verified 1:1, 2026-08-20), while the
    # delta sum is lossy at task boundaries (a prompt shorter than one 2s
    # poll gap is missed entirely; the pre-baseline decode chunk of a new
    # task is lost). The delta-sum path is kept only for the rates.
    is_llamacpp = any(_g(t_p, "llamacpp:gen_delta") is not None for _t, t_p in pts)
    llm_dec = sum(_g(t_p, "llamacpp:gen_delta") or 0 for _t, t_p in pts)
    llm_prm = sum(_g(t_p, "llamacpp:prompt_delta") or 0 for _t, t_p in pts)
    for key, names in (("tokens_per_s", ("vllm:generation_tokens_total", "vllm:prompt_tokens_total")),
                       ("output_per_s", ("vllm:generation_tokens_total",)),
                       ("input_per_s", ("vllm:prompt_tokens_total",)),
                       ("requests_per_s", ("vllm:request_success_total",))):
        if is_llamacpp and key == "output_per_s":
            res[key] = round(llm_dec / dt, 2)
            continue
        if is_llamacpp and key == "input_per_s":
            res[key] = round(llm_prm / dt, 2)
            continue
        if is_llamacpp and key == "tokens_per_s":
            res[key] = round((llm_dec + llm_prm) / dt, 2)
            continue
        ds = []
        for n in names:
            d = _delta(_c(first, n), _c(cur, n))
            if d is not None:
                ds.append(d)
        if ds:
            res[key] = round(sum(ds) / dt, 2)
    # in-window token totals: exact first/last COUNTER delta. For llama.cpp
    # this is the API usage total of every task that COMPLETED in the window
    # (verified 1:1 vs usage + journal, 2026-08-20) — the /slots delta sum
    # used here in part 10 undercounted (out 340 vs 400, in 0 vs 30 on a
    # single-task window). vLLM names are the aliases of the same counters.
    d_in = _delta(_c(first, "vllm:prompt_tokens_total"), _c(cur, "vllm:prompt_tokens_total"))
    d_out = _delta(_c(first, "vllm:generation_tokens_total"), _c(cur, "vllm:generation_tokens_total"))
    if d_in is not None:
        res["in_tokens"] = int(d_in)
    if d_out is not None:
        res["out_tokens"] = int(d_out)
    # Exact in-window total from the same counter deltas (the rate-based
    # estimate at the bottom of this function stays as the vLLM-missing /
    # counter-reset fallback).
    if d_in is not None and d_out is not None:
        res["total_tokens"] = int(d_in) + int(d_out)
    d = _delta(_c(first, "vllm:num_preemptions_total"), _c(cur, "vllm:num_preemptions_total"))
    res["preemptions_per_min"] = round(60.0 * d / dt, 2) if d is not None else None
    h_hits = _delta(_c(first, "vllm:prefix_cache_hits_total"), _c(cur, "vllm:prefix_cache_hits_total"))
    h_qry = _delta(_c(first, "vllm:prefix_cache_queries_total"), _c(cur, "vllm:prefix_cache_queries_total"))
    if h_hits is not None and h_qry is not None and (h_hits + h_qry) > 0:
        res["prefix_hit_rate"] = round(100.0 * h_hits / (h_hits + h_qry), 1)
    # DS4: prefix-cache hit rate from the computed/cached prefill split
    # (ds4_tokens_prefilled_total{kind="cached"} vs kind="computed").
    if res.get("prefix_hit_rate") is None:
        c_f = _c_lab(first, "ds4_tokens_prefilled_total", {"kind": "cached"})
        c_c = _c_lab(cur, "ds4_tokens_prefilled_total", {"kind": "cached"})
        m_f = _c_lab(first, "ds4_tokens_prefilled_total", {"kind": "computed"})
        m_c = _c_lab(cur, "ds4_tokens_prefilled_total", {"kind": "computed"})
        hits = _delta(c_f, c_c)
        miss = _delta(m_f, m_c)
        if hits is not None and miss is not None and (hits + miss) > 0:
            res["prefix_hit_rate"] = round(100.0 * hits / (hits + miss), 1)
    a = _delta(_c(first, "vllm:spec_decode_num_accepted_tokens_total"),
               _c(cur, "vllm:spec_decode_num_accepted_tokens_total"))
    dr = _delta(_c(first, "vllm:spec_decode_num_draft_tokens_total"),
                _c(cur, "vllm:spec_decode_num_draft_tokens_total"))
    if a is not None and dr:
        res["spec_acceptance"] = round(100.0 * a / dr, 1)
    # SGLang: acceptance is a live gauge (spec_accept_rate, 0-1), not a
    # counter pair. Read straight off the original sglang: gauge.
    if res["spec_acceptance"] is None and any(_is_sglang_sample(p) for _t, p in pts):
        sa = _g(cur, "sglang:spec_accept_rate")
        if sa is not None:
            res["spec_acceptance"] = round(100.0 * sa, 1)
        # mean accepted length per step (tok/step) — the analogue of
        # llama.cpp's mean_len / vLLM's per-position decay
        ml = _g(cur, "sglang:spec_accept_length")
        if ml is not None:
            res["spec_mean_len"] = round(ml, 2)
    # DS4: acceptance is a live gauge (spec_accept_ratio, 0-1) — used only
    # when no accepted/draft counter pair computed it; mean accepted length
    # per step is tok_per_step, read whenever a ds4 sample is present.
    if any(_is_ds4_sample(p) for _t, p in pts):
        if res["spec_acceptance"] is None:
            sa = _g(cur, "ds4_spec_accept_ratio")
            if sa is not None:
                res["spec_acceptance"] = round(100.0 * sa, 1)
        if res.get("spec_mean_len") is None:
            ml = _g(cur, "ds4_tok_per_step")
            if ml is not None:
                res["spec_mean_len"] = round(ml, 2)
    a = _delta(_c(first, "vllm:spec_decode_num_accepted_tokens_total"),
               _c(cur, "vllm:spec_decode_num_accepted_tokens_total"))
    dr = _delta(_c(first, "vllm:spec_decode_num_draft_tokens_total"),
                _c(cur, "vllm:spec_decode_num_draft_tokens_total"))
    if a is not None and dr:
        res["spec_acceptance"] = round(100.0 * a / dr, 1)
    # SGLang: acceptance is a live gauge (spec_accept_rate, 0-1), not a
    # counter pair. Read straight off the original sglang: gauge.
    if res["spec_acceptance"] is None and any(_is_sglang_sample(p) for _t, p in pts):
        sa = _g(cur, "sglang:spec_accept_rate")
        if sa is not None:
            res["spec_acceptance"] = round(100.0 * sa, 1)
        # mean accepted length per step (tok/step) — the analogue of
        # llama.cpp's mean_len / vLLM's per-position decay
        ml = _g(cur, "sglang:spec_accept_length")
        if ml is not None:
            res["spec_mean_len"] = round(ml, 2)
    for key, p, names in (("ttft_p50", 50, ("vllm:time_to_first_token_seconds",)),
                          ("ttft_p95", 95, ("vllm:time_to_first_token_seconds",)),
                          ("ttft_p99", 99, ("vllm:time_to_first_token_seconds",)),
                          ("queue_p50", 50, ("vllm:request_queue_time_seconds",)),
                          ("queue_p95", 95, ("vllm:request_queue_time_seconds",)),
                          ("queue_p99", 99, ("vllm:request_queue_time_seconds",)),
                          ("tpot_p50", 50, ("vllm:inter_token_latency_seconds", "vllm:request_time_per_output_token_seconds")),
                          ("tpot_p95", 95, ("vllm:inter_token_latency_seconds", "vllm:request_time_per_output_token_seconds")),
                          ("tpot_p99", 99, ("vllm:inter_token_latency_seconds", "vllm:request_time_per_output_token_seconds")),
                          ("e2e_p50", 50, ("vllm:e2e_request_latency_seconds",)),
                          ("e2e_p95", 95, ("vllm:e2e_request_latency_seconds",)),
                          ("e2e_p99", 99, ("vllm:e2e_request_latency_seconds",))):
        for n in names:
            hd = _hist_delta(first.get("histograms", {}).get(n), cur.get("histograms", {}).get(n))
            if hd and hd.get("count"):
                val = promparse.percentile(hd["buckets"], p)
                res[key] = round(val, 4) if val is not None else None
                break
    # --- breakdowns over the window (delta-based, reset-safe) ---
    # total_tokens: exact (d_in + d_out, set above when both counters are
    # available). The rate-based estimate is only the fallback for the
    # counter-missing / reset case where the exact block left it None.
    if res["total_tokens"] is None:
        res["total_tokens"] = round((res["tokens_per_s"] or 0) * dt, 0)
    # llama.cpp exposes no latency histograms; derive window means from the
    # time/token counters (vLLM keeps its histogram percentiles — only fills
    # what the histogram path left null).
    if res["tpot_p50"] is None:
        dtok = _delta(_c(first, "vllm:generation_tokens_total"), _c(cur, "vllm:generation_tokens_total"))
        dtsec = _delta(_c(first, "vllm:generation_tokens_seconds_total"), _c(cur, "vllm:generation_tokens_seconds_total"))
        if dtok and dtsec is not None:
            m = dtsec / dtok
            res["tpot_p50"] = res["tpot_p95"] = res["tpot_p99"] = round(m, 4)
    if res["ttft_p50"] is None and st.get("port") is not None and \
            any(_is_llamacpp_sample(p) for _t, p in pts):
        # True per-task TTFT: the journal's "prompt eval time" total
        # prompt-processing time per task (the counter ratio below is
        # SECONDS PER PROMPT TOKEN, not time-to-first-token — a 2000-token
        # prompt would read 9 s there while the user waits that for the
        # first token too, and a 23-token prompt reads 4.7 ms against a
        # real 108 ms wait).
        rows = _ttft_rows(st["port"], time.time() - window_s)
        if rows:
            vals = [r[1] for r in rows]
            res["ttft_p50"] = round(_pctl(vals, 50), 4)
            res["ttft_p95"] = round(_pctl(vals, 95), 4)
            res["ttft_p99"] = round(_pctl(vals, 99), 4)
    if res["ttft_p50"] is None:
        dtok = _delta(_c(first, "vllm:prompt_tokens_total"), _c(cur, "vllm:prompt_tokens_total"))
        dtsec = _delta(_c(first, "llamacpp:prompt_seconds_total"), _c(cur, "llamacpp:prompt_seconds_total"))
        if dtok and dtsec is not None:
            m = dtsec / dtok
            res["ttft_p50"] = res["ttft_p95"] = res["ttft_p99"] = round(m, 4)
    # spec-decode acceptance by position (per_pos accepted / total drafts).
    # num_draft_tokens_total is the unlabelled total; per_pos_total is labelled
    # by position. (vLLM counter names keep their _total suffix.)
    d_total = _delta(_c(first, "vllm:spec_decode_num_draft_tokens_total"),
                     _c(cur, "vllm:spec_decode_num_draft_tokens_total"))
    pos_rows = []
    if d_total:
        for lab, _v in promparse.all_labels(cur, "counters", "vllm:spec_decode_num_accepted_tokens_per_pos_total"):
            pos = lab.get("position")
            a = _delta(_c_lab(first, "vllm:spec_decode_num_accepted_tokens_per_pos_total", lab),
                       _c_lab(cur, "vllm:spec_decode_num_accepted_tokens_per_pos_total", lab))
            if a is not None and d_total:
                pos_rows.append({"pos": int(pos) if pos is not None else None,
                                 "pct": round(100.0 * a / d_total, 1)})
    pos_rows.sort(key=lambda r: (r["pos"] is None, r["pos"]))
    res["spec_pos"] = pos_rows[:8]
    # finish-reason split (request_success_total{finished_reason})
    fr = []
    fin_total = 0
    for lab, _v in promparse.all_labels(cur, "counters", "vllm:request_success_total"):
        reason = lab.get("finished_reason")
        d = _delta(_c_lab(first, "vllm:request_success_total", lab),
                   _c_lab(cur, "vllm:request_success_total", lab))
        if d:
            fin_total += d
            fr.append({"reason": reason or "?", "count": int(d)})
    fr.sort(key=lambda r: -r["count"])
    res["finish_reasons"] = fr[:6]
    res["finish_per_min"] = round(60.0 * fin_total / dt, 2) if fin_total else 0.0
    # prompt-token source split (prompt_tokens_by_source_total)
    src_rows = []
    for lab, _v in promparse.all_labels(cur, "counters", "vllm:prompt_tokens_by_source_total"):
        s = lab.get("source")
        d = _delta(_c_lab(first, "vllm:prompt_tokens_by_source_total", lab),
                   _c_lab(cur, "vllm:prompt_tokens_by_source_total", lab))
        if d:
            src_rows.append({"src": s or "?", "count": int(d)})
    src_rows.sort(key=lambda r: -r["count"])
    res["prompt_src"] = src_rows[:6]
    cached = sum(r["count"] for r in src_rows if r["src"] in ("local_cache_hit", "external_kv_transfer"))
    total_prompt = sum(r["count"] for r in src_rows)
    if total_prompt:
        res["prompt_cached_pct"] = round(100.0 * cached / total_prompt, 1)
    # api request rate by status (http_requests_total{status})
    def _http_rate(prefix):
        s = 0.0; seen = False
        for lab, _v in promparse.all_labels(cur, "counters", "http_requests_total"):
            if (lab.get("status") or "").startswith(prefix):
                d = _delta(_c_lab(first, "http_requests_total", lab),
                           _c_lab(cur, "http_requests_total", lab))
                if d is not None:
                    s += d; seen = True
        return round(60.0 * s / dt, 2) if seen else 0.0
    res["http_2xx_per_min"] = _http_rate("2")
    res["http_4xx_per_min"] = _http_rate("4")
    # --- llama.cpp: kv occupancy (live /slots) + prefix reuse (task records) ---
    # vLLM already has real values from its /metrics; only fill what is None.
    if res["kv_pct"] is None and isinstance(st.get("kv"), dict):
        res["kv_pct"] = st["kv"]["pct"]
    if res["prefix_hit_rate"] is None:
        pt = st.get("prefix_tasks") or {}
        now = time.time()
        hits = sum(c for t, c, _q in pt.values() if now - t <= window_s)
        qu = sum(q for t, _c, q in pt.values() if now - t <= window_s)
        if qu > 0:
            res["prefix_hit_rate"] = round(100.0 * hits / qu, 1)
    # SGLang: no hit/query counter pair — prefix reuse is the live radix-cache
    # hit-rate gauge (already aliased to vllm:prefix_cache_hit_rate, 0-100).
    if res["prefix_hit_rate"] is None and any(_is_sglang_sample(p) for _t, p in pts):
        pv = _g(cur, "vllm:prefix_cache_hit_rate")
        if pv is not None:
            res["prefix_hit_rate"] = round(pv, 1)
    return res


def _engine_series(st, window_s):
    """Downsampled per-sample series for sparklines (~150 pts max).

    Token rates are computed PER TICK (consecutive 2s polls) and only then
    strided: the llamacpp:*_delta gauges (llama.cpp decode rate, see
    _update_slots) cover exactly one poll interval, so striding the points
    first and dividing a single-poll delta by the strided span would
    undercount by the stride factor. Counter-based rates (vLLM) work either
    way; per-tick-then-stride keeps both sources consistent.
    """
    now = time.time()
    pts = [(t, p) for (t, p) in st["samples"] if now - t <= window_s]
    if not pts:
        return {"ts": []}
    n = len(pts)
    stride = max(1, n // 150)
    idx = list(range(0, n, stride))
    # --- per-tick token rates (full resolution, before striding) ---
    o_rate = [None] * n
    i_rate = [None] * n
    for i in range(1, n):
        t, p = pts[i]
        tp, pp = pts[i - 1]
        d = t - tp
        o = _delta(_c(pp, "vllm:generation_tokens_total"), _c(p, "vllm:generation_tokens_total"))
        i_ = _delta(_c(pp, "vllm:prompt_tokens_total"), _c(p, "vllm:prompt_tokens_total"))
        # llama.cpp: the /metrics counters only move at task events
        # (on_prediction fires on the stop path — server-context.cpp
        # L3939/L4061), so o/i read ~0 mid-task and spike at completion.
        # The /slots per-poll deltas are the same fields the journal's
        # "eval time ... tokens per second" is computed from.
        gd = _g(p, "llamacpp:gen_delta")
        pd = _g(p, "llamacpp:prompt_delta")
        if gd is not None:
            o = gd
        if pd is not None:
            i_ = pd
        o_rate[i] = round(o / d, 2) if (o is not None and d > 0) else None
        i_rate[i] = round(i_ / d, 2) if (i_ is not None and d > 0) else None
    # --- per-tick TTFT percentiles ---
    # llama.cpp: rolling per-task percentiles from the journal "prompt eval
    # time" lines (total prompt-processing time per task — the true TTFT).
    # vLLM: per-tick histogram deltas.
    ttft_p = [[None] * 3 for _ in range(n)]
    is_llm = any(_is_llamacpp_sample(p) for _t, p in pts)
    if is_llm:
        rows = _ttft_rows(st.get("port"), now - window_s) if st.get("port") is not None else []
        if rows:
            step = _ttft_step_pct(rows, [pts[i][0] for i in idx])
            for j, i in enumerate(idx):
                ttft_p[i] = step[j]
    else:
        for i in range(1, n):
            hd = _hist_delta(pts[i - 1][1].get("histograms", {}).get("vllm:time_to_first_token_seconds"),
                             pts[i][1].get("histograms", {}).get("vllm:time_to_first_token_seconds"))
            if hd and hd.get("count"):
                for k, q in ((0, 50), (1, 95), (2, 99)):
                    v = promparse.percentile(hd["buckets"], q)
                    ttft_p[i][k] = round(v, 4) if v is not None else None
    # --- assemble strided output ---
    out = {"ts": [round(pts[i][0], 1) for i in idx]}
    for key, getter in ((
        "kv_pct", lambda p: _g(p, "vllm:kv_cache_usage_perc")),
        ("running", lambda p: _g(p, "vllm:num_requests_running")),
        ("waiting", lambda p: _g(p, "vllm:num_requests_waiting")),
        ("ttft", lambda p: _g(p, "vllm:prompt_seconds")),
        ("tpot", lambda p: _g(p, "vllm:predicted_tokens_seconds"))):
        vals = []
        for i in idx:
            v = getter(pts[i][1])
            vals.append(round(v, 4) if v is not None and key == "kv_pct" else v)
        out[key] = vals
    for key, arr in (("output_per_s", o_rate), ("input_per_s", i_rate)):
        out[key] = [arr[i] for i in idx]
    for j, key in enumerate(("ttft_p50", "ttft_p95", "ttft_p99")):
        out[key] = [ttft_p[i][j] for i in idx]
    return out


DB_PATH = os.path.join(BASE_DIR, "metrics.db")
DB_FLUSH_EVERY = 15          # polls between DB writes (15 * 2s = 30s cadence)
DB_RETENTION_DAYS_DEFAULT = 14
_db_last_prune_check = 0.0
_last_ledger_flush = None
_gpu_hw_cache = {"t": 0.0, "val": None}
_net_tx = {"t": 0.0, "b": 0.0}
# throttle-since state: which set of reasons is active and when that exact
# set started (survives across 15s probes; resets on process restart).
_throttle_state = {"sig": None, "since": None}


def net_rx_tx():
    """Host network throughput in B/s — delta of /proc/net/dev rx+tx bytes
    over the gpu_hw() 15s cache window. Single-engine box: sum the real
    interfaces (loopback/virtuals excluded)."""
    now = time.time()
    try:
        with open("/proc/net/dev") as f:
            lines = f.readlines()[2:]
        rx = tx = 0.0
        for line in lines:
            name, _, rest = line.partition(":")
            n = name.strip()
            if n == "lo" or n.startswith(("docker", "veth", "br-", "tap", "ifb", "virbr")):
                continue
            parts = rest.split()
            if len(parts) >= 9:
                rx += float(parts[0]); tx += float(parts[8])
        dt = now - _net_tx["t"]
        if dt < 5:
            return 0.0
        rate = (rx + tx - _net_tx["b"]) / dt
        _net_tx["t"], _net_tx["b"] = now, rx + tx
        return round(max(rate, 0.0), 0)
    except Exception:
        return 0.0


def _net_ifaces():
    """(name, operstate, speed_or_None) for real up-capable interfaces.
    speed is None when the kernel reports no fixed speed — wired ports down
    report -1, and Wi-Fi raises OSError(EINVAL) on the speed file, which
    means 'no negotiated speed', not 'unreadable'. Virtual/loopback excluded."""
    out = []
    try:
        names = sorted(os.listdir("/sys/class/net"))
    except Exception:
        return out
    for d in names:
        if d == "lo" or d.startswith(("docker", "veth", "br-", "tap",
                                      "ifb", "virbr")):
            continue
        try:
            state = open("/sys/class/net/%s/operstate" % d).read().strip()
        except OSError:
            continue
        try:
            spd = open("/sys/class/net/%s/speed" % d).read().strip()
        except OSError:
            spd = None  # wireless: no fixed speed (EINVAL on the speed file)
        if spd in ("", "-1"):
            spd = None
        out.append((d, state, int(spd) if spd else None))
    return out


def net_link_mbps():
    """Link speed (Mb/s) of the active real interface: the first up interface
    with a negotiated fixed speed (wired). None when only Wi-Fi / down ports
    are up — callers fall back to wifi_phy_mbps()."""
    for _name, state, spd in _net_ifaces():
        if state == "up" and spd is not None:
            return spd
    return None


def wifi_phy_mbps():
    """Current 802.11 PHY air rate (Mbit/s) of the up wireless interface —
    iw's tx bitrate, the per-frame ceiling that re-modulates with signal
    (MCS/width/streams). NOT a fixed link speed; 802.11 overhead means
    goodput tops out ~50-70% of it, so the gauge reads ~70% on a genuinely
    saturated link. Fallback for net_link_mbps() when no wired port has a
    negotiated speed. None when no up wireless iface has a station."""
    for name, state, spd in _net_ifaces():
        if state != "up" or spd is not None:
            continue  # not up, or a wired port (reports a negotiated speed)
        out = run("iw dev %s station dump" % name, timeout=3)
        m = re.search(r"tx bitrate:\s*([\d.]+)\s*MBit/s", out)
        if m:
            return float(m.group(1))
    return None


def gpu_hw():
    """GB10 hardware detail not on the overview: SM clock (cur/max), throttle
    event reasons + accumulated counters, NVMe temp. 15s shell-out cache."""
    if time.time() - _gpu_hw_cache["t"] < 15 and _gpu_hw_cache["val"]:
        return _gpu_hw_cache["val"]
    val = {"sm_clock_mhz": None, "sm_clock_max_mhz": None,
           "throttle_reasons": {}, "throttle_counters_us": {},
           "throttle_since_ts": None, "net_link_mbps": None,
           "net_link_kind": None,
           "nvme_c": None, "tlimit_c": None, "temp_c": None, "power_w": None,
           "util_pct": None, "net_bps": net_rx_tx(), "now": time.time()}
    # reason keys that aren't actually throttle events
    _NOISE_REASONS = {"Idle", "Sync Boost", "Applications Clocks Setting",
                      "Display Clock Setting", "Sw Thermostat", "Auto Boost"}
    # counter keys that aren't actually throttle durations
    # (SW Power Capping IS a throttle counter — time spent power-cap throttled)
    _NOISE_COUNTERS = ("sync boost",)
    try:
        out = run("nvidia-smi --query-gpu=clocks.sm,clocks.max.sm,"
                  "temperature.gpu,power.draw,utilization.gpu,"
                  "power.limit,temperature.gpu.tlimit "
                  "--format=csv,noheader,nounits", timeout=5)
        # fields (positional): 0 sm, 1 max.sm, 2 temp, 3 power, 4 util,
        # 5 power.limit (can be [N/A]), 6 tlimit
        a = [x.strip() for x in out.split(",")]
        def fnum(i):
            try:
                return float(a[i].strip("[]"))
            except (IndexError, ValueError):
                return None
        sm, mx, t, pw, ut, tl = fnum(0), fnum(1), fnum(2), fnum(3), fnum(4), fnum(6)
        if sm is not None:
            val["sm_clock_mhz"] = int(sm)
        if mx is not None:
            val["sm_clock_max_mhz"] = int(mx)
        if t is not None:
            val["temp_c"] = int(t)
        if pw is not None:
            val["power_w"] = round(pw, 1)
        if ut is not None:
            val["util_pct"] = int(ut)
        if tl is not None:
            val["tlimit_c"] = int(tl)
    except Exception:
        pass
    try:
        q = run("nvidia-smi -q -d PERFORMANCE | grep -A14 'Clocks Event Reasons'", timeout=5)
        section = "reasons"
        for line in q.splitlines():
            if "Counters" in line:
                section = "counters"
                continue
            s = line.strip()
            if not s or "Reasons" in s or "Counters" in s:
                continue
            key = s.split(":")[0].strip()
            if section == "reasons" and ("Active" in s or "Not Active" in s):
                if key not in _NOISE_REASONS:
                    val["throttle_reasons"][key] = "Not Active" not in s
            elif section == "counters":
                if any(n in key.lower() for n in _NOISE_COUNTERS):
                    continue
                mm = re.search(r'(\d+)\s+us', s)
                if mm:
                    val["throttle_counters_us"][key] = int(mm.group(1))
    except Exception:
        pass
    try:
        # reuse the lm-sensors-based temps() (correct scaling) rather than a
        # raw hwmon read (nvme reports millidegrees, not centidegrees)
        val["nvme_c"] = temps().get("nvme")
    except Exception:
        pass
    # GPU temp/power/util fallbacks from the (15s-cached) overview gpu_info:
    # the tlimit query above covers them when nvidia-smi accepts the fields,
    # but on some driver versions tlimit is unknown and the query fails whole.
    try:
        g = gpu_info()
        if val["temp_c"] is None:
            val["temp_c"] = g.get("temp")
        if val["power_w"] is None:
            val["power_w"] = g.get("power_w")
        if val["util_pct"] is None:
            val["util_pct"] = g.get("util")
        if val["tlimit_c"] is None:
            q = run("nvidia-smi -q -d TEMPERATURE | grep -i 'Tlimit'", timeout=5)
            mm = re.search(r"(\d+)\s*°?C", q or "")
            if mm:
                val["tlimit_c"] = int(mm.group(1))
    except Exception:
        pass
    # any active throttle reason -> 1 (for the over-time throttle chart)
    val["throttle_active"] = 1 if any(val["throttle_reasons"].values()) else 0
    # throttle-since: when the CURRENT set of active reasons started. The
    # set (not just on/off) is the key, so swapping causes resets the clock.
    # Resets on dashboard process restart — the honest limit of a live view.
    active = sorted(k for k, v in val["throttle_reasons"].items() if v)
    sig = tuple(active)
    now = time.time()
    if sig != _throttle_state["sig"]:
        _throttle_state["sig"], _throttle_state["since"] = sig, now
    val["throttle_since_ts"] = _throttle_state["since"]
    val["net_link_mbps"] = net_link_mbps()
    if val["net_link_mbps"] is None:
        # no wired port negotiated: fall back to the live 802.11 PHY air
        # rate (moves with signal; goodput tops out ~50-70% of it)
        val["net_link_mbps"] = wifi_phy_mbps()
        val["net_link_kind"] = "wifi" if val["net_link_mbps"] is not None else None
    else:
        val["net_link_kind"] = "wired"
    # host swap (for the detail row; gpu_hw is the single live hardware source)
    try:
        mi = meminfo()
        st_ = mi.get("SwapTotal", 0)
        su_ = mi.get("SwapFree", 0)
        val["swap"] = {"total_gib": round(st_ / 1048576, 1),
                       "used_gib": round((st_ - su_) / 1048576, 1)}
    except Exception:
        val["swap"] = None
    _gpu_hw_cache["t"], _gpu_hw_cache["val"] = time.time(), val
    return val


def gpu_hw_series(window_s):
    """Downsampled live GB10 hardware series for the top over-time charts.
    Served from the 2s _gpu_live ring; long windows (>=1h) are the caller's
    job (SQLite) — the client falls back to that for 1h/24h/7d."""
    now = time.time()
    pts = [(t, s) for (t, s) in _gpu_live if now - t <= window_s and now - t >= 0.5]
    if not pts:
        return {"ts": [], "sm_clock_mhz": [], "temp_c": [], "power_w": [],
                "util_pct": [], "throttle_active": []}
    stride = max(1, len(pts) // 240)
    pts = pts[::stride]
    return {
        "ts": [round(t, 1) for t, _ in pts],
        "sm_clock_mhz": [s["sm"] for _t, s in pts],
        "temp_c": [s["temp"] for _t, s in pts],
        "power_w": [s["power"] for _t, s in pts],
        "util_pct": [s["util"] for _t, s in pts],
        "throttle_active": [s["throttle"] for _t, s in pts],
    }


def _to_db_row(port, st):
    """Flatten one engine's 30s stats into a samples-table row (None-safe)."""
    s = _window_stats(st, 30)
    return {
        "ts": int(time.time()), "port": port,
        "model": st.get("model_live") or st.get("model"),
        "kv_pct": s["kv_pct"], "running": s["running"], "waiting": s["waiting"],
        "out_tps": s["output_per_s"], "in_tps": s["input_per_s"],
        "total_tps": s["tokens_per_s"], "req_per_s": s["requests_per_s"],
        "ttft_p50": s["ttft_p50"], "ttft_p95": s["ttft_p95"],
        "ttft_p99": s["ttft_p99"],
        "tpot_p50": s["tpot_p50"], "tpot_p95": s["tpot_p95"],
        "queue_p50": s["queue_p50"], "queue_p95": s["queue_p95"],
        "e2e_p95": s["e2e_p95"],
        "prefix_hit_rate": s["prefix_hit_rate"],
        "spec_acceptance": s["spec_acceptance"],
        "preempt_per_min": s["preemptions_per_min"],
        "total_tokens": s["total_tokens"],
        "finish_per_min": s["finish_per_min"],
        "http_2xx_per_min": s["http_2xx_per_min"],
        "http_4xx_per_min": s["http_4xx_per_min"],
        "prompt_cached_pct": s["prompt_cached_pct"],
        "in_tokens": s["in_tokens"],
        "out_tokens": s["out_tokens"],
    }


def _to_db_gpu(g, hw):
    reasons = hw.get("throttle_reasons", {})
    counters = hw.get("throttle_counters_us", {})
    active = 1 if any(reasons.values()) else 0

    def _cnt(*keys):
        for k in counters:
            for want in keys:
                if want in k.lower():
                    return counters[k]
        return None

    return {
        "ts": int(time.time()),
        "sm_clock_mhz": hw.get("sm_clock_mhz"),
        "sm_clock_max_mhz": hw.get("sm_clock_max_mhz"),
        "throttle_active": active,
        "throttle_sw_thermal_us": _cnt("sw thermal"),
        "throttle_hw_thermal_us": _cnt("hw thermal"),
        "throttle_hw_brake_us": _cnt("power brake", "hw power brake"),
        "nvme_c": hw.get("nvme_c"),
        "temp_c": g.get("temp"),
        "power_w": g.get("power_w"),
        "util_pct": g.get("util"),
    }


def _lifetime_token_counters(st):
    """Engine's LIFETIME token counters from its newest scraped sample.
    Summed across label sets (sglang emits is_streaming={false,true});
    None when the sample has no counters (engine down / no /metrics)."""
    if not st.get("samples"):
        return None, None
    m = st["samples"][-1][1]
    i = promparse.sum_all(m, "counters", "vllm:prompt_tokens_total")
    o = promparse.sum_all(m, "counters", "vllm:generation_tokens_total")
    return i, o


def _db_maybe_write(gpu_info_now):
    """Writer: every DB_FLUSH_EVERY polls, flush current engine stats + GPU row
    to SQLite, update the all-time ledger, and prune daily. Never raises — a
    DB problem must not kill the poll loop."""
    global _db_conn, _db_last_prune_check, _last_ledger_flush
    try:
        if _db_conn is None:
            metadb.init_db(DB_PATH)
            _db_conn = metadb.connect(DB_PATH)
        _raw, cfg, _err = catalog.read_config()
        retention = 14
        try:
            retention = int(cfg.get("metrics", {}).get("retention_days",
                                                      DB_RETENTION_DAYS_DEFAULT))
        except Exception:
            pass
        now = time.time()
        with eng_metrics["lock"]:
            for port, st in list(eng_metrics["engines"].items()):
                if st.get("has_metrics"):
                    metadb.write_sample(_db_conn, _to_db_row(port, st))
        metadb.write_gpu(_db_conn, _to_db_gpu(gpu_info_now, gpu_hw()))
        # all-time ledger: backfill once (seed from retained samples), then
        # credit lifetime counters + whole-GPU power at LEDGER_FLUSH_S cadence
        now = time.time()
        if _last_ledger_flush is None or now - _last_ledger_flush >= metadb.LEDGER_FLUSH_S:
            _last_ledger_flush = now
            metadb.ledger_backfill(_db_conn, 0)
            pw = gpu_info_now.get("power_w") if gpu_info_now else None
            with eng_metrics["lock"]:
                for port, st in list(eng_metrics["engines"].items()):
                    if not st.get("has_metrics"):
                        continue
                    it, ot = _lifetime_token_counters(st)
                    metadb.ledger_update(_db_conn, port, it, ot, pw)
                    metadb.model_ledger_update(_db_conn, port,
                                               st.get("model_key"), it, ot)
            metadb.ledger_update(_db_conn, -1, None, None, pw)  # idle energy
        _db_conn.commit()
        if time.time() - _db_last_prune_check > 86400:
            _db_last_prune_check = now
            r = metadb.connect(DB_PATH, readonly=True)
            if now - metadb.last_prune_ts(r) > 86400:
                metadb.prune(_db_conn, retention)
            r.close()
    except Exception:
        # writer must never kill the poll loop, but it must not fail
        # SILENTLY either — log the traceback so a persistent failure is
        # visible (journald doesn't capture this unit's stderr)
        try:
            import traceback as _tb
            with open(os.path.join(BASE_DIR, "logs", "dbwriter.err"), "a") as _f:
                _tb.print_exc(file=_f)
        except Exception:
            pass
        try:
            if _db_conn is not None:
                _db_conn.close()
        except Exception:
            pass
        _db_conn = None


def collect():
    global _last_cpu, _poll_count
    mi = meminfo()
    idle, total = cpu_times()
    cpu_pct = 0.0
    if _last_cpu and total > _last_cpu["total"]:
        dt = total - _last_cpu["total"]
        di = idle - _last_cpu["idle"]
        cpu_pct = max(0.0, min(100.0, 100.0 * (1 - di / dt)))
    _last_cpu = {"idle": idle, "total": total}

    load = [float(x) for x in open("/proc/loadavg").read().split()[:3]]
    btime = None
    for line in open("/proc/stat"):
        if line.startswith("btime"):
            btime = float(line.split()[1])
            break
    boot = time.time() - (btime or 0)

    mem_total = mi["MemTotal"]
    mem_avail = mi.get("MemAvailable", mi["MemFree"])
    swap_total = mi.get("SwapTotal", 0)
    swap_used = mi.get("SwapFree", 0)
    swap_used = swap_total - swap_used

    g = gpu_info()
    gpu_mem_mib = sum(a["mem_mib"] for a in g["apps"])
    if g["apps"]:
        g["top_model"] = model_for_pid(g["apps"][0]["pid"])

    # live GB10 series for the /metrics over-time charts (2s cadence; values
    # come from the 15s-cached gpu_hw(), so the line steps in ~15s increments
    # without extra nvidia-smi load)
    try:
        _hw = gpu_hw()
        _gpu_live.append((time.time(), {
            "sm": _hw.get("sm_clock_mhz"),
            "temp": _hw.get("temp_c"),
            "power": _hw.get("power_w"),
            "util": _hw.get("util_pct"),
            "throttle": _hw.get("throttle_active", 0),
        }))
    except Exception:
        pass

    m = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "host": os.uname().nodename,
        "kernel": os.uname().release,
        "uptime_h": round(boot / 3600, 1),
        "cpu_pct": round(cpu_pct, 1),
        "load": load,
        "cores": os.cpu_count(),
        "mem": {
            "total_gib": round(mem_total / 1048576, 1),
            "used_gib": round((mem_total - mem_avail) / 1048576, 1),
            "avail_gib": round(mem_avail / 1048576, 1),
        },
        "swap": {
            "total_gib": round(swap_total / 1048576, 1),
            "used_gib": round(swap_used / 1048576, 1),
        },
        "disk": disk(),
        "gpu": g,
        "gpu_mem_mib": gpu_mem_mib,
        "temps": temps(),
        "models": probe_endpoints(),
        "units": units(),
        "top_rss": top_rss(),
    }
    try:
        scrape_engines()
    except Exception:
        pass
    _poll_count += 1
    if _poll_count % DB_FLUSH_EVERY == 0:
        _db_maybe_write(g)
    h = history
    h["ts"].append(m["ts"])
    h["cpu"].append(m["cpu_pct"])
    h["gpu_temp"].append(g["temp"])
    h["power"].append(g["power_w"])
    h["gpu_util"].append(g["util"])
    h["mem_used"].append(m["mem"]["used_gib"])
    h["load1"].append(load[0])
    h["soc_max"].append(m["temps"]["soc_max"])
    with state["lock"]:
        state["metrics"] = m


def loop():
    while True:
        try:
            collect()
        except Exception as e:
            with state["lock"]:
                prev = state["metrics"]
                if prev:
                    prev["error"] = str(e)
        time.sleep(POLL_S)


def _unit_state_full(unit):
    """active / failed / inactive / unknown + enabled state."""
    st = run(f"systemctl is-active {unit}") or "unknown"
    en = run(f"systemctl is-enabled {unit} 2>/dev/null") or "unknown"
    return st, en


def _main_pid_rss(unit):
    out = run(f"systemctl show {unit} -p MainPID --value")
    if out and out.isdigit() and int(out) > 1:
        try:
            with open(f"/proc/{out}/stat") as f:
                parts = f.read().rsplit(")", 1)[1].split()
            rss_kb = int(parts[10])  # field 24 = rss (1-indexed after comm)
            return int(out), round(rss_kb / 1048576, 2)
        except (OSError, IndexError, ValueError):
            pass
    return None, 0.0


def _port_conflicts(fleet):
    """Map port -> [names] for any port claimed by 2+ fleet entries."""
    byport = {}
    for e in fleet:
        p = e.get("port")
        if p is None:
            continue
        try:
            p = int(p)
        except (TypeError, ValueError):
            continue
        byport.setdefault(p, []).append(e["name"])
    return {str(p): ns for p, ns in byport.items() if len(ns) > 1}


def _engines_preview(cfg_dict):
    """Summary of the engines list in a (normalized) candidate config, for the
    preview modal: per-engine rows + a port-conflict map."""
    engines = cfg_dict.get("engines", [])
    units = [e for e in engines if e.get("kind") == "unit"]
    docker = [e for e in engines if e.get("kind") == "docker"]
    rows = [
        {
            "kind": e.get("kind"),
            "name": e.get("name"),
            "port": e.get("port"),
            "enabled": e.get("enabled", True),
            "label": e.get("label"),
        }
        for e in engines
    ]
    # port conflicts among the candidate list
    byport = {}
    for e in engines:
        p = e.get("port")
        if p is None:
            continue
        byport.setdefault(p, []).append(e["name"])
    conflicts = {str(p): ns for p, ns in byport.items() if len(ns) > 1}
    return {
        "total": len(engines),
        "units": len(units),
        "docker": len(docker),
        "rows": rows,
        "port_conflicts": conflicts,
    }


def engines_discover():
    """Discovery pass: inference engines found on the box, each annotated
    with whether it's already configured and a ready-to-import config entry.
    Read-only. The settings page uses this to offer an import modal."""
    cand = catalog.discovery_candidates()
    _raw, cfg, _err = catalog.read_config()
    configured = {(e["kind"], e["name"]) for e in cfg.get("engines", [])}
    # port -> configured engine names (for live conflict hints)
    port_claimed = {}
    for e in cfg.get("engines", []):
        p = e.get("port")
        if p is None:
            continue
        try:
            p = int(p)
        except (TypeError, ValueError):
            continue
        port_claimed.setdefault(p, []).append(e["name"])

    def annotate(entry, active):
        key = (entry["kind"], entry["name"])
        suggested = {
            "kind": entry["kind"],
            "name": entry["name"],
            "port": entry.get("port"),
            "label": entry.get("label"),
            "enabled": True,
        }
        conflict = None
        if entry.get("port") is not None:
            others = [n for n in port_claimed.get(int(entry["port"]), [])
                      if n != entry["name"]]
            if others:
                conflict = {"port": entry["port"], "others": others}
        return {
            **entry,
            "active": active,
            "configured": key in configured,
            "suggested": suggested,
            "port_conflict": conflict,
        }

    units = [annotate(u, u.get("active")) for u in cand["units"]]
    docker = [annotate(c, c.get("active")) for c in cand["docker"]]
    return {
        "units": units,
        "docker": docker,
        "listening": cand.get("listening", {}),
        "configured_count": len(configured),
    }


def engines_fleet():
    _raw, cfg, _err = catalog.read_config()
    engines_list = [e for e in cfg.get("engines", []) if e.get("enabled", True)]
    gpu_used_gib = round(state["metrics"]["gpu_mem_mib"] / 1048576, 1) if state["metrics"] else 0
    fleet = []
    for entry in engines_list:
        kind = entry.get("kind")
        name = entry.get("name")
        if kind == "unit":
            unit = name
            path = f"/etc/systemd/system/{unit}"
            if not os.path.isfile(path):
                fleet.append({"kind": "unit", "name": unit, "active": "missing",
                              "enabled": "n/a", "error": "unit file not found",
                              "port": entry.get("port"), "engine": "unit"})
                continue
            try:
                u = engines.parse_unit(path)
            except Exception as e:
                fleet.append({"kind": "unit", "name": unit, "active": "error",
                              "error": str(e), "port": entry.get("port")})
                continue
            active, enabled = _unit_state_full(unit)
            pid, rss_gib = _main_pid_rss(unit)
            d = u["derived"]
            port = d["port"] or entry.get("port")
            fleet.append({
                "kind": "unit",
                "name": unit,
                "label": entry.get("label"),
                "path": path,
                "active": active,
                "enabled": enabled,
                "pid": pid,
                "rss_gib": rss_gib,
                "engine": d["fork"] or catalog.infer_engine(u.get("_binary")),
                "model": d["model"],
                "model_short": (d["model"] or "").rsplit("/", 1)[-1] if d["model"] else None,
                "port": port,
                "host": d["host"],
                "ctx": d["ctx"],
                "parallel": d["parallel"],
                "spec_type": d["spec_type"],
                "alias": d["alias"],
            })
        elif kind == "docker":
            c = next((x for x in catalog.docker_containers() if x["name"] == name), None)
            if c is None:
                fleet.append({"kind": "docker", "name": name, "active": "missing",
                              "enabled": "n/a", "error": "container not found",
                              "port": entry.get("port"), "engine": "vllm"})
                continue
            try:
                rec = catalog.load_recipe(name)
                if rec is None:
                    rec = catalog.recipe_from_inspect(name)
                    catalog.save_recipe(rec)
            except Exception as e:
                rec = {"error": str(e)}
            gmu = None
            if isinstance(rec, dict) and rec.get("cmd"):
                cmd = rec["cmd"]
                if "--gpu-memory-utilization" in cmd:
                    i = cmd.index("--gpu-memory-utilization")
                    if i + 1 < len(cmd):
                        try:
                            gmu = float(cmd[i + 1])
                        except ValueError:
                            gmu = None
            port = c.get("host_port") or entry.get("port")
            fleet.append({
                "kind": "docker",
                "name": name,
                "label": entry.get("label"),
                "image": c["image"],
                "active": "active" if c["running"] else "inactive",
                "enabled": "n/a",
                "status": c["status"],
                "port": port,
                "engine": "vllm",
                "model": rec.get("model") if isinstance(rec, dict) else None,
                "model_short": (rec.get("model") or "").rsplit("/", 1)[-1] if isinstance(rec, dict) and rec.get("model") else None,
                "gpu_mem_util": gmu,
                "rss_gib": None,
            })
        elif kind == "port":
            # bare endpoint: name is the label, port is the handle. No unit,
            # no container — liveness + backend come straight off /v1/models
            # (owned_by) and the live /proc scan.
            port = entry.get("port")
            up, model, owned = False, None, None
            if port:
                try:
                    req = urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/v1/models", timeout=1.5
                    )
                    data = json.load(req)
                    up = True
                    dd = data.get("data", [])
                    ids = [m.get("id") for m in dd]
                    model = ids[0] if ids else None
                    owned = dd[0].get("owned_by") if dd else None
                except Exception:
                    pass
            # backend: /v1/models owned_by is definitive when present; fall
            # back to the process binary for a process-bound port.
            backend = None
            if owned:
                backend = owned if owned in ("sglang", "vllm", "llama", "llamacpp") else None
            proc = _engine_proc(port) if port else None
            if proc:
                base = proc["base"]
                if not backend:
                    if "sglang" in " ".join(proc["args"][:2]):
                        backend = "sglang"
                    elif "llama-server" in base or "llama" in base:
                        backend = "llama"
                    elif "vllm" in base or "vllm" in " ".join(proc["args"][:2]):
                        backend = "vllm"
                # sglang/vllm use --model-path; llama uses -m/--model
                mp = _arg(proc["args"], "--model") or _arg(proc["args"], "-m") \
                    or _arg(proc["args"], "--model-path")
                if mp and not model:
                    model = mp
            fleet.append({
                "kind": "port",
                "name": entry.get("name"),
                "label": entry.get("label"),
                "active": "active" if up else "inactive",
                "enabled": "n/a",
                "port": port,
                "engine": backend or "unknown",
                "model": model,
                "model_short": (model or "").rsplit("/", 1)[-1] if model else None,
                "gpu_mem_util": None,
                "rss_gib": None,
            })
    return {"fleet": fleet, "gpu_used_gib": gpu_used_gib, "port_conflicts": _port_conflicts(fleet)}


def _config_unit_path(unit):
    """Resolve a configured unit name to its file path, strictly under
    /etc/systemd/system. Returns None if the unit is not in config or the name
    is not a safe basename. Config (enabled or disabled) is the gate."""
    if not unit.endswith(".service"):
        unit += ".service"
    _raw, cfg, _err = catalog.read_config()
    cfg_units = {e["name"] for e in cfg.get("engines", []) if e.get("kind") == "unit"}
    if unit not in cfg_units:
        return None
    if "/" in unit or unit.startswith(".") or ".." in unit:
        return None
    if not re.fullmatch(r"[A-Za-z0-9@:_\-.]+\.service", unit):
        return None
    return f"/etc/systemd/system/{unit}"


def engines_unit_detail(unit):
    path = _config_unit_path(unit)
    if not path or not os.path.isfile(path):
        return None
    u = engines.parse_unit(path)
    active, enabled = _unit_state_full(unit)
    pid, rss_gib = _main_pid_rss(unit)
    return {
        "kind": "unit",
        "name": unit,
        "path": path,
        "raw": u["raw"],
        "active": active,
        "enabled": enabled,
        "pid": pid,
        "rss_gib": rss_gib,
        "env": u["env"],
        "scalars": u["scalars"],
        "flags": u["flags"],
        "flag_order": u["flag_order"],
        "derived": u["derived"],
        "binary": u["_binary"],
    }


def engines_port_detail(name):
    """Detail payload for kind=port engines: live /v1/models + /proc scan."""
    _raw, cfg, _err = catalog.read_config()
    entry = next((e for e in cfg.get("engines", [])
                  if e.get("kind") == "port" and e.get("name") == name), None)
    if entry is None:
        return None
    port = entry.get("port")
    out = {"kind": "port", "name": name, "label": entry.get("label"),
           "port": port, "active": "inactive", "model": None,
           "owned_by": None, "proc": None}
    if port:
        try:
            req = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/v1/models", timeout=1.5)
            data = json.load(req)
            out["active"] = "active"
            dd = data.get("data", [])
            if dd:
                out["model"] = dd[0].get("id")
                out["owned_by"] = dd[0].get("owned_by")
        except Exception:
            pass
        proc = _engine_proc(port)
        if proc:
            out["proc"] = {"pid": proc.get("pid"), "base": proc.get("base"),
                           "cmdline": " ".join((proc.get("args") or [])[:24])}
    return out


def engines_docker_detail(name):
    try:
        rec = catalog.ensure_recipe(name)
    except Exception as e:
        return {"error": str(e)}
    cs = {c["name"]: c for c in catalog.docker_containers()}
    c = cs.get(name, {})
    return {
        "kind": "docker",
        "name": name,
        "recipe": rec,
        "container": c,
        "run_command": " ".join(catalog._shq(x) for x in catalog.docker_run_command(rec)),
    }


def validate_unit_raw(text):
    """A raw unit save must parse and keep its essential structure."""
    import tempfile
    if not text.strip():
        raise ValueError("empty unit text")
    if "\t" in text:
        # systemd tolerates tabs but our files never use them; keep it strict
        pass
    with tempfile.NamedTemporaryFile("w", suffix=".service", delete=False) as f:
        f.write(text)
        tmp = f.name
    try:
        u = engines.parse_unit(tmp)
        if not u["execstart_present"]:
            raise ValueError("ExecStart= missing after edit")
        if u["_binary"] is None:
            raise ValueError("ExecStart= has no command")
        return u
    finally:
        os.unlink(tmp)


def save_unit_raw(unit, new_text):
    path = _config_unit_path(unit)
    if not path or not os.path.isfile(path):
        raise ValueError("unknown unit")
    with open(path) as f:
        old = f.read()
    if old == new_text:
        return {"changed": False, "backup": None}
    validate_unit_raw(new_text)
    bak = engines_write.save_file(path, new_text)
    run("systemctl daemon-reload")
    return {"changed": True, "backup": bak}


def unit_flag_op(unit, op, flag, value=None):
    path = _config_unit_path(unit)
    if not path:
        raise ValueError("unknown unit")
    with open(path) as f:
        text = f.read()
    if op == "set":
        new_text, changed = engines_write.set_flag(text, flag, value)
    elif op == "add":
        new_text, changed = engines_write.add_flag(text, flag, value)
    elif op == "remove":
        new_text, changed = engines_write.remove_flag(text, flag)
    else:
        raise ValueError("bad op")
    if not changed:
        return {"changed": False, "backup": None}
    validate_unit_raw(new_text)
    bak = engines_write.save_file(path, new_text)
    run("systemctl daemon-reload")
    return {"changed": True, "backup": bak}


def unit_env_op(unit, op, key, value=None):
    path = _config_unit_path(unit)
    if not path:
        raise ValueError("unknown unit")
    with open(path) as f:
        text = f.read()
    if op == "set":
        new_text, changed = engines_write.set_env(text, key, value)
    elif op == "remove":
        new_text, changed = engines_write.remove_env(text, key)
    else:
        raise ValueError("bad op")
    if not changed:
        return {"changed": False, "backup": None}
    bak = engines_write.save_file(path, new_text)
    run("systemctl daemon-reload")
    return {"changed": True, "backup": bak}


def unit_action(unit, action):
    ok_actions = {"start", "stop", "restart", "enable", "disable"}
    if action not in ok_actions:
        raise ValueError("bad action")
    r = run(f"systemctl {action} {unit}", timeout=60)
    return {"ok": r.returncode == 0, "detail": (r.stderr or r.stdout).strip()[:500]}


def docker_action(name, action):
    if action == "apply":
        rec = catalog.ensure_recipe(name)
        ok, detail = catalog.docker_apply(rec)
        return {"ok": ok, "detail": detail}
    if action in {"start", "stop", "restart"}:
        r = run(f"docker {action} {name}", timeout=120)
        return {"ok": r.returncode == 0, "detail": (r.stderr or r.stdout).strip()[:500]}
    if action == "rm":
        r = run(f"docker rm -f {name}", timeout=30)
        return {"ok": r.returncode == 0, "detail": (r.stderr or r.stdout).strip()[:500]}
    raise ValueError("bad action")


ENGINES_PAGE_PATH = os.path.join(BASE_DIR, "engines.html")
METRICS_PAGE_PATH = os.path.join(BASE_DIR, "metrics.html")
SETTINGS_PAGE_PATH = os.path.join(BASE_DIR, "settings.html")
SETUP_PAGE_PATH = os.path.join(BASE_DIR, "setup.html")


def load_engines_page():
    with open(ENGINES_PAGE_PATH, "rb") as f:
        return f.read()


def load_settings_page():
    with open(SETTINGS_PAGE_PATH, "rb") as f:
        return f.read()


def load_setup_page():
    with open(SETUP_PAGE_PATH, "rb") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Onboarding wizard (/setup) — probe + apply
# ---------------------------------------------------------------------------

def _config_exists():
    """True when a readable config.json is on disk."""
    try:
        st = os.stat(catalog.CONFIG_PATH)
        return st.st_size > 0
    except OSError:
        return False


METRICS_PAGE_PATH = os.path.join(BASE_DIR, "metrics.html")


def load_metrics_page():
    with open(METRICS_PAGE_PATH, "rb") as f:
        return f.read()


def api_metrics(window_s, live_only=False):
    """Live / fast view: per-engine stats + series from in-memory ring buffers,
    the host gauges (CPU/mem/disk/load/net), and the GPU hardware card.
    No DB involved (a DB hiccup never degrades the live view).
    live_only=True drops engines without /metrics (the /metrics page renders
    those on the engines page, not here)."""
    m = state.get("metrics") or {}
    hw_for_net = gpu_hw()
    host = {
        "cpu_pct": m.get("cpu_pct"),
        "load": m.get("load"), "cores": m.get("cores"),
        "mem": m.get("mem"), "swap": m.get("swap"),
        "disk": m.get("disk"),
        "gpu_util": (m.get("gpu") or {}).get("util"),
        "gpu_mem_mib": m.get("gpu_mem_mib"),
        "net_bps": hw_for_net.get("net_bps"),
    }
    out = {"window_s": window_s, "refresh_s": POLL_S, "engines": [],
           "gpu_hw": gpu_hw(), "gpu_series": gpu_hw_series(window_s),
           "host": host}
    in_t = out_t = 0
    saw_tok = False
    with eng_metrics["lock"]:
        for port, st in sorted(eng_metrics["engines"].items()):
            if live_only and not st.get("has_metrics"):
                continue
            e = {"port": port, "label": st.get("label"), "kind": st.get("kind"),
                 "model": st.get("model_live") or st.get("model"),
                 "up": st.get("up"), "has_metrics": st.get("has_metrics"),
                 "stats": None, "series": {"ts": []}}
            # backend: detectable from the metric signature of the latest
            # sample (llama.cpp / sglang are unique), else from the process
            # binary (llama-server), else the /v1/models owned_by field for
            # a bare endpoint with no /metrics.
            # model_cmdline: the real model path from the process scan
            # (llama.cpp emits no model_name label — the only source).
            e["backend"] = "unknown"
            if st.get("has_metrics") and st["samples"]:
                last_p = st["samples"][-1][1]
                if _is_llamacpp_sample(last_p):
                    e["backend"] = "llama"
                elif _is_sglang_sample(last_p):
                    e["backend"] = "sglang"
                elif _is_ds4_sample(last_p):
                    e["backend"] = "ds4"
                else:
                    e["backend"] = "vllm"
            proc = _engine_proc(port)
            if proc:
                m = _arg(proc["args"], "--model") or _arg(proc["args"], "-m") \
                    or _arg(proc["args"], "--model-path")
                e["model_cmdline"] = os.path.basename(m) if m else None
                if "sglang" in " ".join(proc["args"][:2]) and e["backend"] in ("unknown", "vllm"):
                    e["backend"] = "sglang"
                if e["backend"] == "unknown" and "llama-server" in proc["base"]:
                    e["backend"] = "llama"
            if e["backend"] == "unknown" and e.get("model") is not None:
                # a configured engine that answered /v1/models: the id/owned_by
                # tells us the serving engine (owned_by=="sglang" is definitive)
                if "sglang" in str(e.get("model", "")):
                    e["backend"] = "sglang"
            if st.get("has_metrics"):
                e["stats"] = _window_stats(st, window_s)
                e["series"] = _engine_series(st, window_s)
                e["spec"] = _engine_spec(port, window_s, e["stats"])
                if e["stats"].get("in_tokens") is not None:
                    in_t += e["stats"]["in_tokens"]; saw_tok = True
                if e["stats"].get("out_tokens") is not None:
                    out_t += e["stats"]["out_tokens"]; saw_tok = True
            out["engines"].append(e)
    out["cost"] = _cost_block(in_t if saw_tok else 0, out_t if saw_tok else 0,
                              _energy_kwh_from_ring(window_s))
    # "today" strip replaces the windowed cost strip's reset-per-window
    # behavior; all-time meter comes from the ledger
    ct = _cost_today()
    if ct is not None:
        out["cost_today"] = ct
        out["alltime"] = _alltime_block()
    return out


def _default_metrics_port():
    with eng_metrics["lock"]:
        for port, st in sorted(eng_metrics["engines"].items()):
            if st.get("has_metrics"):
                return port
    return None


# ── adaptive spec decode ─────────────────────────────────────────────────
# llama.cpp emits ONE tech-agnostic journal line per completed task (slot
# print_timing, INFO level — draft-mtp / draft-dspark / draft-dflash /
# draft-eagle3 / ngram-* all log the identical format), so a single parser
# covers every spec tech with no fork patch. vLLM keeps its /metrics
# histogram percentiles; only the tech label comes from the process args.
_SPEC_TECH_LABELS = {
    "draft-mtp": "MTP",
    "draft-dspark": "DSpark",
    "draft-dflash": "DFlash",
    "draft-eagle3": "EAGLE-3",
    "draft-simple": "draft",
    "ngram-simple": "n-gram",
    "ngram-mod": "n-gram",
    "ngram-cache": "n-gram",
    "ngram-map-k": "n-gram",
    "ngram-map-k4v": "n-gram",
}
_SPEC_LINE_RE = re.compile(
    r"draft acceptance = (\d+\.\d+) \(\s*(\d+) accepted /\s*(\d+) generated\), "
    r"mean len =\s*([\d.]+)")
_spec_cache = {}            # (port, minute-bucket) -> (fetch_ts, rows)
_PROC_CACHE = {}            # port -> (fetch_ts, {pid,args,base})
_SPEC_CACHE_TTL = 10.0
_PROC_CACHE_TTL = 10.0


def _arg(args, flag):
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def _engine_proc(port):
    """World-readable /proc/*/cmdline scan → the process bound to `port`
    (any binary; the --port flag is the key). cmdline is world-readable even
    for other-user processes, so no root is needed. Returns
    {pid, args, base} or None. Cached 10s (the scan reads every /proc/*/cmdline)."""
    hit = _PROC_CACHE.get(port)
    if hit and time.time() - hit[0] < _PROC_CACHE_TTL:
        return hit[1]
    found = None
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/cmdline", "rb") as f:
                args = f.read().replace(b"\0", b" ").decode("utf-8", "replace").split()
        except Exception:
            continue
        if "--port" in args and _arg(args, "--port") == str(port):
            found = {"pid": int(d), "args": args,
                     "base": os.path.basename(args[0] or "")}
            break
    _PROC_CACHE[port] = (time.time(), found)
    if len(_PROC_CACHE) > 32:
        _PROC_CACHE.clear()
    return found


def _proc_unit(pid):
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            m = re.search(r"([a-z0-9-]+\.service)", f.read())
            return m.group(1) if m else None
    except Exception:
        return None


def _fetch_spec_rows(port, since_ts):
    """llama.cpp 'draft acceptance' journal lines since since_ts (per-task)."""
    proc = _engine_proc(port)
    if not proc:
        return []
    unit = _proc_unit(proc["pid"])
    since = datetime.fromtimestamp(since_ts).strftime("%Y-%m-%d %H:%M:%S")
    cmd = "journalctl " + (f"-u {unit} " if unit else f"_PID={proc['pid']} ") \
          + f'--since "{since}" --no-pager -o short-iso-precise -g "draft acceptance ="'
    out = run(cmd, timeout=10)
    rows = []
    for line in out.splitlines():
        m = _SPEC_LINE_RE.search(line)
        if not m:
            continue
        try:
            ts = datetime.fromisoformat(line.split(" ", 1)[0]).timestamp()
        except (ValueError, IndexError):
            continue
        rows.append({"ts": ts, "accepted": int(m.group(2)),
                     "generated": int(m.group(3)), "mean_len": float(m.group(4))})
    return rows


def _spec_rows(port, since_ts):
    """Cached _fetch_spec_rows (10s TTL, 1-min window-start bucket)."""
    key = (port, int(since_ts // 60))
    hit = _spec_cache.get(key)
    if hit and time.time() - hit[0] < _SPEC_CACHE_TTL:
        return [r for r in hit[1] if r["ts"] >= since_ts]
    rows = _fetch_spec_rows(port, since_ts)
    _spec_cache[key] = (time.time(), rows)
    if len(_spec_cache) > 64:
        _spec_cache.clear()
    return rows


def _engine_spec(port, window_s, stats=None):
    """Adaptive spec-decode summary for one engine port.
    llama.cpp: journal acceptance lines (covers every spec tech).
    vLLM: /metrics histogram delta (from `stats`), tech label from
    --speculative-config. Returns a dict (acceptance may be None when spec
    is enabled but idle) or None when the engine has no speculative decoding."""
    proc = _engine_proc(port)
    if not proc:
        return None
    args = proc["args"]
    if "llama-server" in proc["base"]:
        spec_type = _arg(args, "--spec-type") or "none"
        if spec_type == "none":
            return None
        rows = _spec_rows(port, time.time() - window_s)
        acc = sum(r["accepted"] for r in rows)
        gen = sum(r["generated"] for r in rows)
        return {
            "acceptance": round(100.0 * acc / gen, 1) if gen else None,
            "tech": _SPEC_TECH_LABELS.get(spec_type, spec_type),
            "spec_type": spec_type,
            "alias": _arg(args, "--alias"),
            "n_max": _arg(args, "--spec-draft-n-max"),
            "n_tasks": len(rows),
            "accepted": acc, "generated": gen,
            "mean_len": round(sum(r["mean_len"] for r in rows) / len(rows), 2)
                        if rows else None,
            "unit": _proc_unit(proc["pid"]), "source": "journal",
        }
    # SGLang: live gauges (spec_accept_rate / spec_accept_length), tech label
    # from --speculative-algorithm. Distinct from the vLLM histogram path.
    # NOTE: check the FULL args — the engine may run as `python3 -m
    # sglang.launch_server`, where args[:2] is "python3 -m" and the naive
    # slice missed it (tech then fell through to "vllm").
    if "sglang" in " ".join(args):
        acc = (stats or {}).get("spec_acceptance")
        if acc is None:
            return None
        tech = _arg(args, "--speculative-algorithm") or "NEXTN"
        return {
            "acceptance": acc,
            "tech": tech,
            "spec_type": None,
            "alias": None,
            "n_max": _arg(args, "--speculative-num-draft-tokens"),
            "n_tasks": None, "accepted": None, "generated": None,
            "mean_len": (stats or {}).get("spec_mean_len"),
            "unit": _proc_unit(proc["pid"]),
            "source": "metrics",
        }
    acc = (stats or {}).get("spec_acceptance")
    if acc is None:
        return None
    m = re.search(r'"method"\s*:\s*"([A-Za-z0-9_-]+)"', " ".join(args))
    return {"acceptance": acc, "tech": m.group(1) if m else "vllm",
            "spec_type": None, "alias": None, "n_max": None,
            "n_tasks": None, "accepted": None, "generated": None,
            "mean_len": None, "unit": _proc_unit(proc["pid"]),
            "source": "metrics"}


HIST_SPANS = (3600, 86400, 604800)


def _cost_block(in_tokens, out_tokens, energy_kwh):
    """Cloud SKU $ vs local DGX energy $ for the same token work.
    SKU price = (in_tok/1e6 * in_price + out_tok/1e6 * out_price);
    energy $ = kWh * usd_per_kwh. Returns {} when cost is unconfigured.
    in/out tokens and energy_kwh are window totals (caller supplies them)."""
    _raw, cfg, _err = catalog.read_config()
    cost = (cfg or {}).get("cost") or {}
    if not cost.get("enabled", True):
        return {}
    out = {
        "in_tokens": in_tokens, "out_tokens": out_tokens,
        "energy_kwh": round(energy_kwh, 6) if energy_kwh is not None else None,
        "usd_per_kwh": cost.get("usd_per_kwh"),
        "currency": (str(cost.get("currency", "USD")).upper()
                     if str(cost.get("currency", "USD")).upper() in ("USD", "EUR")
                     else "USD"),
        "energy_usd": None, "skus": [],
    }
    if energy_kwh is not None and cost.get("usd_per_kwh") is not None:
        out["energy_usd"] = round(energy_kwh * cost["usd_per_kwh"], 4)
    it = (in_tokens or 0) / 1e6
    ot = (out_tokens or 0) / 1e6
    for s in cost.get("skus", []):
        out["skus"].append({
            "name": s["name"],
            "in_price": s["in_price"],
            "out_price": s["out_price"],
            "usd": round(it * s["in_price"] + ot * s["out_price"], 4),
        })
    return out


def _midnight_ts():
    """Local midnight as epoch seconds."""
    lt = time.localtime()
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0,
                            lt.tm_wday, lt.tm_yday, -1)))


def _cost_today():
    """Since-local-midnight cost block from the DB (token sums + power
    integral). Falls back to None on any DB hiccup — the live view must not
    depend on the DB, so callers degrade to hiding the 'today' strip."""
    try:
        c = metadb.connect(DB_PATH, readonly=True)
        try:
            since = _midnight_ts()
            fleet = metadb.query_token_sums(c, None, since)
            energy = metadb.query_energy_kwh(c, since)
        finally:
            c.close()
        return _cost_block(fleet["in_tokens"], fleet["out_tokens"], energy)
    except Exception:
        return None


def _alltime_block():
    """All-time meter: ledger totals priced at CURRENT config rates + the
    cumulative series for the lifetime chart."""
    c = metadb.connect(DB_PATH, readonly=True)
    try:
        latest = metadb.ledger_latest(c)
        try:
            models = metadb.model_ledger_all(c)
        except Exception:
            models = []
    finally:
        c.close()
    blk = {"since_ts": latest["since_ts"],
           "in_tokens": latest["in_tokens"],
           "out_tokens": latest["out_tokens"],
           "energy_kwh": latest["energy_kwh"],
           "energy_usd": None, "usd_per_kwh": None, "skus": [],
           "currency": "USD"}
    cost = (catalog.read_config()[1] or {}).get("cost") or {}
    cur = str(cost.get("currency", "USD")).upper()
    blk["currency"] = cur if cur in ("USD", "EUR") else "USD"
    if cost.get("enabled", True):
        blk["usd_per_kwh"] = cost.get("usd_per_kwh")
        if latest["energy_kwh"] is not None and cost.get("usd_per_kwh") is not None:
            blk["energy_usd"] = round(latest["energy_kwh"] * cost["usd_per_kwh"], 4)
        it = (latest["in_tokens"] or 0) / 1e6
        ot = (latest["out_tokens"] or 0) / 1e6
        for s in cost.get("skus", []):
            blk["skus"].append({
                "name": s["name"], "in_price": s["in_price"],
                "out_price": s["out_price"],
                "usd": round(it * s["in_price"] + ot * s["out_price"], 4),
            })
    blk["models"] = models
    return blk


def _energy_kwh_from_ring(window_s):
    """Trapezoid integrate the in-memory power ring (history['power'], W at
    2s cadence) over the last window_s seconds -> kWh.
    NOTE: history['ts'] holds ISO-8601 strings (display ring shared with the
    overview page), not epoch floats — parse, never subtract directly."""
    now = time.time()
    ts = list(history["ts"])
    pw = list(history["power"])
    joules = 0.0
    prev = None
    for t, p in zip(ts, pw):
        try:
            t = datetime.fromisoformat(t).timestamp()
        except (TypeError, ValueError):
            continue
        if now - t > window_s or p is None:
            continue
        if prev is not None and t > prev[0]:
            joules += 0.5 * (p + prev[1]) * (t - prev[0])
        prev = (t, p)
    return joules / 3.6e6


def api_metrics_history(port, span_s):
    """Long view (1h/24h/7d) from SQLite. port None -> the engine that has
    /metrics. points=0 on a fresh DB is correct, not a bug."""
    if port is None:
        port = _default_metrics_port()
        if port is None:
            return {"port": None, "model": None, "span_s": span_s,
                    "points": 0, "series": {"ts": []}, "gpu": {"ts": []},
                    "gpu_hw": gpu_hw(),
                    "tokens": {"in_tokens": 0, "out_tokens": 0},
                    "cost": _cost_block(0, 0, 0)}
    since = int(time.time()) - span_s
    c = metadb.connect(DB_PATH, readonly=True)
    try:
        rows = metadb.query_range(c, port, since, limit=1440)
        gpu_rows = metadb.query_gpu(c, since, limit=1440)
        tok = metadb.query_token_sums(c, port, since)          # port-scoped -> tiles
        fleet = metadb.query_token_sums(c, None, since)        # all engines -> cost
        energy = metadb.query_energy_kwh(c, since)
        model = rows[-1]["model"] if rows else None
    finally:
        c.close()
    series = {k: [r.get(k) for r in rows] for k in
              ("ts", "out_tps", "in_tps", "kv_pct", "ttft_p50", "ttft_p95",
               "ttft_p99", "tpot_p95",
               "queue_p95", "running", "waiting", "req_per_s",
               "prefix_hit_rate", "spec_acceptance", "preempt_per_min",
               "total_tokens", "finish_per_min", "http_2xx_per_min",
               "http_4xx_per_min", "prompt_cached_pct",
               "in_tokens", "out_tokens")}
    gpu = {k: [r.get(k) for r in gpu_rows] for k in
           ("ts", "sm_clock_mhz", "temp_c", "power_w", "throttle_active",
            "util_pct", "nvme_c")}
    # running/waiting live gauges aren't sampled into the DB; keep them
    # present but null so the client's mean() stays uniform
    series["running"] = [None] * len(rows)
    series["waiting"] = [None] * len(rows)
    resp = {"port": port, "model": model, "span_s": span_s,
            "points": len(rows), "series": series, "gpu": gpu,
            "gpu_hw": gpu_hw(),
            "spec": _engine_spec(port, span_s),
            "tokens": {"in_tokens": tok["in_tokens"],
                       "out_tokens": tok["out_tokens"]},
            # Cost strip is fleet-wide (matches the live path: it sums every
            # live engine and integrates whole-GPU power), so bill it on the
            # all-engine token sums, not the selected port's.
            "cost": _cost_block(fleet["in_tokens"], fleet["out_tokens"], energy)}
    ct = _cost_today()
    if ct is not None:
        resp["cost_today"] = ct
        resp["alltime"] = _alltime_block()
    return resp


def settings_config(force=False):
    raw, cfg, err = catalog.read_config()
    return {"config": cfg, "raw": raw, "error": err, "roots": catalog.root_stats(force=force)}


def settings_save(raw):
    """Validate + write config.json with a .bak backup. Returns (ok, detail)."""
    import difflib
    normalized, verr = catalog.validate_config(raw)
    if verr:
        return False, verr
    # pretty-print for a stable, diffable on-disk form
    new_text = json.dumps(normalized, indent=2) + "\n"
    old_text = ""
    if os.path.isfile(catalog.CONFIG_PATH):
        with open(catalog.CONFIG_PATH) as f:
            old_text = f.read()
    ts = time.strftime("%Y%m%d_%H%M%S")
    bak = None
    if os.path.isfile(catalog.CONFIG_PATH):
        bak = catalog.CONFIG_PATH + ".bak." + ts
        with open(catalog.CONFIG_PATH, "r") as f:
            old = f.read()
        with open(bak, "w") as f:
            f.write(old)
    with open(catalog.CONFIG_PATH, "w") as f:
        f.write(new_text)
    catalog.clear_config_cache()
    changed = (old_text != new_text)
    return True, {
        "changed": changed,
        "backup": bak,
        "diff": list(difflib.unified_diff(old_text.splitlines(), new_text.splitlines(),
                                          fromfile="current", tofile="proposed",
                                          lineterm="", n=1)) if changed else [],
    }


def port_in_use(port):
    if not port:
        return False
    try:
        with open("/proc/net/tcp") as f:
            for line in f.readlines()[1:]:
                p = line.split()
                if len(p) > 3 and p[3] == "0A":  # LISTEN
                    if int(p[1].split(":")[1], 16) == int(port):
                        return True
    except OSError:
        pass
    return False


def _probe_path(p):
    """Inspect one directory for model content. Returns dict or None."""
    if not os.path.isdir(p):
        return None
    gguf = 0
    hf = 0
    try:
        for fn in os.listdir(p):
            if fn.lower().endswith(".gguf"):
                gguf += 1
            elif fn.startswith("models--"):
                hf += 1
            elif os.path.isdir(os.path.join(p, fn)) and fn.lower() in (
                    "hub", "huggingface", "hf"):
                # e.g. ~/models/huggingface/hub
                sub = os.path.join(p, fn, "hub")
                if os.path.isdir(sub):
                    hf += sum(1 for s in os.listdir(sub)
                              if s.startswith("models--"))
    except OSError:
        return None
    if gguf == 0 and hf == 0:
        return None
    kind = "hf" if hf > gguf else "gguf"
    return {"path": p, "kind": kind,
            "gguf_count": gguf, "hf_count": hf}


def setup_scan_paths():
    """Scan common locations for model directories. Read-only, shallow."""
    found = {}
    home = os.path.expanduser("~")

    def add(p):
        if p in found:
            return
        r = _probe_path(p)
        if r:
            found[p] = r

    # fixed candidates
    for p in (
        os.path.join(home, "gguf"),
        os.path.join(home, "models"),
        os.path.join(home, "models", "hf", "hub"),
        os.path.join(home, "models", "huggingface", "hub"),
        os.path.join(home, ".cache", "huggingface", "hub"),
        "/opt/models",
        "/opt/llama_models",
        "/var/lib/ollama/models",
        "/usr/share/models",
    ):
        add(p)
    # one level under /opt/models (site-specific layouts)
    try:
        for fn in os.listdir("/opt/models"):
            add(os.path.join("/opt/models", fn))
    except OSError:
        pass
    # HF_HOME env
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        add(hf_home)
        add(os.path.join(hf_home, "hub"))
    # model dirs referenced by installed unit files
    try:
        for n in os.listdir("/etc/systemd/system"):
            if not n.endswith(".service"):
                continue
            try:
                with open(os.path.join("/etc/systemd/system", n),
                          encoding="utf-8", errors="replace") as f:
                    txt = f.read(65536)
            except OSError:
                continue
            for m in re.finditer(r"(/[\w./-]+)", txt):
                cand = m.group(1)
                base = cand.rsplit("/", 1)[0]
                if base.startswith("/opt/") or base.startswith(home + "/"):
                    add(base)
    except OSError:
        pass
    # collapse nesting: if a candidate is inside an already-listed parent,
    # mark it covered instead of listing it as a separate root
    ordered = sorted(found.values(),
                     key=lambda r: r["path"].count("/"))
    for i, r in enumerate(ordered):
        rp = r["path"].rstrip("/") + "/"
        for p in ordered[:i]:
            pp = p["path"].rstrip("/") + "/"
            if rp.startswith(pp) and not r.get("covered_by"):
                r["covered_by"] = p["path"]
    out = [r for r in ordered if not r.get("covered_by")]
    return out


def setup_probe():
    """Everything the wizard's step 1/2 need, plus current config state."""
    _raw, cfg, err = catalog.read_config()
    current_roots = {r["path"]: r for r in cfg.get("model_roots", [])}
    current_engines = {(e.get("kind"), e.get("name")): e
                       for e in cfg.get("engines", [])}
    paths = setup_scan_paths()
    # annotate with whether the root is already configured
    for r in paths:
        cur = current_roots.get(r["path"])
        r["configured"] = cur is not None
        if cur:
            r["kind"] = cur.get("kind", r["kind"])
            r["enabled"] = bool(cur.get("enabled", True))
    disc = engines_discover()
    for section in ("units", "docker"):
        for u in disc.get(section, []):
            cur = current_engines.get((u.get("kind"), u.get("name")))
            if cur:
                u["wizard_checked"] = bool(cur.get("enabled", True))
                if cur.get("port"):
                    u["port"] = cur["port"]
    return {
        "has_config": _config_exists(),
        "config_error": err,
        "paths": paths,
        "unconfigured_roots": [
            {"path": p, "kind": r.get("kind", "gguf"), "enabled": False}
            for p, r in current_roots.items()
            if p not in {x["path"] for x in paths}
        ],
        "engines": disc,
    }


def setup_apply(body):
    """Write config.json from wizard answers. Reuses settings_save()
    (validate -> backup -> atomic write -> cache clear)."""
    roots = []
    for r in body.get("model_roots", []):
        if not isinstance(r, dict):
            continue
        p = str(r.get("path", "")).strip()
        k = str(r.get("kind", "gguf")).strip().lower()
        if k not in ("gguf", "hf"):
            k = "gguf"
        if p.startswith("/"):
            roots.append({"path": p, "kind": k,
                          "enabled": bool(r.get("enabled", True))})
    engines_list = []
    seen = set()
    for e in body.get("engines", []):
        if not isinstance(e, dict):
            continue
        k = str(e.get("kind", "")).strip().lower()
        n = str(e.get("name", "")).strip()
        if not n or (k, n) in seen:
            continue
        seen.add((k, n))
        entry = {"kind": k, "name": n, "enabled": bool(e.get("enabled", True))}
        port = e.get("port")
        if port not in (None, "", 0):
            try:
                entry["port"] = int(port)
            except (TypeError, ValueError):
                pass
        label = str(e.get("label") or "").strip()
        if label:
            entry["label"] = label
        engines_list.append(entry)
    cfg = {"model_roots": roots, "engines": engines_list}
    return settings_save(json.dumps(cfg))





class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw) if raw else {}
        except Exception:
            return None

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        if path == "/api/metrics":
            try:
                w = 300
                wm = re.search(r"window=(\d+)", qs)
                if wm:
                    w = min(900, max(120, int(wm.group(1))))
                live_only = "live=1" in qs
                self._json(api_metrics(w, live_only=live_only))
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return
        if path == "/api/metrics/history":
            try:
                span = 86400
                sm = re.search(r"span=(\d+)", qs)
                if sm:
                    span = int(sm.group(1))
                if span not in HIST_SPANS:
                    # snap to the nearest supported window (old code compared
                    # int vs tuple and 500'd on any non-canonical span)
                    span = min(HIST_SPANS, key=lambda s: abs(s - span))
                port = None
                pm = re.search(r"port=(\d+)", qs)
                if pm:
                    port = int(pm.group(1))
                self._json(api_metrics_history(port, span))
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return
        if path == "/api/engines":
            try:
                self._json(engines_fleet())
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return
        if path == "/api/engines/discover":
            try:
                self._json(engines_discover())
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return
        m = re.match(r"^/api/engines/unit/([^/]+)$", path)
        if m:
            detail = engines_unit_detail(m.group(1) + ".service")
            if detail is None:
                self._json({"error": "unknown unit"}, 404)
            else:
                self._json(detail)
            return
        m = re.match(r"^/api/engines/docker/([^/]+)$", path)
        if m:
            self._json(engines_docker_detail(m.group(1)))
            return
        m = re.match(r"^/api/engines/port/([^/]+)$", path)
        if m:
            import urllib.parse
            self._json(engines_port_detail(urllib.parse.unquote(m.group(1))) or {"error": "unknown engine"})
            return
        if path == "/api/engines/models":
            self._json({"models": catalog.model_catalog()})
            return
        if path == "/api/engines/containers":
            self._json({"containers": catalog.docker_containers()})
            return
        if path == "/api/config":
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            force = "force=1" in qs
            try:
                d = settings_config(force)
                self._json(d)
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return
        if path.startswith("/api/engines/logs/"):
            name = path[len("/api/engines/logs/"):]
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            lines = 150
            lm = re.search(r"lines=(\d+)", qs)
            if lm:
                lines = min(int(lm.group(1)), 2000)
            if _config_unit_path(name):
                self._json({"logs": catalog.engine_logs(name + ("" if name.endswith(".service") else ".service"), tail=lines)})
            else:
                self._json({"logs": catalog.docker_logs(name, tail=lines)})
            return
        if path.startswith("/api/"):
            with state["lock"]:
                mtr = state["metrics"]
            if mtr is None:
                self._json({"error": "collecting"}, 503)
                return
            self._json(
                {
                    **mtr,
                    "history": {k: list(v) for k, v in history.items()},
                }
            )
        else:
            if path == "/favicon.ico":
                try:
                    with open(os.path.join(BASE_DIR, "favicon.ico"), "rb") as f:
                        fb = f.read()
                except OSError:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/x-icon")
                self.send_header("Cache-Control", "max-age=86400")
                self.send_header("Content-Length", str(len(fb)))
                self.end_headers()
                self.wfile.write(fb)
                return
            if path in ("/setup", "/setup.html") or not _config_exists():
                # first run (no config.json) forces the wizard for any page
                body = load_setup_page()
            elif path in ("/engines", "/engines.html"):
                body = load_engines_page()
            elif path in ("/settings", "/settings.html"):
                body = load_settings_page()
            else:
                # "/" (and /metrics, /metrics.html) serve the metrics page —
                # it's the source of truth and the landing page now.
                body = load_metrics_page()
            ctype = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        body = self._read_body()
        if body is None:
            self._json({"error": "bad json body"}, 400)
            return

        def ok(result):
            self._json(result)

        def err(e, code=400):
            self._json({"error": str(e)}, code)

        # ---- previews (no writes) ----
        def _diff(old, new):
            return list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                             fromfile="current", tofile="proposed",
                                             lineterm="", n=1))

        # ---- config (settings page) ----
        if path == "/api/config/preview":
            raw = body.get("raw")
            normalized, verr = catalog.validate_config(raw)
            if verr:
                err(verr)
                return
            try:
                preview = catalog.scan_preview(normalized)
                engines_summary = _engines_preview(normalized)
                old_raw, _cfg, _e = catalog.read_config()
                new_text = json.dumps(normalized, indent=2) + "\n"
                diff = list(difflib.unified_diff((old_raw or "").splitlines(), new_text.splitlines(),
                                                 fromfile="current", tofile="proposed",
                                                 lineterm="", n=1))
                ok({"preview": preview, "engines": engines_summary, "diff": diff})
            except Exception as e:
                err(e, 500)
            return

        # ---- onboarding wizard ----
        if path == "/api/setup/probe":
            try:
                ok(setup_probe())
            except Exception as e:
                err(e, 500)
            return
        if path == "/api/setup/apply":
            try:
                okres, detail = setup_apply(body)
            except Exception as e:
                err(e, 500)
                return
            if not okres:
                err(detail)
                return
            ok({"ok": True, **detail})
            return

        if path == "/api/config/save":
            raw = body.get("raw")
            if raw is None:
                err("raw config required")
                return
            try:
                okres, detail = settings_save(raw)
            except Exception as e:
                err(e, 500)
                return
            if not okres:
                err(detail)
                return
            ok({"ok": True, **detail})
            return

        # ---- data management (settings → DATA MANAGEMENT) ----
        # Every action snapshots metrics.db first; token resets rebase
        # watermarks to the live counter readings so the next flush adopts
        # the baseline instead of crediting lifetime counters as fresh.
        if path == "/api/data/reset":
            action = body.get("action")
            confirm = str(body.get("confirm") or "")
            if action not in ("windows", "tokens", "models", "model", "energy", "all"):
                err("unknown reset action")
                return
            if action in ("tokens", "models", "energy", "all") and confirm != "RESET":
                err("type RESET to confirm")
                return
            try:
                bak = metadb.db_backup(DB_PATH)
                # snapshot live counter state for watermark rebasing
                with eng_metrics["lock"]:
                    live_counters, live_models = {}, {}
                    for port, st in list(eng_metrics["engines"].items()):
                        if not st.get("has_metrics"):
                            continue
                        it, ot = _lifetime_token_counters(st)
                        if it is not None:
                            live_counters[port] = (it, ot)
                            mk = st.get("model_key")
                            if mk and mk != f"port {port}":
                                live_models[port] = (mk, it, ot)
                # NEVER the poller's _db_conn: ThreadingHTTPServer runs this
                # handler on a request thread, and sqlite3 raises
                # "created in a thread can only be used in that same thread"
                # on cross-thread use. Own short-lived handle instead — WAL +
                # busy_timeout serialize the write against the writer thread.
                metadb.init_db(DB_PATH)
                rc = metadb.connect(DB_PATH)
                try:
                    if action == "windows":
                        metadb.reset_windows(rc)
                    elif action == "tokens":
                        metadb.reset_tokens(rc, live_counters)
                    elif action == "models":
                        metadb.reset_models(rc, live_models)
                    elif action == "model":
                        model = body.get("model")
                        if not model:
                            err("model name required")
                            return
                        metadb.reset_model(rc, model, live_models)
                    elif action == "energy":
                        metadb.reset_energy(rc)
                    elif action == "all":
                        metadb.reset_all(rc, live_counters, live_models)
                finally:
                    rc.close()
                ok({"ok": True, "backup": bak})
            except Exception as e:
                err(e, 500)
            return

        m = re.match(r"^/api/engines/unit/([^/]+)/flag/preview$", path)
        if m:
            unit = m.group(1) + ".service"
            p = _config_unit_path(unit)
            if not p:
                err("unknown unit", 404)
                return
            try:
                with open(p) as f:
                    text = f.read()
                if body.get("op") == "set":
                    new_text, changed = engines_write.set_flag(text, body.get("flag"), body.get("value"))
                elif body.get("op") == "add":
                    new_text, changed = engines_write.add_flag(text, body.get("flag"), body.get("value"))
                elif body.get("op") == "remove":
                    new_text, changed = engines_write.remove_flag(text, body.get("flag"))
                else:
                    raise ValueError("bad op")
                ok({"changed": changed, "diff": _diff(text, new_text) if changed else []})
            except Exception as e:
                err(e)
            return

        m = re.match(r"^/api/engines/unit/([^/]+)/env/preview$", path)
        if m:
            unit = m.group(1) + ".service"
            p = _config_unit_path(unit)
            if not p:
                err("unknown unit", 404)
                return
            try:
                with open(p) as f:
                    text = f.read()
                if body.get("op") == "set":
                    new_text, changed = engines_write.set_env(text, body.get("key"), body.get("value"))
                elif body.get("op") == "remove":
                    new_text, changed = engines_write.remove_env(text, body.get("key"))
                else:
                    raise ValueError("bad op")
                ok({"changed": changed, "diff": _diff(text, new_text) if changed else []})
            except Exception as e:
                err(e)
            return

        m = re.match(r"^/api/engines/unit/([^/]+)/raw/preview$", path)
        if m:
            unit = m.group(1) + ".service"
            p = _config_unit_path(unit)
            if not p:
                err("unknown unit", 404)
                return
            try:
                with open(p) as f:
                    text = f.read()
                new_text = body.get("content", "")
                if new_text == text:
                    ok({"changed": False, "diff": []})
                else:
                    validate_unit_raw(new_text)
                    ok({"changed": True, "diff": _diff(text, new_text)})
            except Exception as e:
                err(e)
            return

        m = re.match(r"^/api/engines/docker/([^/]+)/recipe/preview$", path)
        if m:
            name = m.group(1)
            try:
                cur = catalog.ensure_recipe(name)
                new = body.get("recipe") or {}
                old_cmd = " ".join(catalog._shq(x) for x in catalog.docker_run_command(cur))
                new_cmd = " ".join(catalog._shq(x) for x in catalog.docker_run_command(new))
                changed = old_cmd != new_cmd
                ok({"changed": changed,
                    "diff": _diff(old_cmd, new_cmd) if changed else [],
                    "current_run": old_cmd})
            except Exception as e:
                err(e)
            return

        # ---- raw unit file save ----
        m = re.match(r"^/api/engines/unit/([^/]+)/raw$", path)
        if m:
            unit = m.group(1) + ".service"
            try:
                r = save_unit_raw(unit, body.get("content", ""))
                ok({"ok": True, **r})
            except Exception as e:
                err(e)
            return

        # ---- surgical flag op ----
        m = re.match(r"^/api/engines/unit/([^/]+)/flag$", path)
        if m:
            unit = m.group(1) + ".service"
            try:
                r = unit_flag_op(unit, body.get("op"), body.get("flag"), body.get("value"))
                ok({"ok": True, **r})
            except Exception as e:
                err(e)
            return

        # ---- env op ----
        m = re.match(r"^/api/engines/unit/([^/]+)/env$", path)
        if m:
            unit = m.group(1) + ".service"
            try:
                r = unit_env_op(unit, body.get("op"), body.get("key"), body.get("value"))
                ok({"ok": True, **r})
            except Exception as e:
                err(e)
            return

        # ---- unit lifecycle ----
        m = re.match(r"^/api/engines/unit/([^/]+)/action$", path)
        if m:
            unit = m.group(1) + ".service"
            try:
                ok(unit_action(unit, body.get("action")))
            except Exception as e:
                err(e)
            return

        # ---- docker recipe save ----
        m = re.match(r"^/api/engines/docker/([^/]+)/recipe$", path)
        if m:
            name = m.group(1)
            try:
                p = catalog.save_recipe(body.get("recipe") or {})
                ok({"ok": True, "path": p})
            except Exception as e:
                err(e)
            return

        # ---- docker lifecycle / apply ----
        m = re.match(r"^/api/engines/docker/([^/]+)/action$", path)
        if m:
            name = m.group(1)
            try:
                ok(docker_action(name, body.get("action")))
            except Exception as e:
                err(e)
            return

        err("no route", 404)



if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), H)
    print(f"dashboard on http://{HOST}:{PORT}")
    srv.serve_forever()
