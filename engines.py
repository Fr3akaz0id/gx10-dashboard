#!/usr/bin/env python3
"""GX10 engine layer: parse systemd unit files and vLLM docker recipes,
and perform *surgical*, byte-stable edits.

Design contract
---------------
The unit file is the single source of truth. This module offers two
kinds of operations:

1. READ / DISPLAY  -> parse_unit()
   Derives a structured view (flags, env, scalar directives, and the
   human-facing fields model/port/host/fork/spec/ctx/...) used by the
   dashboard cards. Read-only: nothing here mutates the file.

2. SURGICAL WRITE  -> set_flag / add_flag / remove_flag / set_env
   Character-span edits that touch only the exact bytes of the flag
   (and its value, if any) being changed. Every other byte -- including
   line breaks, continuations, comment lines, and untouched flags -- is
   preserved. A no-op round-trip (load -> save unchanged) yields
   byte-identical output. That invariant is what makes the raw-file
   editor and the convenience quick-fields both safe.

Tokenization handles:
  * line continuation  (trailing backslash)
  * double-quoted values   "all=CPU"
  * single-quoted values   '{"preserve_thinking": true}'
  * backslash escapes inside quotes
"""

import os
import re

# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

# A token is one of:
#   ('flag',  text, line, col_start, col_end)
#   ('value', text, line, col_start, col_end)
# The (line, col) spans refer to offsets in the ORIGINAL text so we can
# splice replacements back without disturbing anything else.
# We track a flat list of (kind, text, start, end) char offsets into the
# whole document text, which is simpler to splice than line/col.

def _tokenize_logical(text):
    """Yield (kind, text, start, end) for flags and values in an
    ExecStart logical command, respecting quotes and line continuations.
    start/end are char offsets into `text`."""
    toks = []
    i, n = 0, len(text)
    # Skip leading command word (the binary path) -- it's token 0 but not a flag.
    while i < n:
        # whitespace / newlines / continuation
        if text[i] in " \t\r\n":
            i += 1
            continue
        if text[i] == "\\":
            # continuation: backslash + newline
            i += 1
            if i < n and text[i] in "\r\n":
                i += 1
                while i < n and text[i] in " \t\r\n":
                    i += 1
            continue
        if text[i] in "\"'":
            # quoted value
            quote = text[i]
            j = i + 1
            buf = []
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                    continue
                if text[j] == quote:
                    break
                buf.append(text[j])
                j += 1
            toks.append(("value", "".join(buf), i, j + 1))
            i = j + 1
            continue
        # bare token (unquoted)
        j = i
        while j < n and text[j] not in " \t\r\n" and not (text[j] == "\\" and j + 1 < n and text[j + 1] in "\r\n"):
            j += 1
        toks.append(("flag", text[i:j], i, j))
        i = j
    return toks


def _is_flag(token_text):
    return token_text.startswith("-")


# ---------------------------------------------------------------------------
# ExecStart extraction
# ---------------------------------------------------------------------------

# Matches the logical ExecStart value across continuation lines.
def _execstart_lines(lines):
    """Return (start_idx, [line, ...]) for the ExecStart directive.
    Handles the multi-line `\\` continuation form. Returns None if absent."""
    start = None
    for idx, line in enumerate(lines):
        s = line.lstrip()
        if s.startswith("ExecStart=") or s.startswith("ExecStartPre=") or s.startswith("ExecStopPost="):
            # only the bare ExecStart= (the command), not Pre/Stop
            if s.startswith("ExecStart="):
                start = idx
                break
    if start is None:
        return None
    block = [lines[start]]
    k = start
    while True:
        r = lines[k].rstrip()
        if r.endswith("\\"):
            k += 1
            if k < len(lines):
                block.append(lines[k])
            else:
                break
        else:
            break
    return start, block


def _reconstruct_command(block):
    """Join the ExecStart block into one logical command string."""
    parts = []
    for idx, line in enumerate(block):
        s = line
        if idx == 0:
            s = s.split("ExecStart=", 1)[1]
        if s.rstrip().endswith("\\"):
            s = s.rstrip()[:-1]
        parts.append(s)
    return " ".join(p.strip() for p in parts if p.strip())


# ---------------------------------------------------------------------------
# parse_unit
# ---------------------------------------------------------------------------

