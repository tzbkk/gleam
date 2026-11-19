"""Transport domain tests: SSRF safety, robots semantics, redirect
re-validation, limits, extractor dual-environment labels, and outcome
classification. Fully offline: DNS and connections are patched at module
boundaries, trafilatura availability is simulated both via attribute
mocks and via sys.modules manipulation."""

import importlib
import ipaddress
import socket
import sys
import unittest
from unittest import mock

from gleam.core import transport
from gleam.core.transport import extract as extract_mod
from gleam.core.transport import safety
from gleam.core.transport.net import DEFAULT_POLICY, Fetcher
from gleam.core.transport.extract import extract_text
from gleam.core.transport.outcomes import (ABSTAIN, ABSTAIN_KINDS, HTTP_ERROR,
                                           OK, OUTCOMES, TRANSPORT_ERROR,
                                           outcome_for)

PUBLIC_IP = "93.184.216.34"


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
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (records[host], 0))]
    return getaddrinfo


class FakeResponse:
    def __init__(self, status, headers=None, body=b"", exc=None):
        self.status = status
        self._headers = headers or {}
        self._body = body
        self._pos = 0
        self._exc = exc  # raised after the body is drained (mid-stream cut)

    def getheaders(self):
        return list(self._headers.items())

    def read(self, n=-1):
        if self._pos >= len(self._body):
            if self._exc:
                raise self._exc
            return b""
        if n and n > 0:
            chunk = self._body[self._pos:self._pos + n]
        else:
            chunk = self._body[self._pos:]
        self._pos += len(chunk)
        return chunk


class FakeConnection:
    instances = []

    def __init__(self, scheme, host, port, pinned_ip, timeout, route):
        self.scheme = scheme
        self.host = host
        self.port = port
        self.pinned_ip = pinned_ip
        self.timeout = timeout
        self.route = route
        self.sock = None
        self.requests = []
        FakeConnection.instances.append(self)

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path))

    def getresponse(self):
        method, path = self.requests[-1]
        result = self.route(self.host, path)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        pass


def patch_env(testcase, dns_records, route):
    conn_factory = lambda scheme, host, port, pinned_ip, timeout: FakeConnection(
        scheme, host, port, pinned_ip, timeout, route)
    patchers = [
        mock.patch("gleam.core.transport.safety.socket.getaddrinfo",
                   fake_dns(dns_records)),
        mock.patch("gleam.core.transport.net._open_connection", conn_factory),
    ]
    for p in patchers:
        p.start()
        testcase.addCleanup(p.stop)
    FakeConnection.instances.clear()


def make_fetcher(**policy):
    base = {"min_interval_s": 0, "timeout_s": 5.0, "max_bytes": 1024,
            "max_requests": 10, "max_redirects": 3}
    base.update(policy)
    return Fetcher(policy=base, clock=lambda: 0.0, sleep=lambda s: None)


# ---------------------------------------------------------------- safety

