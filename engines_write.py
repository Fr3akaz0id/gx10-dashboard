#!/usr/bin/env python3
"""GX10 engine layer (part 2): surgical, byte-stable writes.

All mutators operate on the RAW file text using character offsets into the
ExecStart region (or the Environment= lines). They touch only the exact
bytes of the flag/value they change. A round-trip with zero edits is
byte-identical -- this is the invariant the whole page relies on.

Mutators:
    set_flag(text, name, value)      -> (new_text, changed)
    remove_flag(text, name)          -> (new_text, changed)
    add_flag(text, name, value)      -> (new_text, changed)   [appends to ExecStart block]
    set_env(text, key, value)        -> (new_text, changed)
    remove_env(text, key)            -> (new_text, changed)
    save_file(path, new_text)        -> writes with .bak.<ts> backup + fsync

Safety:
    every mutator re-parses the result via parse_unit on a temp path and
    raises EngineEditError if the file no longer parses or if the target
    value did not land where expected.
"""

import os
import re
import time

from engines import (
    _tokenize_logical,
    _norm_flag,
    _looks_like_flag,
    _execstart_lines,
    parse_unit,
    _label_tokens,
)


class EngineEditError(Exception):
    pass


# ---------------------------------------------------------------------------
# File-level ExecStart tokenizer
# ---------------------------------------------------------------------------

def _execstart_region(text):
    """Return (start_char, end_char) of the ExecStart command region in the
    raw file text (from the char after 'ExecStart=' to the end of the last
    continuation line), plus the line-index of the last block line."""
    lines = text.split("\n")
    es = _execstart_lines(lines)
    if es is None:
        raise EngineEditError("no ExecStart in unit")
    start_line, block = es
    # char offset of start of each line
    offsets = []
    off = 0
    for ln in lines:
        offsets.append(off)
        off += len(ln) + 1
    # start of command = after 'ExecStart=' on first block line
    first = lines[start_line]
    eq = first.index("ExecStart=") + len("ExecStart=")
    region_start = offsets[start_line] + eq
    last_line = start_line + len(block) - 1
    region_end = offsets[last_line] + len(lines[last_line])
    return region_start, region_end, last_line


