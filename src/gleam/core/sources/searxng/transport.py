"""Trusted-base HTTP transport for the operator-configured SearXNG
endpoint -- deliberately its own bounded path, never the public
fetcher.

Trust model: the base URL comes only from operator configuration (the
config domain owns the value; the caller passes it in). The request
path (``/search``) and query parameters are built by the client, never
accepted from untrusted input. Because the target is the operator's
own local service, the public-probe SSRF/DNS/robots chain does not
apply here -- and none of it is bypassed either: third-party engine
crawling happens inside the SearXNG container under its own policy.

Hard properties of this module:
- redirects are NEVER followed: any 3xx status is returned as-is for
  the caller to grade explicitly (with the ``Location`` header kept);
- a single shared deadline bounds connect + read (the socket timeout
  is tightened to the remaining budget before every read);
- the response body is capped at ``max_bytes`` (a truncated JSON body
  is an honest malformed-json outcome downstream, not a silent one).
"""

from __future__ import annotations

import http.client
import time

__all__ = [
    "request",
]


def request(scheme: str, host: str, port: int, path: str, *,
            timeout_s: float, max_bytes: int,
            user_agent: str) -> tuple[int, dict, bytes]:
    """One bounded GET; returns ``(status, headers, body)`` where
    header keys are lowercased and map to lists of values.

    Raises ``TimeoutError`` when the shared deadline expires and
    propagates ``OSError`` / ``http.client.HTTPException`` for
    connection-level failures -- both are graded by the caller, never
    swallowed here. Redirects are not followed.
    """

    if scheme == "https":
        conn = http.client.HTTPSConnection(host, port, timeout=timeout_s)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout_s)
    deadline = time.monotonic() + timeout_s
    try:
        conn.request("GET", path, headers={
            "User-Agent": user_agent,
            "Accept": "application/json",
            "Connection": "close",
        })
        response = conn.getresponse()
        status = response.status
        headers: dict[str, list[str]] = {}
        for key, value in response.getheaders():
            headers.setdefault(key.lower(), []).append(value)
        chunks, total = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("search response deadline exceeded")
            if conn.sock is not None:
                conn.sock.settimeout(remaining)
            chunk = response.read(8192)
            if not chunk:
                break
            if total + len(chunk) > max_bytes:
                chunks.append(chunk[:max_bytes - total])
                break  # truncated JSON is graded malformed downstream
            chunks.append(chunk)
            total += len(chunk)
        return status, headers, b"".join(chunks)
    finally:
        try:
            conn.close()
        except Exception:
            pass