class CheckHostTest(unittest.TestCase):
    """Offline SSRF verdicts on IP literals (no resolver involved)."""

    def test_rejects_loopback_internal_linklocal(self):
        for host in ("127.0.0.1", "::1", "169.254.1.1", "10.0.0.1"):
            r = safety.check_host(host)
            self.assertFalse(r.allowed, host)
            self.assertEqual(r.error_kind, "unsafe-destination", host)
            self.assertTrue(r.reason, host)

    def test_rejects_private_reserved_v4(self):
        for host in ("192.168.1.1", "172.16.9.9", "0.0.0.1", "100.64.0.1"):
            self.assertFalse(safety.check_host(host).allowed, host)

    def test_rejects_private_v6_and_ipv4_mapped(self):
        for host in ("fc00::1", "fe80::1", "::ffff:10.0.0.1",
                     "::ffff:127.0.0.1", "64:ff9b::7f00:1", "2002:a00:1::1"):
            r = safety.check_host(host)
            self.assertFalse(r.allowed, host)

    def test_accepts_public_literal_with_pinned_address(self):
        r = safety.check_host(PUBLIC_IP)
        self.assertTrue(r.allowed)
        self.assertEqual(r.address, PUBLIC_IP)
        self.assertIsNone(r.error_kind)

    def test_unresolvable_hostname_is_dns_error(self):
        with mock.patch("gleam.core.transport.safety.socket.getaddrinfo",
                        side_effect=OSError("no address")):
            r = safety.check_host("gone.example")
        self.assertFalse(r.allowed)
        self.assertEqual(r.error_kind, "dns-error")

    def test_ip_literals_skip_the_resolver_entirely(self):
        # even with a broken resolver, literal verdicts stay offline-safe
        with mock.patch("gleam.core.transport.safety.socket.getaddrinfo",
                        side_effect=AssertionError("resolver touched")):
            self.assertFalse(safety.check_host("127.0.0.1").allowed)
            self.assertTrue(safety.check_host(PUBLIC_IP).allowed)

    def test_resolver_order_puts_ipv4_before_ipv6(self):
        # The pinned-IP model dials exactly one address, so a host with
        # both A and AAAA records must resolve to the IPv4 even when the
        # resolver lists IPv6 first.
        infos = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "",
             ("2001:4860:4860::8888", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "",
             ("2001:4860:4860::8888", 0)),
        ]
        with mock.patch("gleam.core.transport.safety.socket.getaddrinfo",
                        return_value=infos):
            verdict = safety.check_host("dual.example")
        self.assertTrue(verdict.allowed, verdict.reason)
        self.assertEqual(verdict.address, PUBLIC_IP)


# ------------------------------------------------------------- fetching

class URLValidationTest(unittest.TestCase):
    def setUp(self):
        self.fetcher = make_fetcher()

    def test_rejects_non_http_schemes(self):
        for url in ("ftp://example.com/file", "file:///etc/passwd",
                    "gopher://example.com/1"):
            out = self.fetcher.fetch(url)
            self.assertFalse(out.ok, url)
            self.assertEqual(out.error["kind"], "unsafe-destination", url)

    def test_rejects_credentials_in_url(self):
        out = self.fetcher.fetch("https://user:secret@example.com/")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "unsafe-destination")

    def test_malformed_ipv6_and_bad_ports_are_fetch_errors(self):
        for bad in ("https://[::1/x/", "http://[fe80::1]/", "https://[/",
                    "https://example.com:70000/", "https://example.com:abc/",
                    "https://example.com:0/", "https://example.com:-1/"):
            out = self.fetcher.fetch(bad)
            self.assertFalse(out.ok, bad)
            self.assertEqual(out.error["kind"], "unsafe-destination", bad)

    def test_one_bad_endpoint_does_not_break_the_round(self):
        patch_env(self, {"ok.example": PUBLIC_IP},
                  lambda host, path: FakeResponse(404))
        fetcher = make_fetcher()
        bad = fetcher.fetch("https://example.com:99999/")
        self.assertFalse(bad.ok)
        good = fetcher.fetch("https://ok.example/page")
        self.assertTrue(good.ok)

    def test_uppercase_scheme_normalized(self):
        patch_env(self, {"ok.example": PUBLIC_IP},
                  lambda host, path: FakeResponse(
                      404) if path == "/robots.txt"
                  else FakeResponse(200, body=b"p"))
        out = make_fetcher().fetch("HTTPS://ok.example/page")
        self.assertTrue(out.ok, out.error)
        conn = FakeConnection.instances[-1]
        self.assertEqual(conn.scheme, "https")
        self.assertEqual(conn.port, 443)
        self.assertEqual(safety.origin_key("HTTPS://ok.example/page"),
                         "https://ok.example:443")


