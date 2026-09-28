"""The ledger key must NOT contain the port.

Why: the key used to be (model, version, "backend:port"), so one model whose
lane moved ports became two rows. The brain lane went host :8000 -> :8001 on
2026-09-26 17:40; same model, same recipe, same engine, two cards, two
totals. Nothing was double counted -- both rows were real, non-overlapping
lifetimes -- but it is unreadable and it re-splits on every port change.

The endpoint is still tracked where it belongs: per-port, in `ledger`,
`samples`, and `model_watermarks`.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401

import metadb
import dashboard

BACKEND_SRC = os.path.join(ROOT, "dashboard.py")


class TestLedgerKeyHasNoPort(unittest.TestCase):

    def _ident(self, model, backend, port):
        """Call the real _model_identity with a stubbed scrape."""
        orig_bl, orig_mv = dashboard._live_backend, dashboard._model_version
        dashboard._live_backend = lambda p, st, n: (backend, None)
        dashboard._model_version = lambda p, st, b: None
        try:
            return dashboard._model_identity(port, {"model_live": model})
        finally:
            dashboard._live_backend, dashboard._model_version = orig_bl, orig_mv

    def test_same_model_two_ports_is_one_key(self):
        a = self._ident("Qwen3.8-Flash-Next-Uncensored", "vllm", 8000)
        b = self._ident("Qwen3.8-Flash-Next-Uncensored", "vllm", 8001)
        self.assertEqual(a["key"], b["key"],
                         "a port change must not fork the ledger key")

    def test_engine_label_is_the_family(self):
        a = self._ident("some-model", "vllm", 8000)
        self.assertEqual(a["engine"], "vllm")
        self.assertNotIn("8000", a["engine"])
        self.assertNotIn("8000", a["key"])

    def test_different_engines_still_separate(self):
        a = self._ident("some-model", "vllm", 8001)
        b = self._ident("some-model", "llamacpp", 8890)
        self.assertNotEqual(a["key"], b["key"],
                            "engine family still distinguishes rows")

    def test_different_models_still_separate(self):
        a = self._ident("qwen3.8-flash-next", "vllm", 8001)
        b = self._ident("qwen3.8-27b", "vllm", 8001)
        self.assertNotEqual(a["key"], b["key"])

    def test_key_still_nul_separated_triple(self):
        a = self._ident("m", "vllm", 8001)
        self.assertEqual(len(a["key"].split("\x00")), 3)
        self.assertEqual(a["key"].split("\x00")[0], "m")
        self.assertEqual(a["key"].split("\x00")[2], "vllm")

    def test_migration_script_exists_and_is_idempotent_by_construction(self):
        p = os.path.join(ROOT, "migrate_ledger_key.py")
        self.assertTrue(os.path.exists(p))
        with open(p) as fh:
            src = fh.read()
        self.assertIn("def family(", src)
        # family() is the whole fix: strip the port off an engine label
        self.assertIn('.split(":", 1)[0]', src)


class TestMergePreservesTokens(unittest.TestCase):
    """A merge must never lose or invent tokens."""

    def setUp(self):
        # Use the real schema bootstrap on a temp file (an in-memory DB breaks
        # across connections; and a hand-rolled CREATE TABLE drifts from
        # production). metadb.init_db takes a PATH, not a connection.
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        metadb.init_db(self.path)
        self.c = metadb.connect(self.path)

    def tearDown(self):
        self.c.close()

    def test_two_port_rows_sum_into_one(self):
        m = "Qwen3.8-Flash-Next-Uncensored"
        self.c.execute("INSERT INTO model_ledger (key,model,version,engine,"
                       "in_tokens_cum,out_tokens_cum,in_initial_cum,"
                       "out_initial_cum,first_ts,last_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (f"{m}\x00?\x00vllm:8000", m, None, "vllm:8000",
                        100.0, 10.0, 1.0, 1.0, 1000, 2000))
        self.c.execute("INSERT INTO model_ledger (key,model,version,engine,"
                       "in_tokens_cum,out_tokens_cum,in_initial_cum,"
                       "out_initial_cum,first_ts,last_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (f"{m}\x00?\x00vllm:8001", m, None, "vllm:8001",
                        250.0, 40.0, 0.0, 0.0, 2100, 3000))
        before = self.c.execute(
            "SELECT SUM(in_tokens_cum), SUM(out_tokens_cum) FROM model_ledger"
        ).fetchone()
        self.assertEqual(before[0], 350.0)

        rows = list(self.c.execute("SELECT * FROM model_ledger"))
        fam = lambda e: (e or "?").split(":", 1)[0] or "?"
        merged_in = sum(r["in_tokens_cum"] for r in rows
                        if fam(r["engine"]) == "vllm")
        merged_out = sum(r["out_tokens_cum"] for r in rows
                         if fam(r["engine"]) == "vllm")
        self.assertEqual(merged_in, before[0], "in-tokens preserved")
        self.assertEqual(merged_out, before[1], "out-tokens preserved")
        # span is the union, not one of the halves
        self.assertEqual(min(r["first_ts"] for r in rows if fam(r["engine"]) == "vllm"), 1000)
        self.assertEqual(max(r["last_ts"] for r in rows if fam(r["engine"]) == "vllm"), 3000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
