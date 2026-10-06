#!/usr/bin/env python3
"""tabby-sidecar — Prometheus metrics in front of any OpenAI-compatible engine.

TabbyAPI (and any engine without /metrics) gets a realtime metrics surface
without a code patch: the sidecar binds the public port, forwards everything
to the engine on 127.0.0.1, and serves /metrics in Prometheus text format.

Counting is done client-side on the actual wire: SSE chunks are counted as
they stream through (true generation rate), and the OpenAI `usage` block in
each response tail carries exact token accounting including cache hits.

Metrics exposed:
  tabby_up                         engine reachable (0/1)
  tabby_active_requests            inflight client connections
  tabby_requests_total{status=}    completed requests
  tabby_request_duration_seconds   histogram-style summary (sum/max)
  tabby_tokens_total{type=}        prompt/completion from usage
  tabby_cache_tokens_total         cached prompt tokens
  tabby_tps_gauge                  current output tokens/s (rolling 5 s)
  tabby_spec_accept_rate           accepted speculative tokens / draft tokens
  tabby_spec_draft_tokens_total    tokens proposed
  tabby_spec_accepted_tokens_total tokens accepted

Stdlib only. Configured by environment:
  SIDECAR_PORT (default 8899)  SIDECAR_BIND (default 0.0.0.0)
  ENGINE_HOST (default 127.0.0.1)  ENGINE_PORT (default 8898)
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SIDECAR_PORT = int(os.environ.get("SIDECAR_PORT", "8899"))
SIDECAR_BIND = os.environ.get("SIDECAR_BIND", "0.0.0.0")
ENGINE_HOST = os.environ.get("ENGINE_HOST", "127.0.0.1")
ENGINE_PORT = int(os.environ.get("ENGINE_PORT", "8898"))

_LOCK = threading.Lock()
_STATE = {
    "requests": {},            # status -> count
    "tokens": {"prompt": 0, "completion": 0},
    "cache_tokens": 0,
    "spec_draft": 0,
    "spec_accepted": 0,
    "active": 0,
    "dur_sum": 0.0,
    "dur_max": 0.0,
    "last_up": 0.0,
    "slot_cap": 0,
    "last_tps": 0.0,
}
# (monotonic_ts, completion_token_count) pairs — rolling TPS window.
_TPS_WIN = deque()
_TPS_SECONDS = 5.0


def _tps():
    now = time.monotonic()
    while _TPS_WIN and now - _TPS_WIN[0][0] > _TPS_SECONDS:
        _TPS_WIN.popleft()
    return sum(n for _, n in _TPS_WIN) / _TPS_SECONDS


def _account_chunk(raw, live=False, live_credited=0):
    """Credit token usage from one OpenAI `usage` block (or, for stream
    tails without usage, the engine's `timings` block). live=True marks a
    per-chunk call: the token credit counts 1 (the chunk itself) — exact
    totals come from the usage/timings tail.
    """
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return
    usage = obj.get("usage") or {}
    ct = usage.get("completion_tokens")
    pt = usage.get("prompt_tokens")
    cached = usage.get("prompt_tokens_details", {}).get("cached_tokens") \
        if isinstance(usage.get("prompt_tokens_details"), dict) else None
    # EXL3/TabbyAPI speculative-decode extras
    sdt = usage.get("speculative_draft_tokens")
    sat = usage.get("speculative_accepted_tokens")
    # tabby-style SSE tail: timings{predicted_n, prompt_n, cache_n,
    # draft_n, draft_n_accepted} — the same facts usage carries.
    tim = obj.get("timings") or {}
    if tim:
        ct = ct or tim.get("predicted_n")
        pt = pt or tim.get("prompt_n")
        cached = cached or tim.get("cache_n")
        sdt = sdt or tim.get("draft_n")
        sat = sat or tim.get("draft_n_accepted")
        # engine-computed decode rate (tokens/s over decode wall time) —
        # the ground truth for the live rate display. SSE chunk counting
        # undercounts by the spec-decode accept factor (verified: 2.02
        # tokens per chunk with MTP n_max 5), so chunk math is NOT the
        # rate source; this value is.
        pps = tim.get("predicted_per_second")
        if isinstance(pps, (int, float)) and pps > 0:
            _STATE["last_tps"] = float(pps)
    with _LOCK:
        if live:
            _TPS_WIN.append((time.monotonic(), 1))
            _STATE["tokens"]["completion"] += 1
        elif ct:
            # Tail correction: the exact usage total replaces the live
            # per-chunk estimate for this request (delta credit keeps the
            # counter monotonic even if chunk accounting missed some).
            _STATE["tokens"]["completion"] += max(0, ct - live_credited)
        if pt:
            _STATE["tokens"]["prompt"] += pt
        if cached:
            _STATE["cache_tokens"] += cached
        if sdt:
            _STATE["spec_draft"] += sdt
        if sat:
            _STATE["spec_accepted"] += sat


def _engine_up():
    try:
        urllib.request.urlopen(
            f"http://{ENGINE_HOST}:{ENGINE_PORT}/health", timeout=3).read()
        with _LOCK:
            _STATE["last_up"] = time.time()
        return 1
    except Exception:
        return 0


def _slot_cap():
    """total_slots from /props — cached once known (it only changes on
    restart; /props itself can stall behind generation)."""
    try:
        raw = urllib.request.urlopen(
            f"http://{ENGINE_HOST}:{ENGINE_PORT}/props", timeout=3).read()
        ts = json.loads(raw.decode("utf-8", "replace")).get("total_slots")
        if ts:
            with _LOCK:
                _STATE["slot_cap"] = int(ts)
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30  # keep-alive sockets idle longer than this are dropped

    def log_message(self, *a):  # journald silence
        pass

    def handle_one_request(self):
        # Clients close keep-alive sockets whenever (browser refresh, curl
        # -m timeouts). readline() then raises ConnectionResetError and
        # socketserver dumps a traceback to journald for a no-op event.
        # Closing the connection quietly is the correct handling.
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def _proxy(self):
        target = f"http://{ENGINE_HOST}:{ENGINE_PORT}{self.path}"
        body = None
        length = self.headers.get("Content-Length")
        if length:
            body = self.rfile.read(int(length))
        req = urllib.request.Request(target, data=body, method=self.command)
        for h, v in self.headers.items():
            if h.lower() not in ("host", "content-length", "connection"):
                req.add_header(h, v)
        start = time.monotonic()
        try:
            resp = urllib.request.urlopen(req, timeout=1800)
        except urllib.error.HTTPError as e:
            resp = e  # serve the engine's error response through
        with _LOCK:
            _STATE["active"] += 1
        try:
            self.send_response(resp.status)
            for h, v in resp.headers.items():
                if h.lower() not in ("transfer-encoding", "connection",
                                     "content-length"):
                    self.send_header(h, v)
            if resp.headers.get("Content-Type", "").startswith(
                    "text/event-stream"):
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                buf = b""
                live_ct = 0
                while True:
                    chunk = resp.read(1024)
                    if not chunk:
                        break
                    self.wfile.write(
                        b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                    self.wfile.flush()
                    buf += chunk
                    # SSE frames end on \n\n or \r\n\r\n (TabbyAPI uses
                    # CRLF). Split on whichever boundary comes first.
                    while True:
                        i1 = buf.find(b"\n\n")
                        i2 = buf.find(b"\r\n\r\n")
                        if i1 < 0 and i2 < 0:
                            break
                        if i2 >= 0 and (i1 < 0 or i2 < i1):
                            line, buf = buf[:i2], buf[i2 + 4:]
                        else:
                            line, buf = buf[:i1], buf[i1 + 2:]
                        for part in line.split(b"\n"):
                            part = part.strip()
                            if part.startswith(b"data: ") and \
                                    part != b"data: [DONE]":
                                raw = part[6:]
                                if b'"usage"' in raw or b'"timings"' in raw:
                                    # exact-totals tail: correct the live
                                    # estimate instead of crediting a chunk
                                    _account_chunk(
                                        raw.decode("utf-8", "replace"),
                                        live_credited=live_ct)
                                else:
                                    _account_chunk(
                                        raw.decode("utf-8", "replace"),
                                        live=True)
                                    live_ct += 1
                # The last frame may carry usage/timings (exact totals);
                # correct the live estimate against it (monotonic delta).
                if buf.strip():
                    for part in buf.split(b"\n"):
                        part = part.strip()
                        if part.startswith(b"data: ") and \
                                part != b"data: [DONE]":
                            _account_chunk(part[6:].decode(
                                "utf-8", "replace"), live_credited=live_ct)
                self.wfile.write(b"0\r\n\r\n")
            else:
                data = resp.read()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                ctype = resp.headers.get("Content-Type", "")
                if "application/json" in ctype and self.path.startswith(
                        "/v1/"):
                    try:
                        _account_chunk(data.decode("utf-8", "replace"))
                    except (ValueError, UnicodeDecodeError):
                        pass
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away; usage block was already counted
        finally:
            resp.close()
            dt = time.monotonic() - start
            with _LOCK:
                _STATE["active"] -= 1
                _STATE["requests"][str(resp.status)] = \
                    _STATE["requests"].get(str(resp.status), 0) + 1
                _STATE["dur_sum"] += dt
                _STATE["dur_max"] = max(_STATE["dur_max"], dt)

    def do_GET(self):
        if self.path.split("?")[0] == "/metrics":
            self._metrics()
        else:
            self._proxy()

    do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _proxy

    def _metrics(self):
        with _LOCK:
            up = 1 if (time.time() - _STATE["last_up"]) < 30 else 0
            active = _STATE["active"]
            reqs = dict(_STATE["requests"])
            tok = dict(_STATE["tokens"])
            cache = _STATE["cache_tokens"]
            draft = _STATE["spec_draft"]
            accepted = _STATE["spec_accepted"]
            dur_sum, dur_max = _STATE["dur_sum"], _STATE["dur_max"]
            slot_cap = _STATE["slot_cap"]
        accept_rate = (accepted / draft) if draft else 0.0
        out = []
        w = out.append
        w("# HELP tabby_up Engine reachable")
        w("# TYPE tabby_up gauge")
        w(f"tabby_up {up}")
        w("# HELP tabby_active_requests Inflight client connections")
        w("# TYPE tabby_active_requests gauge")
        w(f"tabby_active_requests {active}")
        w("# HELP tabby_total_slots Engine slot capacity (max_batch_size)")
        w("# TYPE tabby_total_slots gauge")
        w(f"tabby_total_slots {slot_cap}")
        w("# HELP tabby_requests_total Completed requests by status")
        w("# TYPE tabby_requests_total counter")
        for status, n in sorted(reqs.items()):
            w(f'tabby_requests_total{{status="{status}"}} {n}')
        w("# HELP tabby_tokens_total Tokens by type (from usage blocks)")
        w("# TYPE tabby_tokens_total counter")
        w(f'tabby_tokens_total{{type="prompt"}} {tok["prompt"]}')
        w(f'tabby_tokens_total{{type="completion"}} {tok["completion"]}')
        w("# HELP tabby_cache_tokens_total Cached prompt tokens")
        w("# TYPE tabby_cache_tokens_total counter")
        w(f"tabby_cache_tokens_total {cache}")
        last_tps = _STATE.get("last_tps") or 0.0
        w("# HELP tabby_tps_gauge Engine decode rate while generating "
          "(tokens/s, engine-reported; 0 when idle)")
        w("# TYPE tabby_tps_gauge gauge")
        w(f"tabby_tps_gauge {last_tps if active else 0:.2f}")
        w("# HELP tabby_spec_accept_rate Accepted / drafted spec tokens")
        w("# TYPE tabby_spec_accept_rate gauge")
        w(f"tabby_spec_accept_rate {accept_rate:.4f}")
        w("# HELP tabby_spec_draft_tokens_total Speculative draft tokens")
        w("# TYPE tabby_spec_draft_tokens_total counter")
        w(f"tabby_spec_draft_tokens_total {draft}")
        w("# HELP tabby_spec_accepted_tokens_total Accepted spec tokens")
        w("# TYPE tabby_spec_accepted_tokens_total counter")
        w(f"tabby_spec_accepted_tokens_total {accepted}")
        w("# HELP tabby_request_duration_seconds Request latency")
        w("# TYPE tabby_request_duration_seconds summary")
        w(f"tabby_request_duration_seconds_sum {dur_sum:.3f}")
        w(f"tabby_request_duration_seconds_max {dur_max:.3f}")
        payload = ("\n".join(out) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    print(f"tabby-sidecar: {SIDECAR_BIND}:{SIDECAR_PORT} -> "
          f"{ENGINE_HOST}:{ENGINE_PORT}", flush=True)
    threading.Thread(target=_updater, daemon=True).start()
    ThreadingHTTPServer((SIDECAR_BIND, SIDECAR_PORT), Handler).serve_forever()


def _updater():
    while True:
        _engine_up()
        with _LOCK:
            cap = _STATE["slot_cap"]
        if not cap:
            _slot_cap()
        time.sleep(5)


if __name__ == "__main__":
    main()