"""TensorFold lane identity: NAME not SHA on the ledger.

Why: TOKENS BY MODEL rendered a bare commit SHA ("deadbeef...") for the
TensorFold lane. TensorFold-native takes the model path as a POSITIONAL arg
(no --model flag) and keeps the display name in --name. The poll loop only
fills st["model_cmdline"] from --model/-m/--model-path, so _model_identity
fell through to basename(config model path) -- and the config path is an HF
hub snapshot dir, whose leaf is the commit SHA.

Fix: _model_identity uses _live_backend's model_cmdline (which has the
--name fallback), and _model_version resolves the tensorfold positional
path, returning the snapshots/<rev> leaf as the VERSION. The SHA belongs in
version (it is the artifact), never in the model name.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401

import dashboard

REV = "a" * 40  # any 40-hex snapshot rev; the value is opaque to the test
SNAP = "/opt/models/hf/hub/models--acme--BigModel-FP4/snapshots/" + REV
ARGS = ["tensorfold-native", "serve", SNAP, "--name", "BigModel-FP4",
        "--host", "0.0.0.0", "--port", "8888", "--context", "262144",
        "--parallel", "4", "--kv-cache", "fp8", "--vision"]
PROC = {"pid": 1, "args": ARGS, "base": "tensorfold-native"}
TF_SAMPLE = (0, {"counters": {"tensorfold:prompt_tokens_total": {}}})


class TestTensorfoldIdentity(unittest.TestCase):

    def _st(self):
        return {"has_metrics": True, "samples": [TF_SAMPLE],
                "model": SNAP, "model_live": None, "model_cmdline": None}

    def test_name_not_sha(self):
        orig = dashboard._engine_proc
        dashboard._engine_proc = lambda port: PROC
        try:
            ident = dashboard._model_identity(8888, self._st())
        finally:
            dashboard._engine_proc = orig
        self.assertEqual(ident["model"], "BigModel-FP4")
        self.assertNotIn(REV[:8], ident["model"],
                         "commit SHA must never be the model name")
        self.assertEqual(ident["engine"], "tensorfold")
        self.assertEqual(ident["version"], REV,
                         "the snapshot rev is the version")

    def test_new_snapshot_forks_key(self):
        """Swapping the artifact (new snapshot rev) must start a new row."""
        orig = dashboard._engine_proc
        try:
            dashboard._engine_proc = lambda port: PROC
            a = dashboard._model_identity(8888, self._st())
            args2 = list(ARGS)
            args2[2] = SNAP.replace(REV, "0" * 40)
            dashboard._engine_proc = lambda port: {
                "pid": 2, "args": args2, "base": "tensorfold-native"}
            b = dashboard._model_identity(8888, self._st())
        finally:
            dashboard._engine_proc = orig
        self.assertEqual(a["model"], b["model"])
        self.assertNotEqual(a["key"], b["key"],
                            "a new snapshot rev is a new version -> new row")

    def test_model_short_hf_snapshot(self):
        """Engines-page short name: repo dir, not the snapshot SHA leaf."""
        self.assertEqual(dashboard._model_short(SNAP), "BigModel-FP4")
        # plain paths keep the basename behaviour
        self.assertEqual(dashboard._model_short("/opt/models/gguf/X-Q4_K_M.gguf"),
                         "X-Q4_K_M.gguf")
        self.assertIsNone(dashboard._model_short(None))
        self.assertIsNone(dashboard._model_short(""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
