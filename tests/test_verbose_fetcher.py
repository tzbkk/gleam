"""Fetcher --verbose narration (plan verbose-flag §5/§7.6): GET budget
line, robots verdicts through the shared robots_line helper, DNS pin
success/rejection, response bytes/redirect-hop lines, and the
robots.txt pre-fetch silence guard.

Direct Fetcher tests on the transport stub harness (the
test_transport.py pattern): DNS patched at safety.socket.getaddrinfo,
connections patched at net._open_connection -- no network. Narration
is captured through make_verbose(stream=io.StringIO()) with the clock
pinned, so every line is asserted with its full prefix. Budget numbers
are derived from the policy caps each fetcher is built with, never
literals.
"""

import io
import ipaddress
import socket
import time
import unittest
from unittest.mock import patch

from gleam.core.transport.net import Fetcher
from gleam.core.verbose import _quiet, make_verbose

PUBLIC_IP = "93.184.216.34"
FIXED_TIME = time.struct_time(
    (2026, 11, 19, 12, 34, 56, 3, 323, 0))
PREFIX = "[repology 12:34:56] "


def fake_dns(records):
    def getaddrinfo(host, *args, **kwargs):
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            family = socket.AF_INET if addr.version == 4 else socket.AF_INET6
            return [(family, socket.SOCK_STREAM, 6, "", (str(addr), 0))]
        if host not in records:
            raise OSError("no address for", host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 (records[host], 0))]
    return getaddrinfo


class FakeResponse:
    def __init__(self, status, headers=None, body=b""):
        self.status = status
        self._headers = headers or {}
        self._body = body
        self._pos = 0

    def getheaders(self):
        return list(self._headers.items())

    def read(self, n=-1):
        if self._pos >= len(self._body):
            return b""
        if n and n > 0:
            chunk = self._body[self._pos:self._pos + n]
        else:
            chunk = self._body[self._pos:]
        self._pos += len(chunk)
        return chunk


class FakeConnection:
    def __init__(self, scheme, host, port, pinned_ip, timeout, route):
        self.host = host
        self.route = route
        self.sock = None  # _single_request retimes the socket mid-body
        self.requests = []

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path))

    def getresponse(self):
        method, path = self.requests[-1]
        return self.route(self.host, path)

    def close(self):
        pass


def patch_env(testcase, dns_records, route):
    conn_factory = (lambda scheme, host, port, pinned_ip, timeout:
                    FakeConnection(scheme, host, port, pinned_ip, timeout,
                                   route))
    patchers = [
        patch("gleam.core.transport.safety.socket.getaddrinfo",
              fake_dns(dns_records)),
        patch("gleam.core.transport.net._open_connection", conn_factory),
    ]
    for p in patchers:
        p.start()
        testcase.addCleanup(p.stop)


def route_map(routes, default=None):
    fallback = default if default is not None else FakeResponse(
        200, body=b"page")

    def route(host, path):
        return routes.get(path, fallback)
    return route