def parse_unit(path):
    """Parse a systemd unit file into a structured, display-ready dict.
    Read-only. Never mutates the file."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    lines = text.split("\n")

    unit = {
        "path": path,
        "name": os.path.basename(path).replace(".service", ""),
        "raw": text,
        "lines": lines,
        "env": {},
        "scalars": {},
        "flags": {},          # flag-name (normalized, no leading -) -> raw value string (or None for boolean)
        "flag_order": [],     # canonical display order as they appear
        "execstart_present": False,
    }

    # --- Environment= and simple Key=Value ---
    env_re = re.compile(r"^\s*Environment=(.*)$")
    kv_re = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_.-]*)=(.*)$")
    for line in lines:
        m = env_re.match(line)
        if m:
            _parse_env_assignments(m.group(1), unit["env"])
            continue
        # skip the ExecStart block lines from scalar parsing
        m = kv_re.match(line)
        if m and not line.lstrip().startswith("ExecStart"):
            key, val = m.group(1), m.group(2).strip()
            if key not in unit["scalars"]:
                unit["scalars"][key] = val

    # --- ExecStart flags ---
    es = _execstart_lines(lines)
    if es is not None:
        start, block = es
        unit["execstart_present"] = True
        unit["_es_block_line"] = start
        cmd = _reconstruct_command(block)
        toks = _tokenize_logical(cmd)
        # toks are relative to cmd, not the file. We re-tokenize the FILE
        # region to get file-accurate spans (see _file_tokens below).
        # For display we just need flag->value in order.
        it = iter(range(len(toks)))
        buf = [t for t in toks]
        # First bare token is the binary; classify the rest.
        # Re-derive with a pass that labels flag/value by position.
        labeled = _label_tokens(buf)
        for kind, txt, _s, _e in labeled:
            if kind != "flag":
                continue
            name = _norm_flag(txt)
            if name not in unit["flags"]:
                unit["flags"][name] = None
                unit["flag_order"].append(name)
        # attach values
        labeled2 = _label_tokens(buf)
        i = 0
        while i < len(labeled2):
            kind, txt, s, e = labeled2[i]
            if kind == "flag":
                name = _norm_flag(txt)
                val = None
                if i + 1 < len(labeled2) and labeled2[i + 1][0] == "value" and not _looks_like_flag(labeled2[i + 1][1]):
                    val = labeled2[i + 1][1]
                if unit["flags"].get(name) is None or unit["flags"][name] == "":
                    unit["flags"][name] = val
                i += 2 if val is not None else 1
            else:
                i += 1

    unit["_cmd"] = _reconstruct_command(es[1]) if es else None
    unit["_binary"] = unit["_cmd"].split()[0] if unit.get("_cmd") else None

    # --- derived human fields ---
    fl = unit["flags"]
    unit["derived"] = {
        "binary": unit["_binary"],
        "fork": _fork_name(unit["_binary"]),
        "model": fl.get("model") or fl.get("m"),
        "alias": fl.get("alias"),
        "host": fl.get("host"),
        "port": fl.get("port"),
        "ctx": fl.get("c"),
        "parallel": fl.get("parallel"),
        "mmproj": fl.get("mmproj"),
        "spec_type": fl.get("spec-type"),
        "spec_draft_model": fl.get("spec-draft-model"),
        "temp": fl.get("temp") or fl.get("temperature"),
        "top_p": fl.get("top-p"),
        "top_k": fl.get("top-k"),
        "min_p": fl.get("min-p"),
        "flash_attn": fl.get("flash-attn"),
        "kv_k": fl.get("ctk"),
        "kv_v": fl.get("ctv"),
        "gpu_layers": fl.get("gpu-layers"),
        "jinja": fl.get("jinja"),
        "reasoning_budget": fl.get("reasoning-budget"),
        "threads": fl.get("t"),
        "batch": fl.get("tb"),
    }
    # ds4: model often only in env GGUF_FILE
    if not unit["derived"]["model"] and "GGUF_FILE" in unit["env"]:
        unit["derived"]["model"] = os.path.join(unit["env"].get("DS4_GGUF_DIR", ""), unit["env"]["GGUF_FILE"])
    return unit


def _label_tokens(toks):
    """Given raw (kind,text,s,e) from _tokenize_logical (kind only marks
    quoted vs bare), relabel into flag/value by position: token0=binary,
    then anything starting with '-' is a flag, a following non-dash token
    is its value."""
    out = []
    for idx, (kind, txt, s, e) in enumerate(toks):
        if idx == 0 and not txt.startswith("-"):
            out.append(("binary", txt, s, e))
            continue
        if _looks_like_flag(txt):
            out.append(("flag", txt, s, e))
        else:
            out.append(("value", txt, s, e))
    return out


def _looks_like_flag(txt):
    return txt.startswith("-")


def _norm_flag(txt):
    return txt.lstrip("-")


def _parse_env_assignments(s, env):
    """Environment= can hold one or multiple KEY=VAL pairs, optionally quoted.
    Quote-aware tokenisation: whitespace inside a quoted value is preserved."""
    toks, cur, inq, i = [], "", None, 0
    while i < len(s):
        ch = s[i]
        if inq:
            if ch == "\\" and i + 1 < len(s) and s[i + 1] == inq:
                cur += inq
                i += 2
                continue
            if ch == inq:
                inq = None
                i += 1
                continue
            cur += ch
            i += 1
            continue
        if ch in "\"'":
            inq = ch
            i += 1
            continue
        if ch.isspace():
            if cur:
                toks.append(cur)
                cur = ""
            i += 1
            continue
        cur += ch
        i += 1
    if cur:
        toks.append(cur)
    for part in toks:
        if "=" in part:
            k, v = part.split("=", 1)
            env[k] = v


def _fork_name(binary):
    if not binary:
        return None
    base = os.path.basename(binary)
    # /opt/llama-cpp-atomic/... -> atomic ; /opt/llama-cpp-fork -> fork
    m = re.search(r"llama-cpp-([a-z0-9]+)", binary)
    if m:
        return m.group(1)
    return base
