"""Bounded read-only HTTP(S) fetching for untrusted public URLs: pinned-IP
connections, per-hop robots.txt checks, redirect re-validation, and limits.

Frozen contract semantics. The default ``max_requests`` is 52 (a
stress-tested value from live 550B-model telemetry; the config domain owns
the operator-facing default and transcribes the same value).

Safety model:
- http(s) only, no userinfo/credentials; every redirect hop re-runs the
  full validation chain (scheme/userinfo/DNS/public-address).
- DNS is resolved up front and validated as public unicast (SSRF guard in
  safety.py); connections then go to the *pinned address* while TLS keeps
  SNI and certificate verification against the original hostname (DNS
  rebinding defense). Proxy environment variables are never consulted.
- Limits: redirect hops, response body bytes, per-request timeout, total
  request count for one Fetcher, and a minimum interval per host.
- robots.txt: fetched under the same policy and cached per origin;
  disallow / failed retrieval (unknown) / 401/403/5xx all lead to ABSTAIN;
  404/410 mean unrestricted. The one approved
  exemption: hostnames in the operator-owned exemption list
  (domain-granular, startup-validated through the config layer) skip the
  robots pre-fetch entirely; their fetch records carry an explicit
  robots "exempt" marker so exempt fetches never look identical to
  robots-allowed ones. Every other safety layer still applies to them.
- This module only fetches *untrusted target URLs*. Trusted endpoints
  (operator-configured model base / SearXNG base / GitHub API base) live
  in their own domains and share no code path with this fetcher; GitHub
  API requests that do go through here get the full public-URL validation.
- Probe errors mean "unknown", never a verdict of death; byte-hash changes
  are representation changes, not liveness conclusions.
"""

from __future__ import annotations

import hashlib
import http.client
import socket
import ssl
import time
import urllib.parse
import urllib.robotparser
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import assert_never

from .safety import FetchError, _public_address, _validate_url, origin_key
from ..user_agent import USER_AGENT
from ..verbose import _quiet, robots_line

REDIRECT_STATUSES = (301, 302, 303, 307, 308)


DEFAULT_POLICY = {
    "max_bytes": 262144,
    "timeout_s": 15.0,
    "max_redirects": 3,
    # unified request-budget default
    "max_requests": 52,
    "min_interval_s": 1.0,
    "robots_max_bytes": 262144,
    # Shared User-Agent; the version segment comes from the single
    # version source.
    "user_agent": USER_AGENT,
}


@dataclass
class HopResult:
    url: str
    status: int | None = None
    pinned_ip: str | None = None
    headers: dict = field(default_factory=dict)
    body: bytes = b""
    body_truncated: bool = False
    error: "FetchError | None" = None  # read failure after a response started