class FetcherNarrationTests(unittest.TestCase):
    def setUp(self):
        self.sink = io.StringIO()
        clock = patch("gleam.core.verbose.time.gmtime",
                      return_value=FIXED_TIME)
        clock.start()
        self.addCleanup(clock.stop)

    def make_fetcher(self, policy=None, **kwargs):
        base = {"min_interval_s": 0, "timeout_s": 5.0, "max_bytes": 1024,
                "max_requests": 6, "max_redirects": 3}
        base.update(policy or {})
        kwargs.setdefault(
            "verbose", make_verbose("repology", True, stream=self.sink))
        return Fetcher(policy=base, clock=lambda: 0.0,
                       sleep=lambda s: None, **kwargs)

    def messages(self):
        return [line[len(PREFIX):]
                for line in self.sink.getvalue().splitlines()]

    # ------------------------------------------------------- budget line

    def test_exempt_first_request_shows_full_budget(self):
        # pre-reserve read: nothing consumed yet, so remaining-
        # including-this equals the whole cap (the approved 6/6 shape)
        patch_env(self, {"x.example": PUBLIC_IP}, route_map({}))
        fetcher = self.make_fetcher(exempt_hosts=frozenset({"x.example"}))
        cap = fetcher.policy["max_requests"]
        remaining = cap - fetcher.request_count
        out = fetcher.fetch("https://x.example/page")
        self.assertTrue(out.ok)
        self.assertIn(f"GET https://x.example/page "
                      f"(budget {remaining}/{cap})", self.messages())

    def test_budget_after_consumption_counts_reserved_requests(self):
        # second exempt fetch: one request already reserved, so the
        # number is derived from the live count, not the cap alone
        patch_env(self, {"x.example": PUBLIC_IP}, route_map({}))
        fetcher = self.make_fetcher(exempt_hosts=frozenset({"x.example"}))
        cap = fetcher.policy["max_requests"]
        fetcher.fetch("https://x.example/page")
        consumed = fetcher.request_count
        fetcher.fetch("https://x.example/page")
        self.assertIn(f"GET https://x.example/page "
                      f"(budget {cap - consumed}/{cap})", self.messages())

    def test_page_budget_excludes_silent_robots_consumption(self):
        # non-exempt host: the robots pre-fetch reserves one request
        # without narrating, so the page GET is exact at cap-1
        patch_env(self, {"site.example": PUBLIC_IP},
                  route_map({"/robots.txt": FakeResponse(404)}))
        fetcher = self.make_fetcher()
        cap = fetcher.policy["max_requests"]
        out = fetcher.fetch("https://site.example/page")
        self.assertTrue(out.ok)
        self.assertIn(f"GET https://site.example/page "
                      f"(budget {cap - 1}/{cap})", self.messages())

    # --------------------------------------------------- robots wordings

    def test_robots_allow_line(self):
        patch_env(self, {"site.example": PUBLIC_IP},
                  route_map({"/robots.txt": FakeResponse(
                      200, body=b"User-agent: *\nAllow: /\n")}))
        out = self.make_fetcher().fetch("https://site.example/page")
        self.assertTrue(out.ok)
        self.assertIn("robots https://site.example/page: allow",
                      self.messages())

    def test_robots_disallow_line_shows_request_path(self):
        patch_env(self, {"site.example": PUBLIC_IP},
                  route_map({"/robots.txt": FakeResponse(
                      200, body=b"User-agent: *\nDisallow: /private\n")}))
        out = self.make_fetcher().fetch("https://site.example/private/data")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "robots-disallow")
        self.assertIn("robots https://site.example/private/data: "
                      "disallow (path /private/data)", self.messages())
        # the disallowed page request never starts: no GET line at all
        self.assertFalse([m for m in self.messages()
                          if m.startswith("GET ")])

    def test_robots_exempt_operator_file_label(self):
        patch_env(self, {"x.example": PUBLIC_IP}, route_map({}))
        fetcher = self.make_fetcher(exempt_hosts=frozenset({"x.example"}))
        out = fetcher.fetch("https://x.example/page")
        self.assertTrue(out.ok)
        self.assertIn("robots x.example: exempt (operator file)",
                      self.messages())

    def test_robots_exempt_caller_note_label(self):
        patch_env(self, {"x.example": PUBLIC_IP}, route_map({}))
        fetcher = self.make_fetcher(
            exempt_hosts=frozenset({"x.example"}),
            exempt_note="tool-intrinsic, API terms")
        out = fetcher.fetch("https://x.example/page")
        self.assertTrue(out.ok)
        self.assertIn("robots x.example: exempt "
                      "(tool-intrinsic, API terms)", self.messages())

    def test_robots_unknown_status_compresses(self):
        patch_env(self, {"site.example": PUBLIC_IP},
                  route_map({"/robots.txt": FakeResponse(503)}))
        out = self.make_fetcher().fetch("https://site.example/page")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "robots-unknown")
        self.assertIn("robots https://site.example/page: "
                      "unknown (robots 503, abstain)", self.messages())

    def test_robots_unknown_raw_detail_verbatim_fallback(self):
        # truncated robots body: a detail that is not status-shaped,
        # so it rides the line verbatim inside the parens
        patch_env(self, {"site.example": PUBLIC_IP},
                  route_map({"/robots.txt": FakeResponse(
                      200, body=b"User-agent: *\nDisallow: /private\n")}))
        fetcher = self.make_fetcher(policy={"robots_max_bytes": 16})
        out = fetcher.fetch("https://site.example/page")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "robots-unknown")
        self.assertIn("robots https://site.example/page: unknown "
                      "(robots body truncated at robots_max_bytes; "
                      "rules may be incomplete; conservative abstain)",
                      self.messages())

    # -------------------------------------------------------- dns lines

    def test_dns_pin_success_line(self):
        patch_env(self, {"site.example": PUBLIC_IP},
                  route_map({"/robots.txt": FakeResponse(404)}))
        out = self.make_fetcher().fetch("https://site.example/page")
        self.assertTrue(out.ok)
        self.assertIn(f"dns site.example -> {PUBLIC_IP} (pinned)",
                      self.messages())

    def test_dns_rejection_lines_for_both_address_kinds(self):
        # exempt host: no robots pre-fetch would raise first, so the
        # rejection surfaces at the page request inside fetch()'s loop
        for host, records in (("bad.example", {"bad.example": "10.0.0.5"}),
                              ("gone.example", {})):
            with self.subTest(host=host):
                self.sink = io.StringIO()
                patch_env(
                    self, records,
                    route_map({}, default=FakeResponse(
                        200, body=b"unreachable")))
                fetcher = self.make_fetcher(
                    exempt_hosts=frozenset({host}))
                out = fetcher.fetch(f"https://{host}/page")
                self.assertFalse(out.ok)
                self.assertIn(out.error["kind"],
                              ("unsafe-destination", "dns-error"))
                self.assertIn(f"dns {host} rejected: no public address",
                              self.messages())

    # --------------------------------------------- response + hop count

    def test_response_line_without_redirects_has_no_hop_suffix(self):
        patch_env(self, {"site.example": PUBLIC_IP},
                  route_map({"/robots.txt": FakeResponse(404)}))
        out = self.make_fetcher().fetch("https://site.example/page")
        self.assertTrue(out.ok)
        self.assertIn("200 OK, 4 bytes", self.messages())
        self.assertFalse([m for m in self.messages()
                          if "redirect hop" in m])

    def test_single_redirect_hop_suffix_singular(self):
        patch_env(self, {"site.example": PUBLIC_IP},
                  route_map({"/robots.txt": FakeResponse(404),
                             "/old": FakeResponse(301,
                                                  headers={"Location":
                                                           "/new"}),
                             "/new": FakeResponse(200, body=b"new page")}))
        fetcher = self.make_fetcher()
        cap = fetcher.policy["max_requests"]
        out = fetcher.fetch("https://site.example/old")
        self.assertTrue(out.ok)
        self.assertEqual(
            self.messages(),
            ["robots https://site.example/old: allow",
             f"GET https://site.example/old (budget {cap - 1}/{cap})",
             f"dns site.example -> {PUBLIC_IP} (pinned)",
             "301 Moved Permanently, 0 bytes",
             # rules cached: no second robots fetch (else budget cap-2)
             "robots https://site.example/new: allow",
             f"GET https://site.example/new (budget {cap - 2}/{cap})",
             f"dns site.example -> {PUBLIC_IP} (pinned)",
             "200 OK, 8 bytes, 1 redirect hop"])

    def test_two_redirect_hops_suffix_plural(self):
        patch_env(self, {"site.example": PUBLIC_IP},
                  route_map({"/robots.txt": FakeResponse(404),
                             "/a": FakeResponse(301,
                                                headers={"Location": "/b"}),
                             "/b": FakeResponse(302,
                                                headers={"Location": "/c"}),
                             "/c": FakeResponse(200, body=b"x")}))
        out = self.make_fetcher().fetch("https://site.example/a")
        self.assertTrue(out.ok)
        self.assertIn("200 OK, 1 bytes, 2 redirect hops", self.messages())

    # ------------------------------------------------- pre-fetch guard

    def test_robots_pre_fetch_emits_no_get_and_no_dns_line(self):
        # a robots.txt GET really happens (it reserves a request) but
        # narrates nothing; only the page request narrates both lines
        patch_env(self, {"site.example": PUBLIC_IP},
                  route_map({"/robots.txt": FakeResponse(404)}))
        fetcher = self.make_fetcher()
        out = fetcher.fetch("https://site.example/page")
        self.assertTrue(out.ok)
        self.assertEqual(fetcher.request_count, 2)  # robots + page
        cap = fetcher.policy["max_requests"]
        gets = [m for m in self.messages() if m.startswith("GET ")]
        dns = [m for m in self.messages() if m.startswith("dns ")]
        self.assertEqual(gets, [f"GET https://site.example/page "
                                f"(budget {cap - 1}/{cap})"])
        self.assertEqual(dns, [f"dns site.example -> {PUBLIC_IP} "
                               f"(pinned)"])

    # --------------------------------------------------- default quiet

    def test_verbose_unset_is_quiet_and_fetch_still_works(self):
        patch_env(self, {"site.example": PUBLIC_IP},
                  route_map({"/robots.txt": FakeResponse(404)}))
        fetcher = self.make_fetcher(verbose=None)
        self.assertIs(fetcher._verbose, _quiet)
        out = fetcher.fetch("https://site.example/page")
        self.assertTrue(out.ok)

    # --------------------------------------------- emission transcript

    def test_exempt_fetch_transcript_order(self):
        # per-hop emission order: robots verdict -> GET -> dns ->
        # response (dns is knowable only after the request returns)
        patch_env(self, {"x.example": PUBLIC_IP}, route_map({}))
        fetcher = self.make_fetcher(exempt_hosts=frozenset({"x.example"}))
        cap = fetcher.policy["max_requests"]
        out = fetcher.fetch("https://x.example/page")
        self.assertTrue(out.ok)
        self.assertEqual(
            self.messages(),
            ["robots x.example: exempt (operator file)",
             f"GET https://x.example/page (budget {cap}/{cap})",
             f"dns x.example -> {PUBLIC_IP} (pinned)",
             "200 OK, 4 bytes"])


if __name__ == "__main__":
    unittest.main()
