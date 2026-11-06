import ipaddress
import socket
import unittest
from unittest import mock

from gleam import net
from gleam.net import Fetcher, FetchError

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
        self._exc = exc  # body 读尽后抛出（模拟中途断流）

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
    testcase.patchers = [
        mock.patch("gleam.net.socket.getaddrinfo", fake_dns(dns_records)),
        mock.patch("gleam.net._open_connection", conn_factory),
    ]
    for p in testcase.patchers:
        p.start()
        testcase.addCleanup(p.stop)
    FakeConnection.instances.clear()


def make_fetcher(**policy):
    base = {"min_interval_s": 0, "timeout_s": 5.0, "max_bytes": 1024,
            "max_requests": 10, "max_redirects": 3}
    base.update(policy)
    return Fetcher(policy=base, clock=lambda: 0.0, sleep=lambda s: None)


class URLValidationTest(unittest.TestCase):
    def setUp(self):
        self.fetcher = make_fetcher()

    def test_rejects_non_http_scheme(self):
        out = self.fetcher.fetch("ftp://example.com/file")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "unsafe-destination")

    def test_rejects_credentials_in_url(self):
        out = self.fetcher.fetch("https://user:secret@example.com/")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "unsafe-destination")

    def test_rejects_file_scheme(self):
        out = self.fetcher.fetch("file:///etc/passwd")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "unsafe-destination")

    def test_malformed_ipv6_is_fetcherror_not_crash(self):
        for bad in ("https://[::1/x/", "http://[fe80::1]/", "https://[/"):
            out = self.fetcher.fetch(bad)
            self.assertFalse(out.ok, bad)
            self.assertEqual(out.error["kind"], "unsafe-destination", bad)

    def test_bad_ports_are_fetcherror_not_crash(self):
        for bad in ("https://example.com:70000/", "https://example.com:abc/",
                    "https://example.com:0/", "https://example.com:-1/"):
            out = self.fetcher.fetch(bad)
            self.assertFalse(out.ok, bad)
            self.assertEqual(out.error["kind"], "unsafe-destination", bad)

    def test_one_bad_endpoint_does_not_abort_fetcher(self):
        patch_env(self, {"ok.example": PUBLIC_IP},
                  lambda host, path: FakeResponse(404))
        fetcher = make_fetcher()
        bad = fetcher.fetch("https://example.com:99999/")
        self.assertFalse(bad.ok)
        good = fetcher.fetch("https://ok.example/page")
        self.assertTrue(good.ok)

    def test_uppercase_scheme_https_gets_https_conn_port_and_origin(self):
        # HTTPS:// 大写 scheme 不得拿到默认 80 端口 + 明文 HTTP 连接
        patch_env(self, {"ok.example": PUBLIC_IP},
                  lambda host, path: FakeResponse(
                      404) if path == "/robots.txt"
                  else FakeResponse(200, body=b"p"))
        out = make_fetcher().fetch("HTTPS://ok.example/page")
        self.assertTrue(out.ok, out.error)
        conn = FakeConnection.instances[-1]
        self.assertEqual(conn.scheme, "https")
        self.assertEqual(conn.port, 443)
        self.assertEqual(net.origin_key("HTTPS://ok.example/page"),
                         "https://ok.example:443")


