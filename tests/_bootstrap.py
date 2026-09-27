"""Shared test bootstrap: keep tests out of the production log and DB.

Importing `dashboard` gives a test the REAL module, so any code path it
exercises that calls dlog() writes to the live dashboard.log sitting next
to the running service. Simulated failures then show up in the operator's
"errors since restart" check and make the service look broken when it is
not. This has already happened twice during the 2026-09-27 audit.

Every test module should `import _bootstrap` BEFORE `import dashboard`.
That rebinds dashboard.LOG_PATH and dashboard.DB_PATH to per-process temp
files, so a test can log and write rows as loudly as it likes without
touching production state.

Deliberately stdlib-only and dependency-free: the dashboard is stdlib-only
and these tests must stay runnable anywhere.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import dashboard as D  # noqa: E402

# Re-export under its own name so `from _bootstrap import dashboard` works
# for suites that use dashboard.foo() rather than an alias. __all__ alone is
# not enough: it only affects `from _bootstrap import *`.
dashboard = D

# Per-process sink, so parallel or repeated runs never collide and nothing
# accumulates in the real log.
_SANDBOX = tempfile.mkdtemp(prefix="gx10-dash-test-")
D.LOG_PATH = os.path.join(_SANDBOX, "dashboard.log")
D.DB_PATH = os.path.join(_SANDBOX, "metrics.db")

__all__ = ["D", "ROOT", "_SANDBOX", "dashboard"]
