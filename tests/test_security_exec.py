"""Regression tests for the command-execution and traversal fixes.

THE DEFECTS THESE PIN

1. RCE. catalog._docker_inspect_json did
       run(f"docker inspect {name}")     # catalog.run is shell=True
   with `name` taken straight from the URL path
   (/api/engines/docker/<name>). The route regex is [^/]+, which does NOT
   stop shell metacharacters -- "x;touch PWNED" contains no slash and
   passes. Verified live: the injected command ran as this user, whose
   sudo is NOPASSWD ALL. stderr came back inside the JSON error, so it was
   a non-blind channel. The fix is run_argv (shell=False) + _safe_name.

2. Path traversal. save_recipe/load_recipe built paths with
   os.path.join(RECIPE_DIR, f"{rec['name']}.json") and rec['name'] came
   from an unauthenticated POST body. Verified live: one request wrote
   /tmp/HTTP_PWNED.json. Fixed with _safe_name plus a realpath containment
   check, so neither check alone is load-bearing.

3. Env-key injection. engines_write.set_env escaped `key` for the SEARCH
   pattern but wrote it RAW into the unit file, so a key containing a
   newline injected whole new systemd directives.

Run:  python3 -B tests/test_security_exec.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  redirects LOG_PATH / DB_PATH

import catalog
import engines_write

SANDBOX = None


def setUpModule():
    global SANDBOX
    SANDBOX = tempfile.mkdtemp(prefix="secexec-")


def tearDownModule():
    if SANDBOX and os.path.isdir(SANDBOX):
        shutil.rmtree(SANDBOX, ignore_errors=True)


class TestShellInjection(unittest.TestCase):

    def test_docker_inspect_rejects_metacharacters(self):
        for bad in ("x;touch PWNED", "x`id`", "x$(id)", "x|id", "x&&id",
                    "x>out", "../../etc/passwd", "x\nid", ""):
            with self.assertRaises(ValueError, msg="accepted %r" % bad):
                catalog._docker_inspect_json(bad)

    def test_docker_inspect_does_not_execute(self):
        """The real regression: a shell metacharacter in the name must not
        run a command. Uses a relative marker so no '/' is needed -- the
        [^/]+ route regex would pass that through."""
        cwd = os.getcwd()
        os.chdir(SANDBOX)
        try:
            for payload in ("x;touch EXEC_PROOF", "x$(touch EXEC_PROOF2)",
                            "x`touch EXEC_PROOF3`"):
                try:
                    catalog._docker_inspect_json(payload)
                except Exception:
                    pass
            for m in ("EXEC_PROOF", "EXEC_PROOF2", "EXEC_PROOF3"):
                self.assertFalse(os.path.exists(m),
                                 "command injection executed: %s created" % m)
        finally:
            os.chdir(cwd)

    def test_docker_inspect_uses_shell_false(self):
        """Structural guard: the call must go through run_argv, so even a
        name that slipped past validation is one argv entry, not a command
        line for /bin/sh to parse."""
        import inspect
        src = inspect.getsource(catalog._docker_inspect_json)
        self.assertIn("run_argv", src)
        self.assertNotIn('run(f"', src)

    def test_docker_apply_validates_the_recipe_name(self):
        """A recipe whose name carries a metacharacter must be refused
        before it can reach `docker stop`/`docker rm`."""
        with self.assertRaises(ValueError):
            catalog.docker_apply({"name": "x;id"}, confirm_running_loss=False)

    def test_safe_name_accepts_real_names(self):
        for ok in ("vllm-engine", "qwen3.8", "a", "A_b.c-1", "x" * 128):
            self.assertEqual(catalog._safe_name(ok), ok)
        for bad in ("", "-leading", ".dot", "a" * 129, "a/b", "a b", "a;b"):
            with self.assertRaises(ValueError):
                catalog._safe_name(bad)


class TestRecipeTraversal(unittest.TestCase):

    def setUp(self):
        self._orig = catalog.RECIPE_DIR
        self.dir = tempfile.mkdtemp(prefix="recipes-", dir=SANDBOX)
        catalog.RECIPE_DIR = self.dir
        self.addCleanup(self._restore)

    def _restore(self):
        catalog.RECIPE_DIR = self._orig

    def test_save_recipe_refuses_traversal(self):
        for bad in ("../escaped", "../../escaped", "/etc/escaped",
                    "..%2fescaped", "a/../../escaped"):
            with self.assertRaises(ValueError, msg="accepted %r" % bad):
                catalog.save_recipe({"name": bad, "image": "x"})
        # nothing landed outside
        self.assertFalse(os.path.exists(os.path.join(SANDBOX, "escaped.json")))

    def test_load_recipe_returns_none_for_traversal(self):
        # a real file outside the dir, addressed by traversal
        outside = os.path.join(SANDBOX, "outside.json")
        with open(outside, "w") as f:
            json.dump({"secret": "leaked"}, f)
        self.addCleanup(lambda: os.path.exists(outside) and os.unlink(outside))
        rel = os.path.relpath(outside, self.dir)
        self.assertIsNone(catalog.load_recipe(rel))

    def test_roundtrip_still_works(self):
        rec = {"name": "good-name", "image": "vllm", "cmd": ["a", "b"]}
        p = catalog.save_recipe(rec)
        self.assertTrue(os.path.isfile(p))
        self.assertEqual(catalog.load_recipe("good-name"), rec)

    def test_recipe_path_stays_inside_dir(self):
        p = catalog._recipe_path("legit")
        self.assertEqual(os.path.dirname(p), os.path.realpath(self.dir))


class TestEnvKeyInjection(unittest.TestCase):

    UNIT = "[Service]\nExecStart=/usr/bin/vllm\nEnvironment=FOO=old\n"

    def test_newline_in_key_is_rejected(self):
        for bad in ('A\nExecStartPre=/bin/sh -c "id>/tmp/x"',
                    "A\rB", "A B", "A=B", "A\n", "\nExecStartPre=x"):
            with self.assertRaises(ValueError, msg="accepted %r" % bad):
                engines_write.set_env(self.UNIT, bad, "v")

    def test_injected_directive_never_lands(self):
        try:
            out, _ = engines_write.set_env(
                self.UNIT, 'A\nExecStartPre=/bin/sh -c "id>/tmp/x"', "v")
        except ValueError:
            return
        self.fail("injection accepted, produced:\n%s" % out)

    def test_normal_key_still_works(self):
        out, changed = engines_write.set_env(self.UNIT, "FOO", "new")
        self.assertTrue(changed)
        self.assertIn("Environment=FOO=new", out)
        out, changed = engines_write.set_env(self.UNIT, "BAR", "1")
        self.assertIn("Environment=BAR=1", out)

    def test_env_key_validator_shape(self):
        for ok in ("FOO", "_x", "A1_B2", "HF_HOME"):
            self.assertEqual(engines_write._env_key(ok), ok)
        for bad in ("1FOO", "FOO-BAR", "FOO BAR", "FOO\nBAR", "", "FOO.BAR"):
            with self.assertRaises(ValueError):
                engines_write._env_key(bad)


class TestEnvSecretRedaction(unittest.TestCase):

    def test_credential_names_are_redacted(self):
        import re
        pat = catalog._SECRET_KEY_RE
        for k in ("HF_TOKEN", "VLLM_API_KEY", "MY_SECRET", "DB_PASSWORD",
                  "AWS_ACCESS_KEY_ID", "AUTH_HEADER", "PRIVATE_KEY"):
            self.assertTrue(pat.search(k), "would leak: %s" % k)

    def test_ordinary_names_are_not_redacted(self):
        pat = catalog._SECRET_KEY_RE
        for k in ("PATH", "HOME", "VLLM_VERSION", "CUDA_VISIBLE_DEVICES",
                  "NCCL_DEBUG", "MODEL_PATH"):
            self.assertIsNone(pat.search(k), "wrongly redacted: %s" % k)


if __name__ == "__main__":
    unittest.main(verbosity=2)