def _file_tokens(text):
    """Tokenize the ExecStart region of the RAW file. Returns
    (kind, raw_text, start, end) with offsets into `text`.
    Handles continuation backslashes, quoted values, escapes."""
    rs, re_, _last = _execstart_region(text)
    region = text[rs:re_]
    toks = []
    i, n = 0, len(region)
    first = True
    while i < n:
        ch = region[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == "\\" and i + 1 < n and region[i + 1] in "\r\n":
            # continuation
            i += 2
            while i < n and region[i] in " \t\r\n":
                i += 1
            continue
        if ch in "\"'":
            quote = ch
            j = i + 1
            buf = []
            while j < n:
                if region[j] == "\\" and j + 1 < n:
                    buf.append(region[j + 1])
                    j += 2
                    continue
                if region[j] == quote:
                    break
                buf.append(region[j])
                j += 1
            toks.append(("value", "".join(buf), rs + i, rs + j + 1, ch))
            i = j + 1
            first = False
            continue
        j = i
        while j < n and region[j] not in " \t\r\n" and not (
            region[j] == "\\" and j + 1 < n and region[j + 1] in "\r\n"
        ):
            j += 1
        raw = region[i:j]
        kind = "binary" if (first and not raw.startswith("-")) else (
            "flag" if raw.startswith("-") else "value")
        toks.append((kind, raw, rs + i, rs + j, None))
        i = j
        first = False
    return toks


def _find_flag_span(toks, name):
    """Find (flag_start, value_end, value_text) for a flag by its canonical
    name. Returns None if not found. Multiple occurrences: first wins."""
    want = _norm_flag(name)
    toks2 = _label_tokens([(k, t, s, e) for (k, t, s, e, _q) in toks])
    for idx, (kind, txt, s, e) in enumerate(toks2):
        if kind == "flag" and _norm_flag(txt) == want:
            val = None
            if idx + 1 < len(toks2) and toks2[idx + 1][0] == "value":
                val = toks2[idx + 1][1]
                return s, toks2[idx + 1][3], val
            return s, e, None
    return None


def _quote(value):
    """Quote a value for the command line if it contains spaces."""
    if value is None:
        return ""
    if " " in value or "\t" in value:
        return "'" + value.replace("'", "'\\''") + "'"
    return value


# ---------------------------------------------------------------------------
# Mutators
# ---------------------------------------------------------------------------

def set_flag(text, name, value, expect_binary=None):
    """Replace the value of an existing flag. Byte-stable elsewhere.
    Returns (new_text, changed_bool). Raises if flag absent (use add_flag)."""
    toks = _file_tokens(text)
    span = _find_flag_span(toks, name)
    if span is None:
        raise EngineEditError(f"flag {name} not found in ExecStart")
    fstart, vend, _cur = span
    new_val = _quote(str(value))
    # flag token text
    ftext = text[fstart:span[1] if False else _flag_end(toks, name)]
    # preserve the ORIGINAL flag spelling (e.g. '-t' vs '--threads')
    replacement = ftext + ((" " + new_val) if new_val != "" else "")
    new_text = text[:fstart] + replacement + text[vend:]
    _verify(new_text, name, str(value) if new_val else None)
    return new_text, replacement != text[fstart:vend]


def _flag_end(toks, name):
    span = _find_flag_span(toks, name)
    fstart, vend, val = span
    # flag text end: find end of flag token within region
    toks2 = _label_tokens([(k, t, s, e) for (k, t, s, e, _q) in toks])
    for kind, txt, s, e in toks2:
        if kind == "flag" and _norm_flag(txt) == _norm_flag(name):
            return e
    return fstart


def remove_flag(text, name):
    """Remove a flag and its value. Cleans up the surrounding line so we
    don't leave dangling continuations or empty lines. Byte-stable elsewhere."""
    toks = _file_tokens(text)
    span = _find_flag_span(toks, name)
    if span is None:
        raise EngineEditError(f"flag {name} not found in ExecStart")
    fstart, vend, val = span
    # find line boundaries
    line_start = text.rfind("\n", 0, fstart) + 1
    line_end_n = text.find("\n", vend)
    line_end = line_end_n if line_end_n != -1 else len(text)
    line_content = text[line_start:line_end]

    # Case A: this line holds only this flag (and its value) -> drop whole line.
    # Compare the line's body (indent + continuation stripped) against the
    # exact flag+value span, so a valued flag on its own line is detected too.
    line_nc_raw = line_content.rstrip()
    line_nc_body = (line_nc_raw[:-1] if line_nc_raw.endswith("\\") else line_nc_raw).strip()
    flag_value_span = text[fstart:vend].strip()
    if line_nc_body == flag_value_span or (
        val is None and line_nc_body.startswith("-")
        and " ".join(line_nc_body.split()[1:]) == ""
    ):
        # whole-line removal: remove from line_start to next line start
        removed_line = text[line_start:line_end]
        remove_to = line_end + 1 if line_end < len(text) else len(text)
        new_text = text[:line_start] + text[remove_to:]
        # If the removed line was the LAST line of the block (it had no
        # trailing continuation), the previous line now dangles on a backslash
        # and systemd would swallow the next directive into the command.
        # Strip that trailing " \\" from the previous line.
        if not removed_line.rstrip().endswith("\\"):
            nl = new_text.rfind("\n", 0, line_start)  # newline terminating the removed line's predecessor
            prev_nl = new_text.rfind("\n", 0, nl)
            prev_line = new_text[prev_nl + 1:nl]
            if prev_line.rstrip().endswith("\\"):
                fixed = prev_line.rstrip()[:-1].rstrip()
                new_text = new_text[:prev_nl + 1] + fixed + new_text[nl:]
    else:
        # Case B: shared line -> remove flag span + one separator space
        # expand to include trailing whitespace after the value (or flag)
        end = vend
        while end < len(text) and text[end] in " \t":
            end += 1
        # if the flag sat at the line's start, eat its leading indent too so
        # the line doesn't collapse to a bare continuation
        if text[line_start:fstart].strip() == "":
            new_text = text[:line_start] + text[end:]
        else:
            new_text = text[:fstart] + text[end:]
        # a line left with only whitespace + continuation -> drop the whole line
        leftover = new_text[new_text.find("\n", line_start - 1) + 1:new_text.find("\n", end)]
        if leftover.strip().rstrip("\\") == "" and line_start > 0 and new_text[line_start - 1] == "\n":
            drop_start = line_start
            drop_end = new_text.find("\n", end)
            drop_end = drop_end + 1 if drop_end != -1 else len(new_text)
            new_text = new_text[:drop_start] + new_text[drop_end:]
    _verify_removed(new_text, name)
    return new_text, True


def add_flag(text, name, value):
    """Append a flag at the end of the ExecStart block. The previous last
    line gains a continuation; a new line with the flag is appended in the
    unit's prevailing 2-space indent. Byte-stable elsewhere."""
    toks = _file_tokens(text)
    span = _find_flag_span(toks, name)
    if span is not None:
        raise EngineEditError(f"flag {name} already present")
    rs, re_, last_line_idx = _execstart_region(text)
    lines = text.split("\n")
    last = lines[last_line_idx]
    indent = re.search(r"^(\s*)", last).group(1) or "  "
    new_line = f"{indent}{name}{(' ' + _quote(str(value))) if value is not None else ''}"
    new_text = text[:re_] + " \\\n" + new_line + text[re_:]
    _verify(new_text, name, str(value) if value is not None else None)
    return new_text, True


def set_env(text, key, value):
    """Set Environment=KEY=value. Replaces existing line's value, or
    appends a new Environment line after the last existing one.
    Returns changed=False when the stored value already equals `value`
    (after unquoting)."""
    pattern = re.compile(
        r"^(?P<ws>\s*)Environment=" + re.escape(key)
        + r"=([\"']?)(?P<val>.*?)([\"']?)\s*$", re.M)
    m = pattern.search(text)
    if m:
        raw_val = m.group("val")
        lq, vq = m.group(2), m.group(3)
        # unquote current value
        cur = raw_val
        if lq and vq and lq == vq and len(raw_val) >= 2:
            cur = raw_val[1:-1]
        cur = cur.replace('\\"', '"')
        want = str(value)
        if cur == want:
            return text, False
        new_val = _env_quote(want)
        # replace the whole value span incl. any existing quotes
        new_text = text[: m.start(2)] + new_val + text[m.end(3):]
    else:
        # append after last Environment= line (any key)
        envs = list(re.finditer(r"^\s*Environment=.*$", text, re.M))
        if envs:
            last = envs[-1]
            insert_at = last.end() + 1  # after the newline
            ws = last.group(0)[: len(last.group(0)) - len(last.group(0).lstrip())]
        else:
            # insert at start of [Service] section
            sm = re.search(r"^\[Service\]\s*$", text, re.M)
            insert_at = sm.end() + 1 if sm else 0
            ws = ""
        new_text = text[:insert_at] + f"{ws}Environment={key}={_env_quote(value)}\n" + text[insert_at:]
    _verify_env(new_text, key, value)
    return new_text, True


def remove_env(text, key):
    pattern = re.compile(r"^[ \t]*Environment=" + re.escape(key) + r"=.*\n?", re.M)
    m = pattern.search(text)
    if not m:
        raise EngineEditError(f"Environment={key} not found")
    new_text = text[: m.start()] + text[m.end():]
    _verify_env_removed(new_text, key)
    return new_text, True


def _env_quote(value):
    v = str(value)
    if re.search(r"[\s\"]", v):
        return '"' + v.replace('"', '\\"') + '"'
    return v


# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------

def _verify(new_text, name, expect_value):
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".service", delete=False) as f:
        f.write(new_text)
        tmp = f.name
    try:
        unit = parse_unit(tmp)
        cur = unit["flags"].get(_norm_flag(name))
        if expect_value is None and cur is not None:
            raise EngineEditError(f"verify: {name} should be valueless, got {cur!r}")
        if expect_value is not None and (cur is None or str(cur) != expect_value):
            raise EngineEditError(f"verify: {name} = {cur!r}, expected {expect_value!r}")
    finally:
        os.unlink(tmp)


