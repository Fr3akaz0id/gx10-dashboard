"""Regression: query_range/query_gpu must cover the FULL span and stay <= limit,
even when the row count exceeds limit (the old code SQL-LIMITed to `limit`
rows first, silently dropping the oldest data and keeping only the recent slice).
"""
import os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import metadb

def main():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        metadb.init_db(path)
        c = metadb.connect(path)
        now = int(time.time())
        # 5000 samples at 30s cadence for one port + a 2nd port with fewer
        for i in range(5000):
            metadb.write_sample(c, {
                "ts": now - i * 30, "port": 8889, "model": "m",
                "in_tokens": 10, "out_tokens": 5, "out_tps": 1.0,
            })
        for i in range(200):
            metadb.write_sample(c, {
                "ts": now - i * 30, "port": 9999, "model": "n",
                "in_tokens": 1, "out_tokens": 1,
            })
        for i in range(6000):
            metadb.write_gpu(c, {"ts": now - i * 30, "sm_clock_mhz": 2000,
                                 "temp_c": 40, "power_w": 12.0, "util_pct": 5})
        c.commit(); c.close()

        r = metadb.connect(path, readonly=True)
        # 7d window (604800s) holds all 5000 (5000*30=150000s=41.6h < 7d)
        rows = metadb.query_range(r, 8889, now - 604800, 1440)
        assert len(rows) <= 1440, "query_range exceeded limit: %d" % len(rows)
        # full span: oldest kept point should be near the window start
        span = rows[-1]["ts"] - rows[0]["ts"]
        assert span > 140000, "span truncated: %ds" % span  # ~39h+ of 41.6h
        # port isolation: 9999 must not leak into 8889's series
        assert all(x["port"] == 8889 for x in rows)
        # chronological
        assert all(rows[i]["ts"] < rows[i+1]["ts"] for i in range(len(rows)-1)), "not chronological"
        g = metadb.query_gpu(r, now - 604800, 1440)
        assert len(g) <= 1440, "query_gpu exceeded limit: %d" % len(g)
        gspan = g[-1]["ts"] - g[0]["ts"]
        assert gspan > 170000, "gpu span truncated: %ds" % gspan  # ~47h of 50h
        # small window: fewer than limit rows -> all returned, no decimation
        small = metadb.query_range(r, 8889, now - 3600, 1440)
        assert len(small) == 121, "small window should return all 121: %d" % len(small)
        # token sums still true over full window (unaffected by decimation)
        t = metadb.query_token_sums(r, 8889, now - 604800)
        assert t == {"in_tokens": 50000, "out_tokens": 25000}, t
        r.close()
        print("OK metadb decimation: 7d->%d pts (span %.1fh), gpu->%d (span %.1fh), small->%d, sums=%s"
              % (len(rows), span/3600, len(g), gspan/3600, len(small), t))
    finally:
        os.unlink(path)

if __name__ == "__main__":
    main()