class SSRFTest(unittest.TestCase):
    def fetch_with_ip(self, ip):
        patch_env(self, {"internal.example": ip},
                  lambda host, path: FakeResponse(200, body=b"ok"))
        return make_fetcher().fetch("https://internal.example/")

    def test_loopback_rejected(self):
        out = self.fetch_with_ip("127.0.0.1")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "unsafe-destination")

    def test_private_v4_rejected(self):
        for ip in ("10.0.0.5", "192.168.1.1", "172.16.9.9", "169.254.1.1",
                   "0.0.0.1"):
            out = self.fetch_with_ip(ip)
            self.assertEqual(out.error["kind"], "unsafe-destination", ip)

    def test_private_v6_rejected(self):
        for ip in ("::1", "fc00::1", "fe80::1", "::ffff:10.0.0.5"):
            out = self.fetch_with_ip(ip)
            self.assertEqual(out.error["kind"], "unsafe-destination", ip)

    def test_nat64_and_6to4_wrapped_public_v4_unwrapped_and_pinned(self):
        for ip in ("64:ff9b::5db8:d822",   # NAT64 内嵌 93.184.216.34
                   "2002:5db8:d822::1"):   # 6to4 内嵌 93.184.216.34
            out = self.fetch_with_ip(ip)
            self.assertTrue(out.ok, (ip, out.error))
            self.assertEqual(FakeConnection.instances[-1].pinned_ip,
                             PUBLIC_IP, ip)

    def test_nat64_and_6to4_wrapped_private_v4_rejected(self):
        for ip in ("64:ff9b::7f00:1",      # NAT64 内嵌 127.0.0.1
                   "2002:a00:1::1"):       # 6to4 内嵌 10.0.0.1
            out = self.fetch_with_ip(ip)
            self.assertFalse(out.ok, ip)
            self.assertEqual(out.error["kind"], "unsafe-destination", ip)

    def test_public_ip_accepted_and_pinned(self):
        patch_env(self, {"ok.example": PUBLIC_IP},
                  lambda host, path: FakeResponse(200, body=b"hello"))
        out = make_fetcher().fetch("https://ok.example/page")
        self.assertTrue(out.ok, out.error)
        conn = FakeConnection.instances[-1]
        self.assertEqual(conn.pinned_ip, PUBLIC_IP)
        self.assertEqual(conn.host, "ok.example",
                         "TLS SNI/Host header must keep original hostname")

    def test_redirect_to_private_blocked(self):
        route_map = {
            "/robots.txt": FakeResponse(404),
            "/start": FakeResponse(302, headers={"Location": "http://10.0.0.9/x"}),
        }
        patch_env(self, {"site.example": PUBLIC_IP},
                  lambda host, path: route_map.get(
                      path, FakeResponse(200, body=b"page")))
        out = make_fetcher().fetch("https://site.example/start")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "unsafe-destination")
        self.assertTrue(out.error["message"],
                        "rejection must carry a reason")

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

    def test_valid_redirect_followed_and_chain_kept(self):
        route_map = {
            "/robots.txt": FakeResponse(404),
            "/old": FakeResponse(301, headers={"Location": "/new"}),
            "/new": FakeResponse(200, body=b"new page"),
        }
        patch_env(self, {"site.example": PUBLIC_IP},
                  lambda host, path: route_map[path])
        out = make_fetcher().fetch("https://site.example/old")
        self.assertTrue(out.ok)
        self.assertEqual(out.final_url, "https://site.example/new")
        self.assertEqual(out.redirect_chain[0]["status"], 301)

    def test_body_limit_enforced(self):
        big = b"x" * 5000
        patch_env(self, {"site.example": PUBLIC_IP},
                  lambda host, path: FakeResponse(
                      404) if path == "/robots.txt" else FakeResponse(200, body=big))
        out = make_fetcher(max_bytes=100).fetch("https://site.example/big")
        self.assertTrue(out.ok)
        self.assertTrue(out.body_truncated)
        self.assertEqual(out.content_bytes, 100)
        self.assertEqual(len(out.body), 100)

    def test_http_5xx_is_transport_error(self):
        patch_env(self, {"site.example": PUBLIC_IP},
                  lambda host, path: FakeResponse(
                      404) if path == "/robots.txt" else FakeResponse(503))
        out = make_fetcher().fetch("https://site.example/")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "http-5xx")

    def test_request_budget_exhausted_abstains(self):
        patch_env(self, {"site.example": PUBLIC_IP},
                  lambda host, path: FakeResponse(
                      404) if path == "/robots.txt" else FakeResponse(200, body=b"p"))
        fetcher = make_fetcher(max_requests=1)
        out = fetcher.fetch("https://site.example/page")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "budget-exhausted")


