"""Destination safety for untrusted public URLs: URL shape validation and
SSRF protection via public-unicast-only address resolution.

Safety model (docs/research/09 section 4 and 06 section 3):
- http(s) only, no userinfo/credentials in URLs, well-formed IPv6 literals
  and ports; every malformation (including IPv6/port parsing ValueErrors) is
  converted to a single FetchError kind ``unsafe-destination`` so one bad
  endpoint can never abort a whole round with an uncaught exception.
- DNS answers must contain a public unicast address: loopback, private,
  link-local, reserved, multicast and unspecified ranges are rejected,
  including IPv4-mapped IPv6 / 6to4 / NAT64 wrappers which are unwrapped to
  the embedded IPv4 address and re-validated before being accepted.
- IP literals are validated directly without a resolver round-trip, keeping
  ``check_host`` fully offline for literal inputs.
- Callers connect to the pinned address returned here (see net.py) while TLS
  keeps SNI and certificate checks against the original hostname, which
  defeats DNS rebinding between resolution and connection.
- Proxy environment variables are never read: the http.client-based
  transport below does not consult them, and a proxy egress would bypass
  the checks in this module.

This module only ever judges *untrusted target URLs*. Trusted endpoints
(local model base, SearXNG base, GitHub API base) are operator-configured
and live in their own domains; they never route through these checks and
these checks never accept operator-endpoint input.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse
from dataclasses import dataclass

ALLOWED_SCHEMES = ("http", "https")

# RFC 6052 well-known NAT64 prefix: the embedded IPv4 sits in the low 32 bits
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")


class FetchError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class HostCheck:
    """Offline-safe verdict for one hostname or IP literal."""

    host: str
    allowed: bool
    address: str | None = None    # pinned public address when allowed
    error_kind: str | None = None  # FetchError kind when rejected
    reason: str | None = None      # FetchError message when rejected


def _candidate_addresses(host: str) -> list[str]:
    """Raw resolver candidates for host; IP literals skip the resolver."""

    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return [host]
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FetchError("dns-error", f"resolution failed for {host}: {exc}") from exc
    # The pinned-IP model dials exactly one address with no fallback, so
    # prefer IPv4 when the resolver offers both families: a host whose AAAA
    # route is unreachable from the operator network must not shadow a
    # working A record just because the resolver happens to list IPv6
    # first (observed as deterministic fetch timeouts behind blackholed
    # IPv6).
    v4 = [info[4][0] for info in infos if info[0] == socket.AF_INET]
    v6 = [info[4][0] for info in infos if info[0] == socket.AF_INET6]
    return v4 + v6


def _public_address(host: str) -> str:
    """Resolve host and return the first address passing the public-unicast
    checks; raise FetchError when no candidate qualifies."""

    problems = []
    for raw in _candidate_addresses(host):
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            problems.append(f"unparseable {raw}")
            continue
        if isinstance(addr, ipaddress.IPv6Address):
            embedded = (addr.ipv4_mapped            # ::ffff:0:0/96
                        or addr.sixtofour           # 2002::/16 (6to4)
                        or (ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
                            if addr in _NAT64_PREFIX else None))
            if embedded is not None:
                addr = embedded  # unwrap v4 transition forms, then re-validate
        if (addr.is_loopback or addr.is_private or addr.is_link_local
                or addr.is_multicast or addr.is_reserved or addr.is_unspecified
                or not addr.is_global):
            problems.append(f"non-public {raw}")
            continue
        return str(addr)
    raise FetchError("unsafe-destination", "no public address for host "
                     + host + "; rejected: " + "; ".join(problems[:4]))


def check_host(host: str) -> HostCheck:
    """Public-unicast verdict for a hostname or IP literal (offline for
    literals): allowed hosts carry the pinned address to connect to."""

    try:
        return HostCheck(host, True, address=_public_address(host))
    except FetchError as exc:
        return HostCheck(host, False, error_kind=exc.kind, reason=exc.message)


def _validate_url(url: str) -> urllib.parse.ParseResult:
    """Validate URL shape; every malformation (including IPv6/port parsing
    ValueErrors) becomes a FetchError so a single bad endpoint never aborts
    the whole round with an uncaught exception."""

    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname  # malformed IPv6 literals raise ValueError here
        port = parsed.port      # non-numeric/out-of-range ports raise here
    except ValueError as exc:
        raise FetchError("unsafe-destination", f"malformed URL: {exc}") from exc
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise FetchError("unsafe-destination",
                         f"scheme {parsed.scheme!r} not in {ALLOWED_SCHEMES}")
    # Normalize scheme to lowercase once: origin_key / default ports / the
    # connection factory below all compare case-sensitively
    parsed = parsed._replace(scheme=parsed.scheme.lower())
    if parsed.username is not None or parsed.password is not None:
        raise FetchError("unsafe-destination", "credentials in URL are not allowed")
    if not host:
        raise FetchError("unsafe-destination", "URL has no hostname")
    if port is not None and not (1 <= port <= 65535):
        raise FetchError("unsafe-destination",
                         f"port {port!r} out of range 1-65535")
    return parsed


def origin_key(url_or_parsed) -> str | None:
    """Normalized origin key (scheme://host:port); None for malformed input."""

    try:
        parsed = (_validate_url(url_or_parsed)
                  if isinstance(url_or_parsed, str) else url_or_parsed)
    except FetchError:
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return "%s://%s:%d" % (parsed.scheme.lower(), parsed.hostname, port)