class SSRFFetchTest(unittest.TestCase):
    def fetch_with_ip(self, ip):
        patch_env(self, {"internal.example": ip},
                  lambda host, path: FakeResponse(200, body=b"ok"))
        return make_fetcher().fetch("https://internal.example/")

    def test_resolved_private_addresses_rejected(self):
        for ip in ("127.0.0.1", "::1", "169.254.1.1", "10.0.0.1",
                   "192.168.1.1", "fc00::1", "fe80::1", "::ffff:10.0.0.5"):
            out = self.fetch_with_ip(ip)
            self.assertFalse(out.ok, ip)
            self.assertEqual(out.error["kind"], "unsafe-destination", ip)

    def test_transition_wrapped_public_v4_unwrapped_and_pinned(self):
        for ip in ("64:ff9b::5db8:d822",   # NAT64 wrapping 93.184.216.34
                   "2002:5db8:d822::1"):   # 6to4 wrapping 93.184.216.34
            out = self.fetch_with_ip(ip)
            self.assertTrue(out.ok, (ip, out.error))
            self.assertEqual(FakeConnection.instances[-1].pinned_ip,
                             PUBLIC_IP, ip)

    def test_public_ip_accepted_pinned_with_original_hostname(self):
        patch_env(self, {"ok.example": PUBLIC_IP},
                  lambda host, path: FakeResponse(200, body=b"hello"))
        out = make_fetcher().fetch("https://ok.example/page")
        self.assertTrue(out.ok, out.error)
        conn = FakeConnection.instances[-1]
        self.assertEqual(conn.pinned_ip, PUBLIC_IP)
        self.assertEqual(conn.host, "ok.example",
                         "TLS SNI/Host header must keep the original hostname")


class RobotsTest(unittest.TestCase):
    def _route(self, mapping, default=None):
        return lambda host, path: mapping.get(
            path, default if default is not None else FakeResponse(200,
                                                                   body=b"x"))

    def test_disallow_abstains(self):
        patch_env(self, {"site.example": PUBLIC_IP},
                  self._route({"/robots.txt": FakeResponse(
                      200, body=b"User-agent: *\nDisallow: /")}))
        out = make_fetcher().fetch("https://site.example/page")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "robots-disallow")
        self.assertEqual(out.robots["result"], "disallow")

    def test_401_403_5xx_are_conservative_unknown_abstain(self):
        for status in (401, 403, 500, 503):
            patch_env(self, {"site.example": PUBLIC_IP},
                      self._route({"/robots.txt": FakeResponse(status)}))
            out = make_fetcher().fetch("https://site.example/page")
            self.assertFalse(out.ok, status)
            self.assertEqual(out.error["kind"], "robots-unknown", status)
            self.assertEqual(out.robots["result"], "unknown", status)

    def test_404_and_410_mean_unrestricted(self):
        for status in (404, 410):
            patch_env(self, {"site.example": PUBLIC_IP},
                      self._route({"/robots.txt": FakeResponse(status)},
                                  default=FakeResponse(200, body=b"page")))
            out = make_fetcher().fetch("https://site.example/page")
            self.assertTrue(out.ok, (status, out.error))
            self.assertEqual(out.robots["result"], "allowed", status)

    def test_robots_host_failure_propagates_as_transport(self):
        def route(host, path):
            if path == "/robots.txt":
                return OSError("refused")
            return FakeResponse(200, body=b"page")
        patch_env(self, {"site.example": PUBLIC_IP}, route)
        out = make_fetcher().fetch("https://site.example/page")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "connection-error")

    def test_truncated_robots_body_is_unknown(self):
        body = b"User-agent: *\n" + b"# padding line\n" * 22000
        self.assertGreater(len(body), 262144)

        def route(host, path):
            if path == "/robots.txt":
                return FakeResponse(200, body=body)
            return FakeResponse(200, body=b"page")
        patch_env(self, {"site.example": PUBLIC_IP}, route)
        out = make_fetcher().fetch("https://site.example/page")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "robots-unknown")

    def test_robots_rules_cached_per_origin(self):
        calls = {"robots": 0}

        def route(host, path):
            if path == "/robots.txt":
                calls["robots"] += 1
                return FakeResponse(200, body=b"User-agent: *\nAllow: /")
            return FakeResponse(200, body=b"p")
        patch_env(self, {"site.example": PUBLIC_IP}, route)
        fetcher = make_fetcher()
        self.assertTrue(fetcher.fetch("https://site.example/a").ok)
        second = fetcher.fetch("https://site.example/b")
        self.assertTrue(second.ok, second.error)
        self.assertEqual(second.robots["result"], "allowed")
        self.assertTrue(second.robots["cached"])
        self.assertEqual(calls["robots"], 1)

    def test_transient_robots_unknown_not_cached_sticky(self):
        robots_calls = []

        def route(host, path):
            if path == "/robots.txt":
                robots_calls.append(1)
                if len(robots_calls) == 1:
                    return TimeoutError("robots fetch timed out")
                return FakeResponse(404)
            return FakeResponse(200, body=b"page")
        patch_env(self, {"site.example": PUBLIC_IP}, route)
        fetcher = make_fetcher()
        first = fetcher.fetch("https://site.example/page")
        self.assertFalse(first.ok)
        self.assertEqual(first.error["kind"], "robots-unknown")
        second = fetcher.fetch("https://site.example/page")
        self.assertTrue(second.ok, second.error)
        self.assertEqual(len(robots_calls), 2,
                         "transient robots unknown must be retried")

    def test_budget_exhausted_mid_robots_propagates(self):
        patch_env(self, {"site.example": PUBLIC_IP},
                  lambda host, path: FakeResponse(
                      302, headers={"Location": "/loop"}))
        fetcher = make_fetcher(max_requests=2)
        out = fetcher.fetch("https://site.example/page")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "budget-exhausted")

    def test_exempt_host_skips_robots_and_is_marked_exempt(self):
        # a robots.txt that disallows everything still lets an
        # exempt hostname through -- no robots pre-fetch at all -- and
        # the outcome carries the explicit exempt marker; the same
        # server on a non-listed control host abstains as today.
        hits = []

        def route(host, path):
            hits.append((host, path))
            if path == "/robots.txt":
                return FakeResponse(
                    200, body=b"User-agent: *\nDisallow: /")
            return FakeResponse(200, body=b"page")

        patch_env(self, {"exempt.example": PUBLIC_IP,
                         "control.example": "93.184.216.35"}, route)
        base = {"min_interval_s": 0, "timeout_s": 5.0, "max_bytes": 1024,
                "max_requests": 10, "max_redirects": 3}
        fetcher = Fetcher(policy=base,
                          exempt_hosts=frozenset({"Exempt.Example"}),
                          clock=lambda: 0.0, sleep=lambda s: None)
        self.assertEqual(fetcher.exempt_hosts, frozenset({"exempt.example"}))
        out = fetcher.fetch("https://exempt.example/private")
        self.assertTrue(out.ok, out.error)
        self.assertEqual(out.robots["result"], "exempt")
        self.assertIs(out.robots["exempt"], True)
        self.assertNotIn(("exempt.example", "/robots.txt"), hits)
        self.assertIn(("exempt.example", "/private"), hits)
        self.assertEqual(fetcher.policy["robots_exempt"],
                         ["exempt.example"])

        control = make_fetcher()
        control_out = control.fetch("https://control.example/private")
        self.assertFalse(control_out.ok)
        self.assertEqual(control_out.error["kind"], "robots-disallow")
        self.assertEqual(control_out.robots["result"], "disallow")
        self.assertEqual(control.policy["robots_exempt"], [])


