"""A port's history window can span a MODEL SWITCH.

`/api/metrics/history` used to report `model = rows[-1]["model"]` -- only the
newest model seen on that port. Harmless while every port serves exactly one
model (true for all six live ports today), wrong the moment a port serves
model A, then model B, and you query it afterwards: A's history gets
labelled B.

That is the same class of misattribution as the pre-portless ledger split,
so it is worth closing rather than documenting.

The response now carries:
  models           [{model, points}, ...] for every model in the window
  model_is_partial True when len(models) > 1
  series.model     per-point model, so the switch point is explicit

`model` is unchanged: still the newest model, for clients that only want a
label.
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest

# Import _bootstrap BEFORE dashboard: it rebinds LOG_PATH/DB_PATH to a
# temp sandbox so this suite cannot write to production state.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401

import dashboard  # noqa: E402
import metadb  # noqa: E402


def _fixture():
    """A scratch DB: port 8500 serves ModelA then ModelB; 8600 serves one."""
    d = tempfile.mkdtemp(prefix="hist-model-")
    db = os.path.join(d, "t.db")
    metadb.init_db(db)
    c = sqlite3.connect(db)
    now = int(time.time())
    for i, off in enumerate(range(-30, -15, 5)):
        c.execute("INSERT INTO samples(port,ts,model,out_tps,in_tps)"
                  " VALUES(?,?,?,?,?)", (8500, now + off * 60, "ModelA",
                                         10.0 + i, 100.0 + i))
    for i, off in enumerate(range(-10, 5, 5)):
        c.execute("INSERT INTO samples(port,ts,model,out_tps,in_tps)"
                  " VALUES(?,?,?,?,?)", (8500, now + off * 60, "ModelB",
                                         50.0 + i, 500.0 + i))
    for off in range(-30, 5, 5):
        c.execute("INSERT INTO samples(port,ts,model,out_tps,in_tps)"
                  " VALUES(?,?,?,?,?)", (8600, now + off * 60, "Solo", 1.0, 1.0))
    c.commit()
    c.close()
    return db


class TestHistoryModelSwitch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = _fixture()
        cls._orig = dashboard.DB_PATH
        dashboard.DB_PATH = cls.db

    @classmethod
    def tearDownClass(cls):
        dashboard.DB_PATH = cls._orig

    def test_switched_port_reports_every_model(self):
        r = dashboard.api_metrics_history(8500, 86400)
        by = {m["model"]: m["points"] for m in r["models"]}
        self.assertEqual(by, {"ModelA": 3, "ModelB": 3},
                         "both models in the window must be reported")

    def test_legacy_model_field_is_unchanged(self):
        """Clients that only want a label must not break."""
        r = dashboard.api_metrics_history(8500, 86400)
        self.assertEqual(r["model"], "ModelB", "still the newest model")

    def test_partial_flag_only_when_actually_partial(self):
        self.assertTrue(dashboard.api_metrics_history(8500, 86400)["model_is_partial"])
        self.assertFalse(dashboard.api_metrics_history(8600, 86400)["model_is_partial"],
                         "a single-model port is never partial")

    def test_per_point_model_exposes_the_switch(self):
        r = dashboard.api_metrics_history(8500, 86400)
        self.assertEqual(r["series"]["model"],
                         ["ModelA"] * 3 + ["ModelB"] * 3,
                         "per-point model must let a client split the window")

    def test_per_point_model_is_aligned_with_ts(self):
        r = dashboard.api_metrics_history(8500, 86400)
        self.assertEqual(len(r["series"]["model"]), len(r["series"]["ts"]))

    def test_empty_window_is_not_partial(self):
        r = dashboard.api_metrics_history(9999, 86400)
        self.assertEqual(r["models"], [])
        self.assertFalse(r["model_is_partial"])
        self.assertIsNone(r["model"])

    def test_source_does_not_regress_to_last_row_only(self):
        """The old bug in one assertion: labelling by the newest row alone."""
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "dashboard.py")) as fh:
            src = fh.read()
        # Structural, not whitespace-exact: an earlier version of this
        # assertion pinned a two-line string including a newline, which is
        # the same brittleness that silently disabled a mutation test in
        # test_spec_and_estimate. Assert on the code, not on formatting.
        import ast as _ast
        fn = next(n for n in _ast.walk(_ast.parse(src))
                  if isinstance(n, _ast.FunctionDef)
                  and n.name == "api_metrics_history")
        fed = [n for n in _ast.walk(fn)
               if isinstance(n, _ast.Subscript)
               and isinstance(n.value, _ast.Name)
               and n.value.id == "models_seen"]
        self.assertTrue(fed, "models_seen must be populated, not just declared")
        self.assertTrue([n for n in _ast.walk(fn) if isinstance(n, _ast.For)],
                        "models_seen must be fed from a loop over rows")
        self.assertIn('"models": model_list', src,
                      "the response must carry the per-model breakdown")


if __name__ == "__main__":
    unittest.main()
