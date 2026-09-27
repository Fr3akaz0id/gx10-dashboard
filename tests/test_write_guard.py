"""Writes are loopback-only AND must carry the local token; reads stay open.

DESIGN
------
The dashboard listens on 0.0.0.0 so any host on the LAN can view it. Every
state-changing route is refused unless BOTH hold:

  1. the request comes from loopback          -> blocks a machine on the LAN
  2. X-Dashboard-Token matches                -> blocks cross-site requests

Both are needed and neither is sufficient alone. The subtle one is (2): a
browser can be made to POST to 127.0.0.1 by any page you visit, and the
connection genuinely IS from loopback, so an address check alone waves it
through. Requiring a custom header makes the request non-simple, so the
browser sends a CORS preflight first; with an empty allowed-origin set the
preflight fails and the request is never delivered.

The token is a fixed constant, not a secret. Its job is to be unpredictable
TO A REMOTE WEB PAGE. It is served in the page JS because the browser has to
send it -- and anyone who can read the page can already view everything,
which is the intent.

Run:  python3 -B tests/test_write_guard.py
"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import _bootstrap  # noqa: F401  redirects LOG_PATH / DB_PATH

import dashboard as D

HTML = os.path.join(ROOT, "metrics.html")
PAGES = ("metrics.html", "engines.html", "settings.html", "setup.html")
TOKEN = "gx10-local-write"


class TestLoopbackDetection(unittest.TestCase):

    def test_loopback_addresses_accepted(self):
        for a in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            self.assertTrue(D._is_loopback(a), "rejected own address %r" % a)

    def test_remote_addresses_rejected(self):
        for a in ("172.16.16.148", "10.0.0.5", "192.168.1.20", "0.0.0.0",
                  "::ffff:172.16.16.148", "", "localhost"):
            self.assertFalse(D._is_loopback(a), "accepted remote %r" % a)

    def test_non_string_does_not_raise(self):
        self.assertFalse(D._is_loopback(None))


class TestGuardIsWired(unittest.TestCase):

    def setUp(self):
        self.src = open(os.path.join(ROOT, "dashboard.py")).read()

    def test_every_post_is_guarded(self):
        """The check must be the FIRST thing in do_POST, before the body is
        read or any route is matched. If it sat lower, an unauthenticated
        caller could still reach some routes."""
        i = self.src.index("    def do_POST")
        j = self.src.index("\nif __name__", i)
        post = self.src[i:j]
        head = post[:1400]
        self.assertIn("_is_loopback(self.client_address[0])", head)
        self.assertIn("X-Dashboard-Token", head)
        # and before the body read / first route match
        self.assertLess(head.index("_is_loopback"), head.index("_read_body"))

    def test_guard_precedes_every_route(self):
        i = self.src.index("    def do_POST")
        j = self.src.index("\nif __name__", i)
        post = self.src[i:j]
        guard_at = post.index("_is_loopback")
        first_route = post.index('path == "')
        self.assertLess(guard_at, first_route,
                        "a route is reachable before the guard runs")

    def test_get_is_not_guarded(self):
        """Reads must stay open to the LAN -- that is the whole point."""
        i = self.src.index("    def do_GET")
        j = self.src.index("\n    def do_POST", i)
        get = self.src[i:j]
        self.assertNotIn("_is_loopback", get)
        self.assertNotIn("X-Dashboard-Token", get)

    def test_host_still_binds_all_interfaces(self):
        self.assertIn('HOST = "0.0.0.0"', self.src,
                      "the LAN-view requirement regressed")


class TestFrontendSendsToken(unittest.TestCase):

    def test_every_post_carries_the_token(self):
        for page in PAGES:
            p = os.path.join(ROOT, page)
            if not os.path.exists(p):
                continue
            h = open(p).read()
            posts = list(re.finditer(r"method\s*:\s*['\"]POST['\"]", h))
            self.assertTrue(posts, "%s has no POST at all" % page)
            for m in posts:
                start = h.rfind("fetch(", 0, m.start())
                seg = h[start:m.start() + 260]
                self.assertIn("X-Dashboard-Token", seg,
                              "%s: a POST does not send the token, so the "
                              "UI write would 403" % page)

    def test_token_value_matches_the_server(self):
        for page in PAGES:
            p = os.path.join(ROOT, page)
            if not os.path.exists(p):
                continue
            h = open(p).read()
            if "X-Dashboard-Token" in h:
                self.assertIn(TOKEN, h,
                              "%s sends a different token than the server "
                              "expects" % page)

    def test_no_form_posts_that_bypass_fetch(self):
        """A plain <form method=post> would not carry the header."""
        for page in PAGES:
            p = os.path.join(ROOT, page)
            if not os.path.exists(p):
                continue
            h = open(p).read()
            self.assertNotRegex(h, r"<form[^>]*method\s*=\s*['\"]?post",
                                "%s posts via a form, bypassing the guard" % page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