class RobotsTest(unittest.TestCase):
    def test_disallow_abstains(self):
        route_map = {
            "/robots.txt": FakeResponse(200, body=b"User-agent: *\nDisallow: /"),
        }
        patch_env(self, {"site.example": PUBLIC_IP},
                  lambda host, path: route_map.get(
                      path, FakeResponse(200, body=b"page")))
        out = make_fetcher().fetch("https://site.example/page")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "robots-disallow")
        self.assertEqual(out.robots["result"], "disallow")

    def test_robots_403_is_conservative_unknown(self):
        route_map = {
            "/robots.txt": FakeResponse(403),
            "/page": FakeResponse(200, body=b"page"),
        }
        patch_env(self, {"site.example": PUBLIC_IP},
                  lambda host, path: route_map[path])
        out = make_fetcher().fetch("https://site.example/page")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "robots-unknown")

    def test_robots_connection_refused_propagates_as_transport(self):
        # 主机整体不可达时按传输错误呈现，而不是robots 弃权（页面同样抓不到）
        def route(host, path):
            if path == "/robots.txt":
                return OSError("refused")
            return FakeResponse(200, body=b"page")
        patch_env(self, {"site.example": PUBLIC_IP}, route)
        out = make_fetcher().fetch("https://site.example/page")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "connection-error")

    def test_robots_404_allows_fetch(self):
        route_map = {
            "/robots.txt": FakeResponse(404),
            "/page": FakeResponse(200, body=b"content"),
        }
        patch_env(self, {"site.example": PUBLIC_IP},
                  lambda host, path: route_map[path])
        out = make_fetcher().fetch("https://site.example/page")
        self.assertTrue(out.ok)
        self.assertEqual(out.robots["result"], "allowed")

    def test_robots_redirect_followed(self):
        robots_hits = []

        def route(host, path):
            if path == "/robots.txt":
                robots_hits.append(1)
                if len(robots_hits) == 1:
                    return FakeResponse(301, headers={
                        "Location": "https://site.example/robots.txt"})
                return FakeResponse(200, body=b"User-agent: *\nAllow: /")
            return FakeResponse(200, body=b"content")
        patch_env(self, {"site.example": PUBLIC_IP}, route)
        out = make_fetcher().fetch("https://site.example/page")
        self.assertTrue(out.ok)
        self.assertEqual(out.robots["result"], "allowed")

    def test_robots_redirect_to_private_blocked(self):
        def route(host, path):
            if path == "/robots.txt":
                return FakeResponse(302, headers={
                    "Location": "http://192.168.0.1/robots.txt"})
            return FakeResponse(200, body=b"content")
        patch_env(self, {"site.example": PUBLIC_IP}, route)
        out = make_fetcher().fetch("https://site.example/page")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "unsafe-destination")

    def test_robots_cached_per_host(self):
        calls = {"robots": 0}
        route_map = {
            "/robots.txt": FakeResponse(200, body=b"User-agent: *\nAllow: /"),
            "/a": FakeResponse(200, body=b"a"),
            "/b": FakeResponse(200, body=b"b"),
        }

        def route(host, path):
            if path == "/robots.txt":
                calls["robots"] += 1
            return route_map[path]
        patch_env(self, {"site.example": PUBLIC_IP}, route)
        fetcher = make_fetcher()
        self.assertTrue(fetcher.fetch("https://site.example/a").ok)
        self.assertTrue(fetcher.fetch("https://site.example/b").ok)
        self.assertEqual(calls["robots"], 1)

    def test_dns_error_is_transport_error_not_death(self):
        patch_env(self, {}, lambda host, path: FakeResponse(200))
        fetcher = make_fetcher()
        out = fetcher.fetch("https://gone.example/")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "dns-error")

    def test_truncated_robots_body_is_conservative_unknown(self):
        # robots 正文超过 robots_max_bytes（默认 64KiB）被截断：尾部 Disallow
        # 可能丢失，必须判未知弃权，不得读成"允许"
        body = b"User-agent: *\n" + b"# padding line\n" * 6000
        self.assertGreater(len(body), 65536)

        def route(host, path):
            if path == "/robots.txt":
                return FakeResponse(200, body=body)
            return FakeResponse(200, body=b"page")
        patch_env(self, {"site.example": PUBLIC_IP}, route)
        out = make_fetcher().fetch("https://site.example/page")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "robots-unknown")
        self.assertEqual(out.robots["result"], "unknown")

    def test_budget_exhausted_mid_robots_propagates_not_robots_unknown(self):
        # robots 抓取耗尽请求预算（重定向跳数消耗 max_requests）：
        # 按 budget-exhausted 上抛，不得吞成 robots-unknown
        patch_env(self, {"site.example": PUBLIC_IP},
                  lambda host, path: FakeResponse(
                      302, headers={"Location": "/loop"}))
        fetcher = make_fetcher(max_requests=2)
        out = fetcher.fetch("https://site.example/page")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "budget-exhausted")

    def test_robots_timeout_unknown_not_cached_sticky(self):
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
        self.assertNotIn("https://site.example:443", fetcher._robots_rules)
        second = fetcher.fetch("https://site.example/page")
        self.assertTrue(second.ok, second.error)
        self.assertEqual(len(robots_calls), 2,
                         "transient robots unknown must not be cached sticky")

    def test_robots_redirect_cap_unknown_not_cached_sticky(self):
        requests = []

        def route(host, path):
            requests.append(path)
            return FakeResponse(302, headers={"Location": "/loop"})
        patch_env(self, {"site.example": PUBLIC_IP}, route)
        fetcher = make_fetcher()  # max_redirects=3：每次 robots 判定耗 4 个请求
        first = fetcher.fetch("https://site.example/page")
        self.assertFalse(first.ok)
        self.assertEqual(first.error["kind"], "robots-unknown")
        self.assertNotIn("https://site.example:443", fetcher._robots_rules)
        second = fetcher.fetch("https://site.example/page")
        self.assertFalse(second.ok)
        self.assertEqual(second.error["kind"], "robots-unknown")
        self.assertEqual(len(requests), 8,
                         "redirect-cap unknown must be retried, not sticky")


