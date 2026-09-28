#!/usr/bin/env python3
"""One-time: fold per-port model_ledger rows into per-engine-family rows.

WHY
    The ledger key used to be (model, version, "backend:port"), so one model
    whose lane moved ports became two rows -- one model, two cards, two
    totals. Nothing was double counted; the defect was identity, not
    arithmetic. _model_identity() no longer puts the port in the key, and this
    brings history in line so pre-migration rows do not linger.

GUARANTEES
    * Every token is preserved: new_cum = SUM(old_cum) over the group.
    * first_ts = MIN, last_ts = MAX, so the lane's real span is preserved.
    * *_initial_cum summed too, so "incl. N unobserved" stays honest.
    * model_watermarks are REPOINTED at the merged key, NOT re-seeded, so the
      live counter is not re-credited on the next flush.
    * Idempotent: a second run is a no-op.
    * Backs up the DB before writing (unless --force).

    The merged row's engine label is the family ('vllm', not 'vllm:8001').
    Ports stay visible per-port in ledger / samples / model_watermarks.

USAGE
    python3 migrate_ledger_key.py                 # dry run: prints the plan, writes nothing
    python3 migrate_ledger_key.py --apply         # performs the merge
    python3 migrate_ledger_key.py --apply --force # ... without taking a backup

SAFE BY DEFAULT. A bare invocation only prints. The first version of this
script was inverted -- it wrote unless --dry-run was passed, so a casual call
mutated the ledger. A migration that rewrites a lifetime meter should never act
without an explicit opt-in.

STOP THE SERVICE FIRST. A running instance still holds the old key-building
code and will re-create the rows you just merged, seconds after you merge
them. That happened once during development and is why this note exists.

Paths come from the environment so the script is not tied to one install:
    GXDASH_DB          database to migrate   (default /opt/gx10-dashboard/metrics.db)
    GXDASH_BACKUP_DIR  where the backup goes (default /tmp)
"""

import os
import shutil
import sqlite3
import sys
import time

DB = os.environ.get("GXDASH_DB", "/opt/gx10-dashboard/metrics.db")
BACKUP_DIR = os.environ.get("GXDASH_BACKUP_DIR", "/tmp")


def family(engine):
    """'vllm:8001' -> 'vllm'. Already-bare engines pass through."""
    return (engine or "?").split(":", 1)[0] or "?"


def main():
    # SAFE BY DEFAULT. This was originally inverted -- it wrote unless
    # --dry-run was passed, so a bare invocation on a production DB mutated
    # it. A migration that touches a ledger nobody can rebuild should refuse
    # to act without an explicit opt-in. --apply (or the legacy --dry-run
    # absence) is what commits; --force additionally skips the backup.
    args = set(sys.argv[1:])
    dry = "--apply" not in args
    if "--force" in args:
        skip_backup = True
    else:
        skip_backup = False
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    bak = os.path.join(BACKUP_DIR, "metrics.db.bak.keymerge.%s" % stamp)

    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row

    rows = list(c.execute(
        "SELECT key, model, version, engine, in_tokens_cum, out_tokens_cum,"
        "       in_initial_cum, out_initial_cum, first_ts, last_ts"
        "  FROM model_ledger"))

    # group by (model, version, family)
    groups = {}
    for r in rows:
        gk = (r["model"], r["version"], family(r["engine"]))
        groups.setdefault(gk, []).append(r)

    multi = {k: v for k, v in groups.items() if len(v) > 1}
    print("rows: %d   groups: %d   groups with >1 row: %d"
          % (len(rows), len(groups), len(multi)))
    for gk, v in sorted(multi.items()):
        print("\n  %s / %s / %s" % gk)
        for r in v:
            print("     %-14s in=%-13s out=%-9s init=%-10s %s..%s"
                  % (r["engine"], f"{r['in_tokens_cum']:,.0f}",
                     f"{r['out_tokens_cum']:,.0f}",
                     f"{r['in_initial_cum']:,.0f}",
                     time.strftime('%m-%d %H:%M', time.localtime(r["first_ts"] or 0)),
                     time.strftime('%m-%d %H:%M', time.localtime(r["last_ts"] or 0))))
        tot_in = sum(r["in_tokens_cum"] for r in v)
        print("     -> merged in=%s out=%s" % (f"{tot_in:,.0f}",
              f"{sum(r['out_tokens_cum'] for r in v):,.0f}"))

    if dry:
        print("\nnothing written (pass --apply to commit, --force to skip"
              " the backup)")
        return 0

    if skip_backup:
        print("\n--force: SKIPPING backup (rows are still merged in one txn)")
    else:
        shutil.copy2(DB, bak)
        print("\nbackup: %s" % bak)

    merged = 0
    for gk, v in groups.items():
        model, version, fam = gk
        newkey = "\x00".join([model, version or "?", fam])
        if len(v) == 1:
            r = v[0]
            if r["key"] == newkey and r["engine"] == fam:
                continue                      # already correct
            # Rewrite the LABEL too, not just the key. The first run of this
            # script only fixed `key`, so four rows kept an engine label like
            # "vllm:8000" while their key said "vllm" -- the UI showed the port
            # and the very thing this migration removes was still on screen.
            c.execute("UPDATE model_ledger SET key=?, engine=? WHERE key=?",
                      (newkey, fam, r["key"]))
            c.execute("UPDATE model_watermarks SET key=?, engine=? WHERE key=?",
                      (newkey, fam, r["key"]))
            continue
        new_in = sum(r["in_tokens_cum"] for r in v)
        new_out = sum(r["out_tokens_cum"] for r in v)
        new_ii = sum(r["in_initial_cum"] for r in v)
        new_io = sum(r["out_initial_cum"] for r in v)
        new_first = min((r["first_ts"] or 0) for r in v)
        new_last = max((r["last_ts"] or 0) for r in v)
        c.execute("DELETE FROM model_ledger WHERE key IN (%s)"
                  % ",".join("?" * len(v)), [r["key"] for r in v])
        c.execute(
            "INSERT OR REPLACE INTO model_ledger (key, model, version, engine,"
            " in_tokens_cum, out_tokens_cum, in_initial_cum, out_initial_cum,"
            " first_ts, last_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (newkey, model, version, fam, new_in, new_out, new_ii, new_io,
             new_first, new_last))
        # Repoint watermarks WITHOUT touching the counter watermarks: the live
        # counter must not be re-credited just because the label changed.
        for r in v:
            c.execute("UPDATE model_watermarks SET key=?, engine=? WHERE key=?",
                      (newkey, fam, r["key"]))
        merged += 1

    c.commit()
    print("merged %d groups" % merged)

    # verify
    tot_before = None
    print("\nresulting rows:")
    for r in c.execute("SELECT key, engine, in_tokens_cum, out_tokens_cum,"
                       " in_initial_cum FROM model_ledger ORDER BY in_tokens_cum DESC"):
        print("  %-46s in=%-13s out=%-9s init=%s"
              % (r["engine"], f"{r['in_tokens_cum']:,.0f}",
                 f"{r['out_tokens_cum']:,.0f}", f"{r['in_initial_cum']:,.0f}"))
    grand = c.execute("SELECT SUM(in_tokens_cum) FROM model_ledger").fetchone()[0]
    print("\ngrand total in = %s" % f"{grand:,.0f}")
    stale = c.execute(
        "SELECT COUNT(*) FROM model_ledger WHERE engine LIKE '%:%'").fetchone()[0]
    print("rows still carrying a port in engine: %d" % stale)
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