class PinnedHTTPConnection(http.client.HTTPConnection):
    """Connect to the pinned IP; the Host header and virtual hosting keep
    using the original hostname (HTTP layer unchanged)."""

    def __init__(self, host: str, port: int, pinned_ip: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self):
        self.sock = socket.create_connection((self._pinned_ip, self.port),
                                             timeout=self.timeout)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to the pinned IP; TLS SNI and hostname verification still
    target the original hostname (DNS rebinding defense)."""

    def __init__(self, host: str, port: int, pinned_ip: str, timeout: float,
                 context: ssl.SSLContext):
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip

    def connect(self):
        raw = socket.create_connection((self._pinned_ip, self.port),
                                       timeout=self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


_SSL_CONTEXT: ssl.SSLContext | None = None


def _ssl_context() -> ssl.SSLContext:
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        _SSL_CONTEXT = ssl.create_default_context()
    return _SSL_CONTEXT


def _open_connection(scheme: str, host: str, port: int, pinned_ip: str,
                     timeout: float):
    """Connection factory; tests inject fake connections by replacing it."""

    if scheme == "https":
        return PinnedHTTPSConnection(host, port, pinned_ip, timeout,
                                     _ssl_context())
    return PinnedHTTPConnection(host, port, pinned_ip, timeout)


def _response_headers(response) -> dict:
    out = {}
    try:
        for key, value in response.getheaders():
            out.setdefault(key.lower(), []).append(value)
    except Exception:  # some fake response objects have no headers to read
        pass
    return out


def _single_request(url: str, timeout: float, max_bytes: int, user_agent: str,
                    method: str = "GET",
                    extra_headers: dict | None = None) -> HopResult:
    parsed = _validate_url(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    pinned_ip = _public_address(host)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    conn = _open_connection(parsed.scheme, host, port, pinned_ip, timeout)
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
        "Accept-Language": "en",
        "Connection": "close",
    }
    # extra_headers are written only by tool code (e.g. fixed GitHub API
    # version headers); never from model arguments, so the model cannot
    # inject arbitrary headers
    for key, value in (extra_headers or {}).items():
        headers[str(key)] = str(value)
    try:
        conn.request(method, path, headers=headers)
        response = conn.getresponse()
        status = response.status
        headers = _response_headers(response)
        deadline = time.monotonic() + timeout
        chunks, total, truncated = [], 0, False
        body_error: FetchError | None = None
        # Response already started: any read failure from here on keeps the
        # status/headers/partial body obtained so far
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FetchError("timeout",
                                     f"body read deadline exceeded for {url}")
                if conn.sock is not None:
                    conn.sock.settimeout(remaining)
                chunk = response.read(8192)
                if not chunk:
                    break
                if total + len(chunk) > max_bytes:
                    take = max_bytes - total
                    if take:
                        chunks.append(chunk[:take])
                    total = max_bytes
                    truncated = True
                    break
                chunks.append(chunk)
                total += len(chunk)
        except FetchError as exc:
            body_error = exc
        except (socket.timeout, TimeoutError) as exc:
            body_error = FetchError("timeout", f"{type(exc).__name__}: {exc}")
        except OSError as exc:
            body_error = FetchError("connection-error",
                                    f"{type(exc).__name__}: {exc}")
        return HopResult(url=url, status=status, pinned_ip=pinned_ip,
                         headers=headers, body=b"".join(chunks),
                         body_truncated=truncated, error=body_error)
    except FetchError:
        raise
    except (socket.timeout, TimeoutError) as exc:
        raise FetchError("timeout", f"{type(exc).__name__}: {exc}") from exc
    except OSError as exc:
        raise FetchError("connection-error", f"{type(exc).__name__}: {exc}") from exc
    except (http.client.HTTPException, ssl.SSLError, ValueError) as exc:
        raise FetchError("transport-error", f"{type(exc).__name__}: {exc}") from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass


@dataclass
class FetchOutcome:
    ok: bool
    status: int | None = None
    final_url: str | None = None
    redirect_chain: list = field(default_factory=list)
    body: bytes = b""
    body_truncated: bool = False
    content_sha256: str | None = None
    content_bytes: int = 0
    error: dict | None = None
    robots: dict | None = None
    hops: list = field(default_factory=list)  # HopResult per hop (history)

    def __post_init__(self):
        if self.ok and self.body is not None:
            self.content_sha256 = hashlib.sha256(self.body).hexdigest()
            self.content_bytes = len(self.body)


def _response_line(hop: HopResult, redirect_count: int) -> str:
    """One hop's response narration: ``200 OK, 8192 bytes``, plus a
    redirect-hop count suffix once redirects have accumulated."""
    reason = http.client.responses.get(hop.status, "")
    status_text = f"{hop.status} {reason}" if reason else f"{hop.status}"
    line = f"{status_text}, {len(hop.body)} bytes"
    if redirect_count > 0:
        plural = "" if redirect_count == 1 else "s"
        line += f", {redirect_count} redirect hop{plural}"
    return line


class Fetcher:
    """Bounded fetcher with a global request budget and per-origin cached
    robots rules."""

    def __init__(self, policy: dict | None = None, offline: bool = False,
                 clock=time.monotonic, sleep=time.sleep,
                 exempt_hosts: frozenset[str] = frozenset(),
                 verbose: Callable[[str], None] | None = None,
                 exempt_note: str = "operator file"):
        self.policy = dict(DEFAULT_POLICY)
        if policy:
            self.policy.update(policy)
        self.offline = offline
        self.request_count = 0
        self._last_hit: dict[str, float] = {}
        # Cache *parsed rules / absence / unknown* per origin, never path
        # decisions: re-judging a new path on the same origin (including a
        # redirect destination) issues no extra request
        self._robots_rules: dict[str, dict] = {}
        self._robots_bodies: dict[str, bytes] = {}
        self._clock = clock
        self._sleep = sleep
        # Operator exemption: domain-granular, lowercased. The
        # sorted list rides the policy dict so every policy disclosure
        # (observation records, the run close-out) stays auditable even
        # when the set is empty.
        self.exempt_hosts = frozenset(
            str(host).lower() for host in (exempt_hosts or ()))
        self.policy["robots_exempt"] = sorted(self.exempt_hosts)
        # Narration sink (``make_verbose`` callable or _quiet when
        # unset); ``exempt_note`` is the label the exempt robots line
        # carries (repology passes its tool-intrinsic API-terms note)
        self._verbose = verbose or _quiet
        self._exempt_note = exempt_note

    def _reserve_request(self) -> None:
        if self.request_count >= self.policy["max_requests"]:
            raise FetchError("budget-exhausted",
                             f"request cap {self.policy['max_requests']} reached")
        self.request_count += 1

    def _throttle(self, netloc: str) -> None:
        interval = float(self.policy["min_interval_s"])
        if interval <= 0:
            return
        last = self._last_hit.get(netloc)
        now = self._clock()
        if last is not None:
            wait = interval - (now - last)
            if wait > 0:
                self._sleep(wait)
        self._last_hit[netloc] = self._clock()

    def _request_counted(self, url: str, *, max_bytes=None, method="GET",
                         headers: dict | None = None) -> HopResult:
        self._reserve_request()
        parsed = urllib.parse.urlsplit(url)
        netloc = parsed.netloc.lower()
        self._throttle(netloc)
        # Failed hops that already retained a partial response are returned
        # as-is; the caller decides the semantics
        return _single_request(
            url, float(self.policy["timeout_s"]),
            int(max_bytes or self.policy["max_bytes"]),
            self.policy["user_agent"], method=method,
            extra_headers=headers)

    def _fetch_follow(self, url: str, *, max_bytes=None) -> HopResult:
        """Bounded redirect following (for auxiliary resources like
        robots.txt); each hop is budgeted and re-validated independently."""

        current = url
        for _ in range(int(self.policy["max_redirects"]) + 1):
            hop = self._request_counted(current, max_bytes=max_bytes)
            if hop.error is not None:
                raise hop.error
            if hop.status not in REDIRECT_STATUSES:
                return hop
            location = (hop.headers.get("location") or [None])[0]
            if not location:
                raise FetchError("transport-error",
                                 "robots redirect without Location header")
            target = urllib.parse.urljoin(current, location)
            _validate_url(target)  # re-validate scheme/userinfo per hop; the
            current = target       # address check happens inside the request
        raise FetchError("redirect-cap",
                         f"robots fetch exceeded max redirects "
                         f"({self.policy['max_redirects']})")

    def _robots_rules_for(self, parsed) -> dict:
        """Fetch (or reuse) the robots rules for this origin; only the rules
        are cached, never per-path decisions."""

        key = origin_key(parsed)
        robots_url = key + "/robots.txt"
        cached = self._robots_rules.get(key)
        if cached is not None:
            return dict(cached, cached=True)
        rules = {"mode": "unknown", "detail": "not fetched", "url": robots_url}
        try:
            hop = self._fetch_follow(
                robots_url, max_bytes=int(self.policy["robots_max_bytes"]))
        except FetchError as exc:
            if exc.kind in ("dns-error", "connection-error",
                            "unsafe-destination", "budget-exhausted"):
                # Host-level failure (including an unsafe address): the page
                # fetch would fail the same way, so surface the original
                # error instead of mislabeling an unreachable/unsafe host as
                # a robots abstain; budget exhaustion likewise propagates as
                # itself (matching the page-request side), not as unknown
                raise
            rules = {"mode": "unknown",
                     "detail": f"robots fetch failed ({exc.kind}: {exc.message}); "
                               f"conservative abstain",
                     "url": robots_url}
            if exc.kind not in ("timeout", "redirect-cap"):
                # Timeouts and redirect-cap overruns are transient: their
                # unknown verdict is not cached per origin, so the next
                # decision fetches robots again instead of turning a
                # transient unknown into a sticky abstain
                self._robots_rules[key] = rules
            return dict(rules, cached=False)
        status = hop.status or 0
        if hop.error is not None:
            rules = {"mode": "unknown",
                     "detail": f"robots fetch error mid-body (status {status}, "
                               f"{hop.error.kind}); conservative abstain",
                     "url": robots_url}
        elif status in (404, 410):
            rules = {"mode": "absent", "detail": f"robots absent ({status})",
                     "url": robots_url}
        elif status in (401, 403) or status >= 500 or not (200 <= status < 300):
            rules = {"mode": "unknown",
                     "detail": f"robots status {status}; conservative abstain",
                     "url": robots_url}
        else:
            if hop.body_truncated:
                # robots body truncated at robots_max_bytes: trailing
                # Disallow lines may be lost, the rules are incomplete, so
                # abstain as unknown -- never read that as "allowed"
                rules = {"mode": "unknown",
                         "detail": "robots body truncated at robots_max_bytes; "
                                   "rules may be incomplete; conservative abstain",
                         "url": robots_url}
            else:
                text = hop.body.decode("utf-8", "replace")
                self._robots_bodies[key] = hop.body
                parser = urllib.robotparser.RobotFileParser()
                try:
                    parser.parse(text.splitlines())
                except Exception as exc:  # parser failures are unknown
                    rules = {"mode": "unknown",
                             "detail": f"robots parse error: {exc}",
                             "url": robots_url}
                else:
                    rules = {"mode": "rules", "parser": parser,
                             "detail": "robots rules parsed", "url": robots_url}
        self._robots_rules[key] = rules
        return dict(rules, cached=False)

    def _narrate_robots(self, url: str, host: str, decision: dict) -> None:
        """One robots verdict narration line per decision, through the
        shared ``robots_line`` helper (bts narrates the same shape)."""
        match decision["result"]:
            case "allowed":
                self._verbose(robots_line(url, "allow", None))
            case "disallow":
                self._verbose(robots_line(url, "disallow", decision["path"]))
            case "unknown":
                self._verbose(robots_line(url, "unknown", decision["detail"]))
            case "exempt":
                self._verbose(robots_line(host, "exempt", self._exempt_note))
            case unreachable:
                assert_never(unreachable)

    def robots_decision(self, url: str) -> dict:
        """robots verdict for one URL; rules are cached per origin, the
        path is judged fresh every time."""

        parsed = _validate_url(url)
        host = (parsed.hostname or "").lower()
        if host in self.exempt_hosts:
            # No robots.txt fetch, no abstain logic for listed
            # hosts; the explicit "exempt" result is the parallel
            # positive marker next to robots-disallow / robots-unknown,
            # so exempt fetches never look identical to robots-allowed
            # ones in the audit trail.
            decision = {"result": "exempt", "exempt": True,
                        "detail": "host is operator-exempt from robots.txt "
                                  "(robots-exempt.txt); robots "
                                  "pre-fetch skipped",
                        "url": None, "cached": False}
            self._narrate_robots(url, host, decision)
            return decision
        rules = self._robots_rules_for(parsed)
        base = {"detail": rules["detail"], "url": rules.get("url"),
                "cached": bool(rules.get("cached"))}
        if rules["mode"] == "absent":
            decision = dict(base, result="allowed")
        elif rules["mode"] == "unknown":
            decision = dict(base, result="unknown")
        else:
            allowed = rules["parser"].can_fetch("*", url)
            decision = dict(base,
                            result="allowed" if allowed else "disallow",
                            path=parsed.path or "/")
        self._narrate_robots(url, host, decision)
        return decision

    def fetch(self, url: str, *, headers: dict | None = None) -> FetchOutcome:
        """Fetch one URL: every hop (including redirect destinations) gets a
        robots check first, then a bounded request.

        headers only carries fixed headers written by tool code (e.g. the
        GitHub API version header); it does not interact with redirect
        semantics -- every hop is re-validated from scratch.
        """

        try:
            _validate_url(url)
        except FetchError as exc:
            return FetchOutcome(ok=False, error={"kind": exc.kind,
                                                 "message": exc.message})
        chain, hops = [], []
        current = url
        last_robots = None
        try:
            for depth in range(self.policy["max_redirects"] + 1):
                robots = self.robots_decision(current)
                last_robots = robots
                if robots["result"] not in ("allowed", "exempt"):
                    return FetchOutcome(
                        ok=False, final_url=current, redirect_chain=chain,
                        hops=hops, robots=robots,
                        error={"kind": f"robots-{robots['result']}",
                               "message": robots["detail"]})
                host = (urllib.parse.urlsplit(current).hostname or
                        "").lower()
                remaining = self.policy["max_requests"] - self.request_count
                self._verbose(f"GET {current} "
                              f"(budget {remaining}/"
                              f"{self.policy['max_requests']})")
                try:
                    hop = self._request_counted(current, headers=headers)
                except FetchError as exc:
                    # Address pinning failed inside the request (raised
                    # by _public_address before any connection); only
                    # the two address-rejection kinds narrate a dns line
                    if exc.kind in ("unsafe-destination", "dns-error"):
                        self._verbose(
                            f"dns {host} rejected: no public address")
                    raise
                hops.append(hop)
                self._verbose(f"dns {host} -> {hop.pinned_ip} (pinned)")
                self._verbose(_response_line(hop, len(chain)))
                if hop.error is not None:
                    # Failure with a (partial) response already obtained:
                    # keep status/headers/partial body
                    return FetchOutcome(
                        ok=False, status=hop.status, final_url=current,
                        redirect_chain=chain, hops=hops, robots=robots,
                        error={"kind": hop.error.kind,
                               "message": hop.error.message})
                if hop.status in REDIRECT_STATUSES:
                    location = (hop.headers.get("location") or [None])[0]
                    if not location:
                        return FetchOutcome(
                            ok=False, status=hop.status, final_url=current,
                            redirect_chain=chain, hops=hops, robots=robots,
                            error={"kind": "transport-error",
                                   "message": "redirect without Location header"})
                    target = urllib.parse.urljoin(current, location)
                    # Full re-validation on every hop (scheme/userinfo/DNS/
                    # public address); any unsafe hop aborts the fetch
                    _validate_url(target)
                    chain.append({"from": current, "to": target,
                                  "status": hop.status})
                    current = target
                    continue
                if hop.status >= 500:
                    return FetchOutcome(
                        ok=False, status=hop.status, final_url=current,
                        redirect_chain=chain, hops=hops, robots=robots,
                        error={"kind": "http-5xx",
                               "message": f"server error status {hop.status}"})
                # 2xx / 3xx-terminal / 4xx: the HTTP exchange completed (the
                # semantic grading of 4xx happens in the outcomes layer)
                return FetchOutcome(
                    ok=True, status=hop.status, final_url=current,
                    redirect_chain=chain, hops=hops, robots=robots,
                    body=hop.body, body_truncated=hop.body_truncated)
            return FetchOutcome(
                ok=False, status=hops[-1].status if hops else None,
                final_url=current, redirect_chain=chain, hops=hops,
                robots=last_robots,
                error={"kind": "redirect-cap",
                       "message": f"exceeded max redirects "
                                  f"({self.policy['max_redirects']})"})
        except FetchError as exc:
            return FetchOutcome(
                ok=False, status=(hops[-1].status if hops else None),
                final_url=current, redirect_chain=chain, hops=hops,
                error={"kind": exc.kind, "message": exc.message})
