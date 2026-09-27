"""Security regression tests: shell-injection + command-exec gating.

Audit 2026-09-27 found unauthenticated RCE on three routes. Every payload here
is a harmless marker file; a test FAILS if the marker appears.

Run: python3 tests/test_security_shell.py
"""
import os
import sys
import glob
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import catalog
import dashboard as D

MARK = os.path.join(tempfile.gettempdir(), "audit_shelltest_marker")
FAILS = []


def _clean():
    for f in glob.glob(MARK + "*"):
        try:
            os.remove(f)
        except OSError:
            pass


def ok(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)


print("1. name validation rejects every shell metacharacter")
for bad in ["x;id", "x>out", "x$(id)", "x`id`", "a|b", "a&b", "a\nid",
            "", "..", "-rf", "a b", "a/b", "a'b", 'a"b', "a*b", "a?b",
            "a;b", "$(curl x)", "${IFS}"]:
    try:
        catalog._safe_name(bad)
        ok(False, f"catalog._safe_name accepted {bad!r}")
    except ValueError:
        ok(True, f"rejected {bad!r}")
ok(catalog._safe_name("qwen38-flash-next") == "qwen38-flash-next",
   "legit container name still accepted")

print("\n2. docker_logs does not execute injected commands")
_clean()
for payload in [f"x>{MARK}", f"x;touch$IFS{MARK}", f"x$(touch$IFS{MARK})",
                f"x`touch$IFS{MARK}`"]:
    try:
        catalog.docker_logs(payload)
    except ValueError:
        pass
    except Exception:
        pass
ok(not os.path.exists(MARK), f"no marker created by docker_logs payloads")
_clean()

print("\n3. engine_logs does not execute injected commands")
try:
    catalog.engine_logs(f"x;touch$IFS{MARK}")
except Exception:
    pass
ok(not os.path.exists(MARK), "no marker created by engine_logs")
_clean()

print("\n4. unit_action refuses units that are not in config.json")
for unit in ["sshd", "cron", "docker", "nginx",
             f"nope.service; touch {MARK}"]:
    try:
        D.unit_action(unit, "start")
        ok(False, f"unit_action accepted {unit!r}")
    except ValueError:
        ok(True, f"refused {unit!r}")
ok(not os.path.exists(MARK), "no marker created via unit_action")
_clean()

print("\n5. unit_action refuses a valid-but-unconfigured unit name")
try:
    D.unit_action("definitely-not-a-real-unit-xyz", "start")
    ok(False, "accepted an unconfigured unit")
except ValueError:
    ok(True, "refused unconfigured unit")

print("\n6. docker_action refuses injected container names")
for name in [f"x; touch {MARK}", f"x>{MARK}", "$(id)"]:
    try:
        D.docker_action(name, "stop")
        ok(False, f"docker_action accepted {name!r}")
    except ValueError:
        ok(True, f"refused {name!r}")
ok(not os.path.exists(MARK), "no marker created via docker_action")
_clean()

print("\n7. run_argv really is shell=False (metachars are inert data)")
r = D.run_argv(["echo", f"a; touch {MARK}"])
ok(os.path.exists(MARK) is False, "no marker via run_argv")
ok("a;" in (r.stdout or ""), "metachars survive as literal text")
_clean()

print("\n8. CORS is same-origin only (no wildcard)")
ok(not hasattr(D, "_CORS_ALLOWED_ORIGINS") or
   D._CORS_ALLOWED_ORIGINS == set(),
   "wildcard origins not configured")

print()
if FAILS:
    print(f"FAILED {len(FAILS)} check(s):")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("OK - all shell-injection guards hold")