class RedirectTest(unittest.TestCase):
    def test_valid_redirect_followed_with_chain(self):
        route_map = {
            "/robots.txt": FakeResponse(404),
            "/old": FakeResponse(301, headers={"Location": "/new"}),
            "/new": FakeResponse(200, body=b"new page"),
        }
        patch_env(self, {"site.example": PUBLIC_IP},
                  lambda host, path: route_map[path])
        out = make_fetcher().fetch("https://site.example/old")
        self.assertTrue(out.ok, out.error)
        self.assertEqual(out.final_url, "https://site.example/new")
        self.assertEqual(out.redirect_chain[0]["status"], 301)

    def test_redirect_to_private_blocked(self):
        route_map = {
            "/robots.txt": FakeResponse(404),
            "/start": FakeResponse(302,
                                   headers={"Location": "http://10.0.0.9/x"}),
        }
        patch_env(self, {"site.example": PUBLIC_IP},
                  lambda host, path: route_map.get(
                      path, FakeResponse(200, body=b"page")))
        out = make_fetcher().fetch("https://site.example/start")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "unsafe-destination")
        self.assertTrue(out.error["message"], "rejection must carry a reason")

    def test_redirect_to_bad_scheme_blocked(self):
        route_map = {
            "/robots.txt": FakeResponse(404),
            "/start": FakeResponse(301, headers={"Location": "gopher://x/1"}),
        }
        patch_env(self, {"site.example": PUBLIC_IP},
                  lambda host, path: route_map.get(
                      path, FakeResponse(200, body=b"page")))
        out = make_fetcher().fetch("https://site.example/start")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "unsafe-destination")

    def test_redirect_cap(self):
        def always_redirect(host, path):
            if path == "/robots.txt":
                return FakeResponse(404)
            return FakeResponse(302, headers={"Location": "/loop"})
        patch_env(self, {"site.example": PUBLIC_IP}, always_redirect)
        out = make_fetcher(max_redirects=2).fetch("https://site.example/loop")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "redirect-cap")

    def test_same_host_path_change_rechecked_against_robots(self):
        route_map = {
            "/robots.txt": FakeResponse(
                200, body=b"User-agent: *\nDisallow: /private\n"),
            "/start": FakeResponse(302, headers={"Location": "/private"}),
        }
        hits = []

        def route(host, path):
            hits.append(path)
            return route_map.get(path, FakeResponse(200, body=b"x"))
        patch_env(self, {"site.example": PUBLIC_IP}, route)
        out = make_fetcher().fetch("https://site.example/start")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "robots-disallow")
        self.assertNotIn("/private", hits,
                         "disallowed target must never be GET-ed")

    def test_cross_host_destination_robots_checked(self):
        hits = []

        def route(host, path):
            hits.append((host, path))
            if path == "/robots.txt":
                if host == "start.example":
                    return FakeResponse(404)
                return FakeResponse(
                    200, body=b"User-agent: *\nDisallow: /private\n")
            if (host, path) == ("start.example", "/go"):
                return FakeResponse(
                    301, headers={"Location": "http://other.example/private"})
            return FakeResponse(200, body=b"never")
        patch_env(self, {"start.example": PUBLIC_IP,
                         "other.example": "93.184.216.35"}, route)
        out = make_fetcher().fetch("https://start.example/go")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "robots-disallow")
        self.assertIn(("other.example", "/robots.txt"), hits)
        self.assertNotIn(("other.example", "/private"), hits)


