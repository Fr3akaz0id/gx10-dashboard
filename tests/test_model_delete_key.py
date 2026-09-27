"""Model-card delete must survive the HTML round-trip.

THE DEFECT THIS PINS: the ledger key is a NUL-separated triple
("model\\x00version\\x00engine"). NUL cannot survive an HTML attribute --
the browser decodes it to U+FFFD REPLACEMENT CHARACTER. reset_model
deletes with `DELETE ... WHERE key=?`, so the mangled key matched zero
rows, the handler still replied {"ok": true}, and the UI showed
"removed" while the card stayed on screen. The user reported it as "I
hit the x but the card stays".

Fix is metadb._norm_key(): map U+FFFD back to NUL before comparing, and
have the endpoint report an error when no row matched instead of
claiming success.

Run:  python3 -B tests/test_model_delete_key.py
"""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  -- redirects LOG_PATH/DB_PATH to a temp dir
import metadb

NUL = "\x00"
FFFD = "�"

TRIPLE = "m" + NUL + "?" + NUL + "vllm:1"   # the real key in the DB
MANGLED = "m" + FFFD + "?" + FFFD + "vllm:1"  # what the DOM hands back


class TestModelDeleteKey(unittest.TestCase):

    def setUp(self):
        import tempfile
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(self.db) and os.unlink(self.db))
        metadb.init_db(self.db)
        self.rc = metadb.connect(self.db)
        self.add_model(TRIPLE)
        self.add_model("qwen3.8-27b")            # NUL-free control
        self.add_model("a" + FFFD + "b")          # genuine U+FFFD, not a NUL

    def tearDown(self):
        self.rc.close()

    def add_model(self, key, engine="vllm:1"):
        self.rc.execute(
            "INSERT OR REPLACE INTO model_ledger (key, model, version, engine)"
            " VALUES (?,?,?,?)", (key, key.split(NUL)[0], None, engine))
        self.rc.commit()

    def count(self, key):
        return self.rc.execute("SELECT COUNT(*) FROM model_ledger WHERE key=?",
                               (key,)).fetchone()[0]

    # ── the regression itself ──────────────────────────────────
    def test_mangled_key_would_match_nothing(self):
        """Guards the premise: without normalisation the delete is a no-op.

        If this ever fails it means the key format changed, so _norm_key is
        probably no longer the right fix and the whole approach needs
        revisiting.
        """
        self.assertEqual(self.count(MANGLED), 0,
                         "mangled key unexpectedly matches a row")
        self.assertEqual(self.count(TRIPLE), 1)

    def test_reset_model_deletes_through_the_mangled_key(self):
        metadb.reset_model(self.rc, MANGLED, {})
        self.assertEqual(self.count(TRIPLE), 0,
                         "reset_model did not delete the NUL-separated key")
        self.assertEqual(self.count(MANGLED), 0)

    def test_normaliser_round_trips(self):
        self.assertEqual(metadb._norm_key(MANGLED), TRIPLE)

    def test_normaliser_is_idempotent(self):
        self.assertEqual(metadb._norm_key(metadb._norm_key(MANGLED)), TRIPLE)

    def test_normaliser_leaves_clean_keys_alone(self):
        for k in ("qwen3.8-27b", "llama:8890", "", "a/b:c"):
            self.assertEqual(metadb._norm_key(k), k)

    def test_normaliser_passes_through_non_strings(self):
        self.assertIsNone(metadb._norm_key(None))
        self.assertEqual(metadb._norm_key(42), 42)

    # ── it must not over-delete ────────────────────────────────
    def test_delete_only_touches_the_named_model(self):
        metadb.reset_model(self.rc, MANGLED, {})
        self.assertEqual(self.count("qwen3.8-27b"), 1,
                         "an unrelated model was deleted")
        self.assertEqual(self.count("a" + FFFD + "b"), 1,
                         "a key that merely LOOKS mangled was deleted")

    def test_nul_free_key_still_deletable(self):
        metadb.reset_model(self.rc, "qwen3.8-27b", {})
        self.assertEqual(self.count("qwen3.8-27b"), 0)

    # ── the endpoint must not lie ──────────────────────────────
    def test_precheck_counts_through_the_normaliser(self):
        """The handler's no-row-matched guard has to normalise too, or it
        reports an error for a delete that actually succeeded."""
        n = self.rc.execute("SELECT COUNT(*) FROM model_ledger WHERE key=?",
                            (metadb._norm_key(MANGLED),)).fetchone()[0]
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