class FailedFetchRetentionTest(unittest.TestCase):
    def test_mid_body_failure_retains_status_headers_partial_body(self):
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


class RobotsOnRedirectDestinationTest(unittest.TestCase):
    def test_same_host_redirect_to_disallowed_path_abstains(self):
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
        self.assertNotIn("/private", hits, "disallowed target must not be GET-ed")

    def test_cross_host_redirect_destination_robots_checked(self):
        hits = []

        def route(host, path):
            hits.append((host, path))
            if path == "/robots.txt":
                if host == "start.example":
                    return FakeResponse(404)
                return FakeResponse(
                    200, body=b"User-agent: *\nDisallow: /private\n")
            if (host, path) == ("start.example", "/go"):
                return FakeResponse(301,
                                    headers={"Location": "http://other.example/private"})
            return FakeResponse(200, body=b"never")
        patch_env(self, {"start.example": PUBLIC_IP,
                         "other.example": "93.184.216.35"}, route)
        fetcher = make_fetcher()
        out = fetcher.fetch("https://start.example/go")
        self.assertFalse(out.ok)
        self.assertEqual(out.error["kind"], "robots-disallow")
        self.assertIn(("other.example", "/robots.txt"), hits)
        self.assertNotIn(("other.example", "/private"), hits)
        # 目标 origin 的 robots 原文已捕获（落盘名由 observe 层做哈希）
        from gleam.net import origin_key
        self.assertIn(origin_key("http://other.example/private"),
                      fetcher._robots_bodies)

    def test_robots_rules_reused_across_paths_same_origin(self):
        route_map = {
            "/robots.txt": FakeResponse(200,
                                        body=b"User-agent: *\nAllow: /"),
        }
        robots_hits = []

        def route(host, path):
            if path == "/robots.txt":
                robots_hits.append(1)
                return route_map[path]
            return FakeResponse(200, body=b"p")
        patch_env(self, {"site.example": PUBLIC_IP}, route)
        fetcher = make_fetcher()
        self.assertTrue(fetcher.fetch("https://site.example/a").ok)
        # 同 origin 新路径：不重新抓 robots，也不递归 robots 检查
        self.assertTrue(fetcher.fetch("https://site.example/b").ok)
        self.assertEqual(len(robots_hits), 1)
        self.assertIn("https://site.example:443", fetcher._robots_bodies)


if __name__ == "__main__":
    unittest.main()