class LimitsAndRetentionTest(unittest.TestCase):
    def test_body_limit_enforced(self):
        big = b"x" * 5000
        patch_env(self, {"site.example": PUBLIC_IP},
                  lambda host, path: FakeResponse(
                      404) if path == "/robots.txt"
                  else FakeResponse(200, body=big))
        out = make_fetcher(max_bytes=100).fetch("https://site.example/big")
        self.assertTrue(out.ok)
        self.assertTrue(out.body_truncated)
        self.assertEqual(out.content_bytes, 100)
        self.assertEqual(len(out.body), 100)

    def test_request_budget_exhausted_abstains(self):
        patch_env(self, {"site.example": PUBLIC_IP},
                  lambda host, path: FakeResponse(
                      404) if path == "/robots.txt"
                  else FakeResponse(200, body=b"p"))
        fetcher = make_fetcher(max_requests=1)
        out = fetcher.fetch("https://site.example/page")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "budget-exhausted")

    def test_default_max_requests_is_fifty_two(self):
        # unified request-budget default
        self.assertEqual(DEFAULT_POLICY["max_requests"], 52)

    def test_default_user_agent_carries_package_version(self):
        # the UA identifies the tool and carries the version
        # segment from the single source (installed metadata,
        # pyproject fallback).
        from gleam.entries.version import version_string
        ua = DEFAULT_POLICY["user_agent"]
        self.assertTrue(ua.startswith("gleam/"), ua)
        self.assertIn(version_string(), ua)
        self.assertIn("https://github.com/tzbkk/gleam", ua)
        self.assertIn("mailto:tzbkk@outlook.com", ua)

    def test_http_5xx_is_failed_outcome(self):
        patch_env(self, {"site.example": PUBLIC_IP},
                  lambda host, path: FakeResponse(
                      404) if path == "/robots.txt" else FakeResponse(503))
        out = make_fetcher().fetch("https://site.example/")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "http-5xx")

    def test_mid_body_failure_retains_partial_response(self):
        page = FakeResponse(
            200, headers={"Content-Type": "text/html"},
            body=b"partial-content", exc=OSError("connection reset mid-body"))
        route_map = {
            "/robots.txt": FakeResponse(404),
            "/page": page,
        }
        patch_env(self, {"site.example": PUBLIC_IP},
                  lambda host, path: route_map[path])
        out = make_fetcher().fetch("https://site.example/page")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "connection-error")
        hop = out.hops[-1]
        self.assertEqual(hop.status, 200)
        self.assertEqual(hop.body, b"partial-content")
        self.assertEqual(hop.headers.get("content-type"), ["text/html"])


