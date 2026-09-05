#!/usr/bin/env python3
"""GX10 engine layer (part 3): model catalog + docker recipe store.

model_catalog():
    Scans the model directories and returns a list of model entries with
    paths and sizes, grouped by source dir. Used to power the model
    dropdown in the UI.

Docker recipes:
    A vLLM engine's source of truth lives in /opt/gx10-dashboard/recipes/
    <name>.json, imported from `docker inspect` on first sight. From then
    on the recipe is authoritative; "apply" recreates the container from
    it (stop -> rm -> run).
"""

import json
import os
import re
import subprocess

import engines  # noqa: E402  (same-dir module, provides parse_unit)

RECIPE_DIR = "/opt/gx10-dashboard/recipes"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

# Fallback used only when config.json is missing or unparseable.
DEFAULT_CONFIG = {
    "model_roots": [
        {"path": "/opt/llama_models", "kind": "gguf", "enabled": True},
        {"path": "/opt/models/gguf", "kind": "gguf", "enabled": True},
        {"path": "/opt/models/hf/hub", "kind": "hf", "enabled": True},
    ],
    "engines": [
        {"kind": "unit", "name": "ds4-server.service", "port": 8000, "enabled": True},
        {"kind": "docker", "name": "qwen38-4bit", "port": 8002, "enabled": True},
        {"kind": "unit", "name": "llama-server.service", "port": 8888, "enabled": True},
        {"kind": "unit", "name": "llama-server-35b.service", "port": 8889, "enabled": True},
        {"kind": "unit", "name": "llama-step-3.7-flash.service", "port": 8890, "enabled": True},
        {"kind": "unit", "name": "llama-qw3vl.service", "port": 8989, "enabled": True},
        {"kind": "unit", "name": "llama-nsfwtune.service", "port": 8989, "enabled": True},
    ],
    # Cloud $/token SKUs vs local energy $ (cost strip on /metrics).
    # Prices are $/M-tokens. Energy $ = integrated GPU power * usd_per_kwh.
    "cost": {
        "enabled": True,
        "usd_per_kwh": 0.30,
        "skus": [
            {"name": "Ultra cloud", "in_price": 5.0, "out_price": 25.0},
            {"name": "Mid cloud", "in_price": 3.0, "out_price": 15.0},
            {"name": "Low cloud", "in_price": 0.4, "out_price": 1.6},
        ],
    },
}

_CFG_CACHE = {"mtime": None, "raw": None, "cfg": None, "err": None}
_STATS_CACHE = {"ts": 0.0, "stats": None}
_CAT_CACHE = {"key": None, "ts": 0.0, "entries": None}
CATALOG_TTL = 60.0
STATS_TTL = 30.0

SHARD_RE = re.compile(r"^(?P<stem>.+?)-(?P<idx>\d+)-of-(?P<n>\d+)$")


def run(cmd, timeout=15):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


# ---------------------------------------------------------------------------
# Config (hot-reload: re-read when mtime changes)
# ---------------------------------------------------------------------------

def _normalize_config(cfg):
    roots = cfg.get("model_roots")
    if not isinstance(roots, list):
        roots = []
    out = []
    for r in roots:
        if not isinstance(r, dict):
            continue
        p = str(r.get("path", "")).strip()
        k = str(r.get("kind", "gguf")).strip().lower()
        if k not in ("gguf", "hf"):
            k = "gguf"
        if p and p.startswith("/"):
            out.append({"path": p, "kind": k, "enabled": bool(r.get("enabled", True))})
    cost = _normalize_cost(cfg.get("cost"))
    return {"model_roots": out, "engines": _normalize_engines(cfg.get("engines")),
            "cost": cost}


def _normalize_engines(eng):
    """Normalize the engines list. Unit names must be bare .service basenames
    under /etc/systemd/system; docker names are container names."""
    if not isinstance(eng, list):
        return []
    out = []
    for e in eng:
        if not isinstance(e, dict):
            continue
        k = str(e.get("kind", "")).strip().lower()
        n = str(e.get("name", "")).strip()
        if k not in ("unit", "docker", "port"):
            continue
        if k == "unit":
            if not n.endswith(".service"):
                n += ".service"
            if "/" in n or n.startswith(".") or not re.fullmatch(r"[A-Za-z0-9@:_\-.]+", n):
                continue
            if n == "gx10-dashboard.service":
                continue
        elif k == "port":
            # bare endpoint (e.g. an sglang/vllm server not in a unit or
            # container): name is a user-chosen label, port is the handle.
            if not re.fullmatch(r"[a-zA-Z0-9_.\-]+", n):
                continue
        elif not re.fullmatch(r"[a-zA-Z0-9_.\-]+", n):
            continue
        port = e.get("port")
        try:
            port = int(port) if port not in (None, "") else None
        except (TypeError, ValueError):
            port = None
        if port is not None and not (1 <= port <= 65535):
            port = None
        model = e.get("model")
        label = e.get("label")
        out.append({
            "kind": k,
            "name": n,
            "label": str(label).strip() if label not in (None, "") else None,
            "port": port,
            "model": str(model).strip() if model not in (None, "") else None,
            "enabled": bool(e.get("enabled", True)),
        })
    return out


