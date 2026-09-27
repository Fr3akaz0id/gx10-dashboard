"""The DB writer must actually fire, and on wall time.

Two defects this suite exists to prevent:

1. CADENCE DRIFT. The flush used to be gated on a poll count
   (`_poll_count % DB_FLUSH_EVERY`), so the real period was
   POLL_S * DB_FLUSH_EVERY PLUS the cost of every collect(). Measured
   32.2s against a nominal 30s target, and the differenced rate series
   undercounted by that margin. The gate is now wall time.

2. SILENT WRITER STALL. Adding the time gate without adding
   `_last_sample_flush` to collect()'s `global` statement made the name
   local, so every collect() raised UnboundLocalError. A bare
   `except Exception: pass` swallowed it, the service stayed "active",
   the log stayed clean, and NO SAMPLES WERE WRITTEN AT ALL — which from
   the UI is indistinguishable from a lane that is simply idle. It was
   only caught by noticing the newest sample was 191s old.

So these tests assert the flush gate fires on schedule AND that collect()
can mutate the gate without NameError/UnboundLocalError.

Run: python3 tests/test_sample_cadence.py
"""
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import _bootstrap  # noqa: F401  (must precede dashboard: isolates log + DB)
from _bootstrap import D


class TestFlushGateIsWallTime(unittest.TestCase):
    def test_cadence_constant_is_defined(self):
        self.assertTrue(hasattr(D, "SAMPLE_FLUSH_S"),
                        "SAMPLE_FLUSH_S missing: the flush gate needs a "
                        "named target, not an inline literal")
        self.assertEqual(D.SAMPLE_FLUSH_S, 30.0)

    def test_gate_state_is_initialised(self):
        """A module-level _last_sample_flush must exist, or the first
        collect() raises NameError before the first sample is ever written."""
        self.assertTrue(hasattr(D, "_last_sample_flush"),
                        "_last_sample_flush must be initialised at module "
                        "level so the first collect() can subtract from it")

    def test_collect_declares_the_gate_global(self):
        """The exact bug: assigning _last_sample_flush inside collect()
        without `global` makes it local, so the read raises
        UnboundLocalError and the writer never runs."""
        with open(os.path.join(ROOT, "dashboard.py")) as fh:
            src = fh.read()
        start = src.index("def collect():")
        # the global statement sits in the first few lines of the body
        head = src[start:start + 400]
        self.assertIn("global", head,
                      "collect() has no `global` statement in its first "
                      "lines")
        gl = head[head.index("global"):]
        gl = gl[:gl.index("\n")]
        self.assertIn("_last_sample_flush", gl,
                      "collect() assigns _last_sample_flush but does not "
                      "declare it global — every poll raises "
                      "UnboundLocalError and NO samples are written")


class TestCollectActuallyWrites(unittest.TestCase):
    def test_collect_runs_without_nameerror(self):
        """Belt and braces: run the real collect() with a stubbed writer and
        assert the gate advanced. An exception here is the stall."""
        calls = []
        D._db_maybe_write = lambda g: calls.append(g)
        D._last_sample_flush = 0.0
        try:
            D.collect()
        except (NameError, UnboundLocalError) as e:
            self.fail("collect() raised %s: %s" % (type(e).__name__, e))
        finally:
            # restore the real writer
            del D._db_maybe_write
        self.assertGreaterEqual(D._last_sample_flush, time.time() - 5,
                                "flush gate did not advance")
        self.assertTrue(calls, "writer was not called even though the gate "
                               "was long overdue")

    def test_gate_respects_the_interval(self):
        """With a recent flush recorded, collect() must NOT write."""
        calls = []
        D._db_maybe_write = lambda g: calls.append(g)
        D._last_sample_flush = time.time()   # just flushed
        try:
            D.collect()
        finally:
            del D._db_maybe_write
        self.assertEqual(calls, [],
                         "wrote a sample immediately after a flush; the "
                         "interval gate is not being honoured")


if __name__ == "__main__":
    unittest.main(verbosity=2)