# ------------------------------------------------------------- outcomes

class OutcomeClassificationTest(unittest.TestCase):
    def _failed(self, kind):
        from gleam.core.transport.net import FetchOutcome
        return FetchOutcome(ok=False, error={"kind": kind, "message": "m"})

    def test_ok_for_2xx(self):
        from gleam.core.transport.net import FetchOutcome
        self.assertEqual(outcome_for(FetchOutcome(ok=True, status=200)), OK)

    def test_http_error_for_4xx(self):
        from gleam.core.transport.net import FetchOutcome
        for status in (400, 403, 404, 410, 499):
            self.assertEqual(
                outcome_for(FetchOutcome(ok=True, status=status)), HTTP_ERROR,
                status)

    def test_transport_error_for_connection_dns_timeout_5xx(self):
        for kind in ("connection-error", "dns-error", "timeout",
                     "transport-error", "http-5xx", "redirect-cap"):
            self.assertEqual(outcome_for(self._failed(kind)),
                             TRANSPORT_ERROR, kind)

    def test_abstain_for_all_five_kinds(self):
        self.assertEqual(
            set(ABSTAIN_KINDS),
            {"robots-disallow", "robots-unknown", "budget-exhausted",
             "unsafe-destination", "offline"})
        for kind in ABSTAIN_KINDS:
            self.assertEqual(outcome_for(self._failed(kind)), ABSTAIN, kind)

    def test_vocabulary_is_exactly_four_values(self):
        self.assertEqual(OUTCOMES,
                         {"OK", "HTTP_ERROR", "TRANSPORT_ERROR", "ABSTAIN"})

    def test_end_to_end_classification_through_fetch(self):
        # robots disallow -> ABSTAIN; 404 page -> HTTP_ERROR; both offline
        route_map = {
            "/robots.txt": FakeResponse(
                200, body=b"User-agent: *\nDisallow: /"),
        }
        patch_env(self, {"blocked.example": PUBLIC_IP},
                  lambda host, path: route_map.get(
                      path, FakeResponse(200, body=b"x")))
        blocked = make_fetcher().fetch("https://blocked.example/page")
        self.assertEqual(outcome_for(blocked), ABSTAIN)

        route_map = {
            "/robots.txt": FakeResponse(404),
            "/gone": FakeResponse(404),
        }
        patch_env(self, {"gone.example": PUBLIC_IP},
                  lambda host, path: route_map[path])
        gone = make_fetcher().fetch("https://gone.example/gone")
        self.assertTrue(gone.ok)
        self.assertEqual(outcome_for(gone), HTTP_ERROR)


# ------------------------------------------------------------- extraction

HTML = b"""<html><head><title>librcd page</title></head><body>
<nav>Menu Home About</nav>
<main><h1>librcd</h1>
<p>librcd is a character conversion library used by RusXMMS.</p>
<p>Author: RusXMMS project team, maintained since 2001.</p>
<a href="/downloads.html">downloads</a>
<a href="https://mirror.example.org/librcd">mirror</a>
<a href="mailto:maintainer@example.org">contact maintainer</a>
<a href="https://mirror.example.org/librcd">mirror again</a>
</main>
<footer>(c) 2001-2020 RusXMMS contributors; all rights reserved</footer>
</body></html>"""