def _normalize_cost(cost):
    """Normalize the optional cost block (cloud SKU $ vs local energy $).
    Prices are $/M-tokens for input/output; usd_per_kwh is the energy tariff.
    Returns {} when the block is absent/disabled so callers can no-op."""
    if not isinstance(cost, dict):
        return {}
    cur = str(cost.get("currency", "USD")).upper()
    out = {"enabled": bool(cost.get("enabled", True)),
           "usd_per_kwh": _f(cost.get("usd_per_kwh")),
           "currency": cur if cur in ("USD", "EUR") else "USD"}
    skus = []
    for s in cost.get("skus", []) or []:
        if not isinstance(s, dict):
            continue
        name = str(s.get("name", "")).strip()
        ip, op = _f(s.get("in_price")), _f(s.get("out_price"))
        if not name or ip is None or op is None:
            continue
        skus.append({"name": name, "in_price": ip, "out_price": op})
    out["skus"] = skus
    return out


def _f(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_ENGINE_NAME_RE = re.compile(r"^[A-Za-z0-9@:_\-.]+$")


def validate_engine_entry(e, i, seen):
    """Validate one engines[] entry. Raises ValueError with a message."""
    if not isinstance(e, dict):
        raise ValueError("engines[%d] must be an object" % i)
    k = e.get("kind")
    if k not in ("unit", "docker", "port"):
        raise ValueError("engines[%d].kind must be 'unit', 'docker' or 'port'" % i)
    n = str(e.get("name", "")).strip()
    if k == "unit":
        if not n.endswith(".service"):
            n += ".service"
        if "/" in n or not _ENGINE_NAME_RE.match(n):
            raise ValueError("engines[%d].name must be a plain .service basename" % i)
        if n == "gx10-dashboard.service":
            raise ValueError("engines[%d]: dashboard service cannot be managed here" % i)
        n += ""  # keep as .service
    elif k == "port":
        # bare endpoint: name is a user-chosen label, port is the handle.
        if not re.fullmatch(r"[a-zA-Z0-9_.\-]+", n):
            raise ValueError("engines[%d].name (port kind) may use letters, digits, _ . -" % i)
    else:
        if not re.fullmatch(r"[a-zA-Z0-9_.\-]+", n):
            raise ValueError("engines[%d].name must be a docker container name" % i)
    port = e.get("port")
    if port not in (None, ""):
        try:
            port = int(port)
            if not (1 <= port <= 65535):
                raise ValueError("engines[%d].port out of range" % i)
        except (TypeError, ValueError) as ex:
            if "out of range" in str(ex):
                raise
            raise ValueError("engines[%d].port must be an integer" % i)
    key = (k, n)
    if key in seen:
        raise ValueError("duplicate engine %s/%s" % (k, n))
    seen.add(key)
    return k, n


def read_config():
    """Return (raw_text, parsed_cfg, error). Cached by mtime."""
    global _CFG_CACHE
    try:
        st = os.stat(CONFIG_PATH)
    except OSError:
        return None, DEFAULT_CONFIG, None
    if _CFG_CACHE["mtime"] == st.st_mtime:
        return _CFG_CACHE["raw"], _CFG_CACHE["cfg"], _CFG_CACHE["err"]
    try:
        with open(CONFIG_PATH) as f:
            raw = f.read()
        cfg = _normalize_config(json.loads(raw))
        _CFG_CACHE.update(mtime=st.st_mtime, raw=raw, cfg=cfg, err=None)
    except Exception as e:
        _CFG_CACHE.update(mtime=st.st_mtime, raw=None, cfg=DEFAULT_CONFIG, err=str(e))
    return _CFG_CACHE["raw"], _CFG_CACHE["cfg"], _CFG_CACHE["err"]


def clear_config_cache():
    _CFG_CACHE.update(mtime=None, raw=None, cfg=None, err=None)


def validate_config(raw):
    """Validate raw config JSON. Returns (normalized_cfg, error)."""
    try:
        cfg = json.loads(raw)
    except Exception as e:
        return None, "invalid JSON: %s" % e
    if not isinstance(cfg, dict):
        return None, "config must be a JSON object"
    roots = cfg.get("model_roots", [])
    if not isinstance(roots, list):
        return None, "model_roots must be a list"
    seen = set()
    for i, r in enumerate(roots):
        if not isinstance(r, dict):
            return None, "model_roots[%d] must be an object" % i
        p = r.get("path", "")
        if not isinstance(p, str) or not p.startswith("/"):
            return None, "model_roots[%d].path must be an absolute path" % i
        k = r.get("kind", "gguf")
        if k not in ("gguf", "hf"):
            return None, "model_roots[%d].kind must be 'gguf' or 'hf'" % i
        if p in seen:
            return None, "duplicate root %s" % p
        seen.add(p)
    # engines
    engines = cfg.get("engines", [])
    if not isinstance(engines, list):
        return None, "engines must be a list"
    eseen = set()
    for i, e in enumerate(engines):
        try:
            validate_engine_entry(e, i, eseen)
        except ValueError as ex:
            return None, str(ex)
    # cost (optional)
    cost = cfg.get("cost")
    if cost is not None and not isinstance(cost, dict):
        return None, "cost must be an object"
    return _normalize_config(cfg), None


# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------

def _scan_gguf_files(path):
    """Walk a gguf root, return list of file dicts (unsharded)."""
    files = []
    if not os.path.isdir(path):
        return files
    for root, _dirs, files_ in os.walk(path):
        for fn in files_:
            if not fn.lower().endswith(".gguf"):
                continue
            p = os.path.join(root, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            files.append({"path": p, "name": fn, "gib": round(st.st_size / 1073741824, 2)})
    return files


def _scan_hf_repos(hub_dir):
    """List HF hub repos with total blob size (vLLM pulls by repo id)."""
    repos = []
    if not os.path.isdir(hub_dir):
        return repos
    for d in sorted(os.listdir(hub_dir)):
        if not d.startswith("models--"):
            continue
        repo = d[len("models--"):].replace("--", "/")
        size = 0
        blobs = os.path.join(hub_dir, d, "blobs")
        if os.path.isdir(blobs):
            for b in os.listdir(blobs):
                try:
                    size += os.stat(os.path.join(blobs, b)).st_size
                except OSError:
                    pass
        repos.append({
            "path": repo,
            "name": repo,
            "gib": round(size / 1073741824, 2),
        })
    return repos


def group_shards(file_list):
    """Group -NNNNN-of-MMMMM shard files into single logical entries.

    Returns a list of entries: singletons unchanged (name kept), shards
    collapsed to {path: first shard, name: stem, gib: total, shards: N,
    parts: [paths]}.
    """
    out = []
    groups = {}
    for f in file_list:
        stem_name = f["name"][:-5] if f["name"].lower().endswith(".gguf") else f["name"]
        m = SHARD_RE.match(stem_name)
        if m:
            stem, n = m.group("stem"), int(m.group("n"))
            groups.setdefault((f["path"].rsplit("/", 1)[0], stem, n), []).append(f)
        else:
            out.append(f)
    for (d, stem, n), parts in groups.items():
        if len(parts) == n and n > 1:
            parts.sort(key=lambda p: int(SHARD_RE.match(p["name"][:-5]).group("idx")))
            out.append({
                "path": parts[0]["path"],
                "name": stem,
                "gib": round(sum(p["gib"] for p in parts), 2),
                "shards": n,
                "parts": [p["path"] for p in parts],
                "full": True,
            })
        else:
            # incomplete shard set: keep as individual files
            out.extend(parts)
    out.sort(key=lambda e: (e["name"], e["path"]))
    return out


def model_catalog(force=False):
    """Model entries from all enabled roots, shard-grouped. Cached (TTL)."""
    import time
    _raw, cfg, _err = read_config()
    enabled = [r for r in cfg["model_roots"] if r["enabled"]]
    key = repr(sorted((r["path"], r["kind"]) for r in enabled))
    now = time.time()
    if not force and _CAT_CACHE["key"] == key and now - _CAT_CACHE["ts"] < CATALOG_TTL:
        return _CAT_CACHE["entries"]

    entries = []
    for r in enabled:
        if r["kind"] == "gguf":
            files = _scan_gguf_files(r["path"])
            for e in group_shards(files):
                e["kind"] = "gguf"
                e["source"] = r["path"]
                entries.append(e)
        else:
            for e in _scan_hf_repos(r["path"]):
                e["kind"] = "hf"
                e["source"] = "huggingface"
                entries.append(e)
    entries.sort(key=lambda e: (e["source"], e["name"]))
    _CAT_CACHE.update(key=key, ts=now, entries=entries)
    return entries


def root_stats(force=False):
    """Per-root stats for the settings page (exists, model count, GiB)."""
    import time
    now = time.time()
    if not force and now - _STATS_CACHE["ts"] < STATS_TTL and _STATS_CACHE["stats"] is not None:
        return _STATS_CACHE["stats"]
    _raw, cfg, err = read_config()
    rows = []
    for r in cfg["model_roots"]:
        exists = os.path.isdir(r["path"])
        count, gib = 0, 0.0
        if exists:
            if r["kind"] == "gguf":
                files = _scan_gguf_files(r["path"])
                count = len(group_shards(files))
                gib = sum(f["gib"] for f in files)
            else:
                repos = _scan_hf_repos(r["path"])
                count = len(repos)
                gib = sum(f["gib"] for f in repos)
        rows.append({
            "path": r["path"],
            "kind": r["kind"],
            "enabled": r["enabled"],
            "exists": exists,
            "models": count,
            "gib": round(gib, 1),
            "error": err if not exists else None,
        })
    _STATS_CACHE.update(ts=now, stats=rows)
    return rows


def scan_preview(cfg_dict):
    """What WOULD be cataloged from a candidate config (for the diff modal)."""
    total, byroot = 0, []
    for r in cfg_dict.get("model_roots", []):
        if not r.get("enabled", True):
            continue
        count, gib = 0, 0.0
        if r["kind"] == "gguf":
            files = _scan_gguf_files(r["path"])
            count = len(group_shards(files))
            gib = sum(f["gib"] for f in files)
        else:
            repos = _scan_hf_repos(r["path"])
            count = len(repos)
            gib = sum(f["gib"] for f in repos)
        byroot.append({"path": r["path"], "kind": r["kind"], "models": count, "gib": round(gib, 1)})
        total += count
    return {"roots": byroot, "total_models": total, "total_gib": round(sum(x["gib"] for x in byroot), 1)}


# ---------------------------------------------------------------------------
# Docker containers + recipes
# ---------------------------------------------------------------------------

def docker_containers(include_exited=True):
    """List engine-relevant docker containers with their live state."""
    out = run("docker ps -a --format '{{.Names}}\\t{{.Image}}\\t{{.Status}}\\t{{.Ports}}'")
    rows = []
    for line in (out.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        name, image, status = parts[0], parts[1], parts[2]
        ports = parts[3] if len(parts) > 3 else ""
        # keep inference-engine containers (image/name hint: vllm or sglang
        # — the SGLang image qwen38-27b-sglang-dflash2-sm121 has no "vllm")
        hint = image.lower() + " " + name.lower()
        if "vllm" not in hint and "sglang" not in hint:
            continue
        running = status.startswith("Up")
        if not running and not include_exited:
            continue
        host_port = None
        m = re.search(r"(\d+)->(\d+)/tcp", ports.replace("\t", " "))
        if m:
            host_port = int(m.group(1))
        rows.append({
            "name": name,
            "image": image,
            "status": status,
            "running": running,
            "host_port": host_port,
        })
    return rows


def _docker_inspect_json(name):
    r = run(f"docker inspect {name}")
    if r.returncode != 0:
        raise RuntimeError(f"docker inspect {name}: {r.stderr.strip()}")
    return json.loads(r.stdout)[0]


def recipe_from_inspect(name):
    """Build a recipe dict from a live/exited container's inspect data."""
    d = _docker_inspect_json(name)
    cfg = d.get("Config", {})
    hc = d.get("HostConfig", {})
    cmd = cfg.get("Cmd") or []
    envs = []
    for e in cfg.get("Env", []) or []:
        # skip docker-injected defaults
        if e.startswith(("PATH=", "HOSTNAME=", "HOME=", "TERM=", "container=", "LS_COLORS=", "PYTHONPATH=", "PYTHON_", "CPLUS_INCLUDE", "C_INCLUDE", "LD_LIBRARY", "PKG_", "GPG_", "TZ=", "DEBIAN", "LESSCLOSE", "LESSOPEN", "HOSTNAME", "NVIDIA_REQUIRE", "NVIDIA_VISIBLE", "NVIDIA_DRIVER", "DOCKER_IMAGE")):
            continue
        envs.append(e)
    binds = hc.get("Binds") or []
    # normalize binds to src:dst:mode
    mounts = []
    for b in binds:
        parts = b.split(":")
        if len(parts) >= 2:
            mounts.append({"src": parts[0], "dst": parts[1], "mode": parts[2] if len(parts) > 2 else ""})
    port_bindings = []
    for container_port, lst in (hc.get("PortBindings") or {}).items():
        for b in lst or []:
            port_bindings.append({
                "host": str(b.get("HostPort", "")),
                "container": container_port.replace("/tcp", ""),
                "host_ip": b.get("HostIp", "127.0.0.1"),
            })
    rec = {
        "name": name,
        "image": cfg.get("Image"),
        "cmd": cmd,
        "env": envs,
        "mounts": mounts,
        "ports": port_bindings,
        "network": d.get("NetworkSettings", {}).get("Networks", {}) and list(d.get("NetworkSettings", {}).get("Networks", {}).keys())[0],
        "restart": hc.get("RestartPolicy", {}).get("Name", "no"),
        "workdir": cfg.get("Workdir"),
        "created_from_inspect": d.get("Created"),
    }
    # derive served model: --served-model-name is the true display name when
    # present, then --model-path, then the first positional arg after 'serve'.
    model = None
    if "--served-model-name" in cmd and cmd.index("--served-model-name") + 1 < len(cmd):
        model = cmd[cmd.index("--served-model-name") + 1]
    elif "--model-path" in cmd and cmd.index("--model-path") + 1 < len(cmd):
        model = cmd[cmd.index("--model-path") + 1]
    elif "serve" in cmd:
        i = cmd.index("serve")
        if i + 1 < len(cmd):
            model = cmd[i + 1]
    rec["model"] = model
    return rec


def load_recipe(name):
    p = os.path.join(RECIPE_DIR, f"{name}.json")
    if os.path.isfile(p):
        with open(p) as f:
            return json.load(f)
    return None


def save_recipe(rec):
    os.makedirs(RECIPE_DIR, exist_ok=True)
    p = os.path.join(RECIPE_DIR, f"{rec['name']}.json")
    with open(p, "w") as f:
        json.dump(rec, f, indent=2)
    return p


def ensure_recipe(name):
    """Return the recipe for a container, importing from inspect if absent."""
    rec = load_recipe(name)
    if rec is None:
        rec = recipe_from_inspect(name)
        save_recipe(rec)
    return rec


def docker_run_command(rec):
    """Render a docker run command line from a recipe (for preview)."""
    parts = ["docker", "run", "--name", rec["name"]]
    if rec.get("restart"):
        parts += ["--restart", rec["restart"]]
    for mp in rec.get("mounts", []):
        v = mp["src"] + ":" + mp["dst"] + (":" + mp["mode"] if mp.get("mode") else "")
        parts += ["-v", v]
    for e in rec.get("env", []):
        parts += ["-e", e]
    for pb in rec.get("ports", []):
        parts += ["-p", f"{pb['host_ip']}:{pb['host']}:{pb['container']}"]
    if rec.get("network") and rec["network"] != "bridge":
        parts += ["--network", rec["network"]]
    parts.append(rec["image"])
    parts += rec.get("cmd", [])
    return parts


def docker_apply(rec, confirm_running_loss=True):
    """Recreate the container from the recipe. Returns (ok, detail)."""
    name = rec["name"]
    # stop + remove if present
    run(f"docker stop {name}")
    run(f"docker rm {name}")
    parts = docker_run_command(rec)
    cmd = " ".join(_shq(x) for x in parts)
    r = run(cmd, timeout=60)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()
    return True, f"recreated {name}"


def _shq(s):
    if re.fullmatch(r"[\w./:=@+{},-]+", s or ""):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


def docker_logs(name, tail=200):
    r = run(f"docker logs --tail {tail} {name} 2>&1")
    return (r.stdout or "") + (r.stderr or "")


def engine_logs(unit, tail=120):
    r = run(f"journalctl -u {unit} --no-pager -n {tail} 2>&1")
    return r.stdout or ""


# ---------------------------------------------------------------------------
# Engine discovery (systemd units + docker containers)
# ---------------------------------------------------------------------------

SYSTEMD_SYSTEM = "/etc/systemd/system"
_UNIT_ENGINE_HINT = re.compile(r"llama-server|llama-cli|llama-server-bench|ds4-server|vllm|/vllm|llama-cpp|sglang|ollama", re.I)
_EXCLUDE_UNITS = {"gx10-dashboard.service"}


def _cmd_is_inference(cmd):
    """Heuristic: does this ExecStart command look like an LLM engine?"""
    if not cmd:
        return False
    if _UNIT_ENGINE_HINT.search(cmd):
        return True
    # python -m vllm / sglang / llama stack
    if "python" in cmd.lower() and any(x in cmd.lower() for x in ("vllm", "sglang", "llama")):
        return True
    return False


def infer_engine(binary):
    """Classify an engine from its ExecStart binary path."""
    if not binary:
        return "unknown"
    low = binary.lower()
    if "vllm" in low:
        return "vllm"
    if "ds4-server" in low or "/ds4/" in low:
        return "ds4"
    m = re.search(r"llama-cpp-([a-z0-9]+)", binary)
    if m:
        return m.group(1)
    if "llama-server" in low or "llama" in low:
        return "llama"
    return "unknown"


def unit_file_present(name):
    """True if the .service file exists in /etc/systemd/system."""
    if not name.endswith(".service"):
        name += ".service"
    return os.path.isfile(os.path.join(SYSTEMD_SYSTEM, name))


def _listening_ports():
    """Set of TCP ports currently in LISTEN state (from /proc/net/tcp{,6})."""
    ports = set()
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(f) as fh:
                for line in fh.readlines()[1:]:
                    p = line.split()
                    if len(p) > 3 and p[3] == "0A":  # LISTEN
                        ports.add(int(p[1].split(":")[1], 16))
        except OSError:
            pass
    return ports


def _unit_active(name):
    try:
        return subprocess.run(
            f"systemctl is-active {name} 2>/dev/null", shell=True,
            capture_output=True, text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _port_probe_model(port, timeout=1.2):
    """Best-effort: what /v1/models serves on a port (None if down)."""
    import urllib.request
    try:
        req = urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=timeout)
        data = json.load(req)
        ids = [mm.get("id") for mm in data.get("data", [])]
        return ids[0] if ids else None
    except Exception:
        return None


def discovery_candidates():
    """Scan for inference engines the user can import into config.

    Returns {units:[...], docker:[...], listening:{port:names}}. Read-only.
    """
    units = []
    try:
        names = [n for n in os.listdir(SYSTEMD_SYSTEM)
                 if n.endswith(".service") and n not in _EXCLUDE_UNITS]
    except OSError:
        names = []
    for n in sorted(names):
        path = os.path.join(SYSTEMD_SYSTEM, n)
        try:
            u = engines.parse_unit(path)
        except Exception as e:
            # can't parse -> can't classify; skip (not an engine we can read)
            continue
        if not _cmd_is_inference(u.get("_cmd")):
            continue
        d = u["derived"]
        engine = d["fork"] or infer_engine(u.get("_binary"))
        if engine == "unknown":
            engine = "llama"
        units.append({
            "kind": "unit",
            "name": n,
            "engine": engine,
            "port": int(d["port"]) if str(d["port"] or "").isdigit() else None,
            "model": (d["model"] or "").rsplit("/", 1)[-1] if d["model"] else None,
            "active": _unit_active(n),
        })
    containers = docker_containers(include_exited=True)
    docker = []
    for c in containers:
        model = None
        try:
            rec = ensure_recipe(c["name"])
            model = rec.get("model")
        except Exception:
            pass
        docker.append({
            "kind": "docker",
            "name": c["name"],
            "engine": "sglang" if "sglang" in c.get("image", "").lower() else "vllm",
            "port": c.get("host_port"),
            "model": (model or "").rsplit("/", 1)[-1] if model else None,
            "image": c.get("image"),
            "active": "active" if c.get("running") else "inactive",
        })
    # port -> which configured/discovered engines claim it (conflict map)
    listeners = _listening_ports()
    port_map = {}
    for e in units + docker:
        p = e.get("port")
        if p:
            port_map.setdefault(p, []).append(e["name"])
    return {"units": units, "docker": docker, "listening": {str(k): v for k, v in port_map.items()}}


def _port_in_use(port):
    return port in _listening_ports() if port else False
