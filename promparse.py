"""Prometheus exposition-format parser + histogram percentile helpers.

Stdlib only. Tolerant of label ordering, _created series, and missing TYPE
lines (unknown scalar type defaults to gauge; vLLM/llama.cpp both emit TYPE).
Multi-sample gauges (multiple label sets, one series) are summed — matches
the sum(...) queries the Grafana dashboards use for the same series.
"""
import re

_LINE = re.compile(
    r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)$'
)
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="([^"]*)"')


def _val(s):
    if s == "+Inf":
        return float("inf")
    if s == "-Inf":
        return float("-inf")
    return float(s)


def _new_hist():
    # Buckets are accumulated per-`le` across label sets and _sum/_count are
    # summed across label sets, so a histogram whose series carry an extra
    # label (sglang's is_streaming={false,true} emits TWO full bucket
    # ladders + two _sum/_count per base name) merges into one. A single
    # label set (vLLM / llama.cpp) reduces to the exact same result as the
    # old append/overwrite path: sum of one value is the value.
    return {"_acc_b": {}, "sum": None, "count": None}


def parse(text):
    """Parse Prometheus text format.

    Returns {'counters': {name: {'value': float, 'labels': dict}},
             'gauges':   {name: {'value': float, 'labels': dict}},
             'histograms': {name: {'buckets': [(le, count)...], 'sum': float, 'count': float}}}
    """
    out = {"counters": {}, "gauges": {}, "histograms": {}}
    types = {}
    for line in text.splitlines():
        if not line or line[0] == "#":
            if line.startswith("# TYPE "):
                parts = line.split()
                if len(parts) == 4:
                    types[parts[2]] = parts[3]
            continue
        mm = _LINE.match(line)
        if not mm:
            continue
        name, labpart, raw = mm.group(1), mm.group(2) or "", mm.group(3)
        val = _val(raw)
        labels = {}
        if labpart:
            for lm in _LABEL.finditer(labpart):
                labels[lm.group(1)] = lm.group(2)
        if name.endswith("_bucket"):
            base = name[:-len("_bucket")]
            h = out["histograms"].setdefault(base, _new_hist())
            if labels.get("le") is not None:
                le = _val(labels["le"])
                h["_acc_b"][le] = h["_acc_b"].get(le, 0.0) + val
        elif name.endswith("_sum"):
            b = name[:-len("_sum")]
            h = out["histograms"].setdefault(b, _new_hist())
            h["sum"] = (h["sum"] or 0.0) + val
        elif name.endswith("_count"):
            b = name[:-len("_count")]
            h = out["histograms"].setdefault(b, _new_hist())
            h["count"] = (h["count"] or 0.0) + val
        elif name.endswith("_created"):
            continue
        elif types.get(name) == "counter":
            slot = out["counters"].setdefault(name, {"value": None, "labels": {}})
            if slot["value"] is None:  # first label-set wins (single-engine box)
                slot["value"], slot["labels"] = val, labels
            # per-label sets: append if new, else overwrite (monotonic counter
            # value is replaced in place)
            ser = slot.setdefault("series", [])
            hit = None
            for s in ser:
                if s["labels"] == labels:
                    hit = s
                    break
            if hit is None:
                ser.append({"value": val, "labels": labels})
            else:
                hit["value"] = val
        elif types.get(name) == "histogram":
            # bare histogram line without _bucket suffix — nothing to record
            continue
        else:
            cur = out["gauges"].get(name)
            if cur is None:
                out["gauges"][name] = {"value": val, "labels": labels,
                                       "series": [{"value": val, "labels": labels}]}
            else:
                cur["value"] += val  # sum over label sets (back-compat total)
                ser = cur.setdefault("series", [])
                hit = None
                for s in ser:
                    if s["labels"] == labels:
                        hit = s
                        break
                if hit is None:
                    ser.append({"value": val, "labels": labels})
                else:
                    hit["value"] = val
    for h in out["histograms"].values():
        h["buckets"] = sorted(h.pop("_acc_b").items())
    return out


def sum_all(p, kind, name):
    """Sum a multi-label counter/gauge series over all label sets (the
    sum(...) equivalent). None when the series is absent."""
    v = (p or {}).get(kind, {}).get(name)
    if not v:
        return None
    total = 0.0
    seen = False
    for s in v.get("series", []):
        total += s["value"]
        seen = True
    return total if seen else None


def value_by_label(p, kind, name, labels=None):
    """Value of one label-set of a series (exact labels dict). None if absent.
    For series that are stored summed (unlabeled) this is just the value."""
    v = (p or {}).get(kind, {}).get(name)
    if not v:
        return None
    if not labels:
        return v.get("value")
    for s in v.get("series", []):
        if s["labels"] == labels:
            return s["value"]
    return None


def all_labels(p, kind, name):
    """Every (labels, value) pair of a series, including the unlabeled form."""
    v = (p or {}).get(kind, {}).get(name)
    if not v:
        return []
    out = []
    for s in v.get("series", []):
        out.append((s["labels"], s["value"]))
    return out


def percentile(buckets, p):
    """Percentile over cumulative histogram buckets [(le, count)...], p in (0, 100].

    Linear interpolation within the containing band — same convention as
    Prometheus histogram_quantile. Returns None when the total count is 0 or
    the band is +Inf (no finite bound).
    """
    if not buckets:
        return None
    total = buckets[-1][1]
    if total <= 0:
        return None
    rank = total * (p / 100.0)
    prev_le, prev_c = 0.0, 0.0
    for le, c in buckets:
        if c >= rank:
            if le == float("inf"):
                return None
            frac = (rank - prev_c) / max(c - prev_c, 1e-9)
            return prev_le + (le - prev_le) * frac
        prev_le, prev_c = le, c
    return None