class ProvenanceCaptureTest(unittest.TestCase):
    """Signals captured before boilerplate cleanup, in BOTH environments
    (real trafilatura installed or not)."""

    def test_anchors_mailto_footer_copyright_captured(self):
        rich = extract_text(HTML, "text/html; charset=utf-8",
                            "https://up.example.org/librcd/")
        anchors = [(a["text"], a["url"]) for a in rich.anchors]
        self.assertIn(("downloads", "https://up.example.org/downloads.html"),
                      anchors)
        self.assertIn(("mirror", "https://mirror.example.org/librcd"),
                      anchors)
        self.assertIn("maintainer@example.org", rich.mailtos)
        joined = " ".join(rich.footer_lines)
        self.assertIn("RusXMMS contributors", joined)
        self.assertTrue(any("2001" in line or "(c)" in line.lower()
                            for line in rich.copyright_lines))
        # links deduplicated, order preserved
        self.assertEqual(
            rich.links,
            ["https://up.example.org/downloads.html",
             "https://mirror.example.org/librcd"])

    def test_relative_anchor_resolved_against_base(self):
        rich = extract_text(
            b"<html><body><a href='../deep/x.html'>x</a></body></html>",
            "text/html", "https://a.example/p/q/")
        self.assertEqual(rich.anchors[0]["url"],
                         "https://a.example/p/deep/x.html")

    def test_credential_and_nonweb_anchors_dropped(self):
        rich = extract_text(
            b"<html><body><a href='http://u:p@evil.example/'>e</a>"
            b"<a href='ftp://files.example/x'>f</a></body></html>",
            "text/html", "https://a.example/")
        self.assertEqual(rich.links, [])
        self.assertEqual([a["url"] for a in rich.anchors if a["url"]], [])

    def test_base_url_optional_only_absolute_anchors_survive(self):
        rich = extract_text(
            b"<html><body><a href='/rel'>r</a>"
            b"<a href='https://abs.example/x'>a</a></body></html>",
            "text/html")
        self.assertEqual(rich.links, ["https://abs.example/x"])


class ExtractorLabelingTest(unittest.TestCase):
    """Three-tag dual-environment behavior (offline, no real extraction)."""

    def test_missing_trafilatura_labeled_fallback_stdlib(self):
        with mock.patch.object(extract_mod, "HAVE_TRAFILATURA", False), \
                mock.patch.object(extract_mod, "trafilatura", None):
            rich = extract_text(HTML, "text/html", "https://up.example.org/")
        self.assertEqual(rich.extractor, "fallback-stdlib")
        self.assertTrue(rich.text)
        self.assertIn("maintainer@example.org", rich.mailtos)

    def test_trafilatura_empty_output_labeled_fallback_empty(self):
        stub = mock.Mock(extract_metadata=mock.Mock(return_value=None),
                         extract=mock.Mock(return_value=None))
        with mock.patch.object(extract_mod, "HAVE_TRAFILATURA", True), \
                mock.patch.object(extract_mod, "trafilatura", stub):
            rich = extract_text(
                b"<html><body><p>visible text stays</p></body></html>",
                "text/html", "https://a.example/")
        self.assertEqual(rich.extractor, "fallback-empty-stdlib")
        self.assertIn("visible text stays", rich.text)

    def test_trafilatura_success_labeled_trafilatura(self):
        stub = mock.Mock(
            extract_metadata=mock.Mock(
                return_value=mock.Mock(title="T", author=None)),
            extract=mock.Mock(return_value="# extracted\n\nbody"))
        with mock.patch.object(extract_mod, "HAVE_TRAFILATURA", True), \
                mock.patch.object(extract_mod, "trafilatura", stub):
            rich = extract_text(HTML, "text/html", "https://a.example/")
        self.assertEqual(rich.extractor, "trafilatura")
        self.assertIn("extracted", rich.text)
        self.assertEqual(rich.metadata.get("title"), "T")

    def test_sys_modules_none_yields_fallback_with_body_preserved(self):
        # QA contract: simulating a missing install via sys.modules must
        # flip to the labeled fallback and keep the body text
        with mock.patch.dict(sys.modules, {"trafilatura": None}):
            rich = extract_text(
                b"<html><body><p>Hello upstream</p></body></html>",
                "text/html")
        self.assertIn(rich.extractor,
                      ("fallback-stdlib", "fallback-empty-stdlib"))
        self.assertIn("Hello upstream", rich.text)

    def test_import_time_probe_without_trafilatura(self):
        try:
            with mock.patch.dict(sys.modules, {"trafilatura": None}):
                reloaded = importlib.reload(extract_mod)
                self.assertFalse(reloaded.HAVE_TRAFILATURA)
                self.assertIsNone(reloaded.trafilatura)
        finally:
            importlib.reload(extract_mod)  # restore real install state
        self.assertEqual(extract_mod.HAVE_TRAFILATURA,
                         sys.modules.get("trafilatura") is not None)

    @unittest.skipUnless(extract_mod.HAVE_TRAFILATURA,
                         "trafilatura not installed in this environment")
    def test_real_extraction_labels_trafilatura(self):
        rich = extract_text(HTML, "text/html; charset=utf-8",
                            "https://up.example.org/librcd/")
        self.assertEqual(rich.extractor, "trafilatura")
        self.assertIn("character conversion library", rich.text)
        self.assertTrue(rich.anchors,
                        "provenance anchors survive boilerplate cleanup")