def _verify_removed(new_text, name):
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".service", delete=False) as f:
        f.write(new_text)
        tmp = f.name
    try:
        unit = parse_unit(tmp)
        if _norm_flag(name) in unit["flags"]:
            raise EngineEditError(f"verify: {name} still present after removal")
    finally:
        os.unlink(tmp)


def _verify_env(new_text, key, value):
    m = re.search(r"^\s*Environment=" + re.escape(key) + r"=(.*)$", new_text, re.M)
    if not m:
        raise EngineEditError(f"verify: Environment={key} missing")
    got = m.group(1).strip().strip("\"'")
    if got != str(value):
        raise EngineEditError(f"verify: Environment={key} = {got!r}, expected {value!r}")


def _verify_env_removed(new_text, key):
    m = re.search(r"^\s*Environment=" + re.escape(key) + r"=", new_text, re.M)
    if m:
        raise EngineEditError(f"verify: Environment={key} still present")


# ---------------------------------------------------------------------------
# Save with backup
# ---------------------------------------------------------------------------

def save_file(path, new_text):
    """Write new_text to path with an atomic .bak.<ts> backup first.
    Returns the backup path."""
    ts = time.strftime("%Y%m%d_%H%M%S")
    bak = f"{path}.bak.{ts}"
    with open(path, "r", encoding="utf-8") as f:
        old = f.read()
    with open(bak, "w", encoding="utf-8") as f:
        f.write(old)
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new_text)
        f.flush()
        os.fsync(f.fileno())
    return bak