class PassthroughTest(unittest.TestCase):
    def test_json_untouched(self):
        payload = b'{"results": [{"n": 1.5, "s": "caf\\u00e9"}]}'
        rich = extract_text(payload, "application/json", "https://a.example/x")
        self.assertEqual(rich.kind, "json")
        self.assertEqual(rich.text, payload.decode())
        self.assertEqual(rich.extractor, "passthrough")

    def test_plain_text_untouched(self):
        payload = b"plain text line\nsecond line\n"
        rich = extract_text(payload, "text/plain; charset=utf-8",
                            "https://a.example/x")
        self.assertEqual(rich.kind, "text")
        self.assertEqual(rich.text, payload.decode())

    def test_empty_body(self):
        rich = extract_text(b"", None, "https://a.example/")
        self.assertEqual(rich.text, "")
        self.assertEqual(rich.links, [])

    def test_to_dict_roundtrip(self):
        rich = extract_text(HTML, "text/html", "https://up.example.org/")
        d = rich.to_dict()
        self.assertEqual(
            sorted(d),
            ["anchors", "copyright_lines", "extractor", "footer_lines",
             "kind", "links", "mailtos", "metadata", "notes", "text"])
        self.assertEqual(d["text"], rich.text)


class DecodeReadmeTest(unittest.TestCase):
    def test_base64_roundtrip_and_cap(self):
        import base64 as b64
        payload = {"encoding": "base64",
                   "content": b64.b64encode(b"x" * 50).decode()}
        text, mode = extract_mod.decode_readme(payload, cap=10)
        self.assertTrue(text.startswith("xxxxxxxxxx"))
        self.assertIn("truncated at 10", text)
        self.assertEqual(mode, "decoded-from-base64+markdown-text")

    def test_markdown_rendered_to_prose(self):
        import base64 as b64
        readme = ("## Status\n\n> **This is a Maintenance Fork!**\n>\n"
                  "> See the [upstream discussion](https://example.org/x) "
                  "for context.\n")
        payload = {"encoding": "base64",
                   "content": b64.b64encode(readme.encode()).decode()}
        text, mode = extract_mod.decode_readme(payload, cap=2000)
        self.assertIn("This is a Maintenance Fork!", text)
        self.assertIn("upstream discussion", text)
        self.assertNotIn("**", text)
        self.assertNotIn("](https", text)
        self.assertNotIn("##", text)
        self.assertEqual(mode, "decoded-from-base64+markdown-text")

    def test_missing_content(self):
        text, mode = extract_mod.decode_readme({"encoding": "base64"}, 100)
        self.assertEqual(text, "")
        self.assertEqual(mode, "missing-content")


# ------------------------------------------------------- package surface

class PackageSurfaceTest(unittest.TestCase):
    def test_public_names_reachable_from_package(self):
        for name in ("Fetcher", "FetchOutcome", "HopResult", "check_host",
                     "HostCheck", "FetchError", "origin_key",
                     "extract_text", "RichExtraction",
                     "extract_page", "decode_readme", "outcome_for",
                     "DEFAULT_POLICY", "OUTCOMES", "ABSTAIN_KINDS",
                     "OK", "HTTP_ERROR", "TRANSPORT_ERROR", "ABSTAIN"):
            self.assertTrue(hasattr(transport, name), name)

if __name__ == "__main__":
    unittest.main()
