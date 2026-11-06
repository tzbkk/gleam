"""Typed BTS client: the bounded trusted-path fetcher and the auto
fallback orchestration.

Public surface (consumed by the bts entry shell and the investigate
fetch_bug tool):

- :class:`BtsClient` -- ``BtsClient().fetch(bug_id, source="auto")``
  returning a typed :class:`BtsResult`;
- :class:`BtsPolicy` -- numeric bounds (live values arrive from
  the config domain at call time);
- :class:`BtsResult` -- ``to_data()`` yields exactly the
  ``bts.data`` field set; ``truncated``/``more_available`` carry the
  truncation facts for the entry to map to ``partial``.

Frozen semantics:

- ``auto`` (default) tries the documented machine interface
  (Debbugs/SOAP ``get_bug_log``) first. The mbox fallback is attempted
  ONLY when the SOAP attempt fails with an eligibility error
  (``transport-error`` / ``timeout`` / ``soap-fault`` / ``parse-error``,
  the eligible set) AND robots explicitly permits the mbox endpoint for
  ``User-agent: *``. ``rate-limited`` / ``not-found`` /
  ``budget-exhausted`` and every non-eligible kind never fall back, and
  an unknown or denying robots verdict never falls back. There is NO
  bypass switch of any kind (the bugs.debian.org web interface
  disallows ``User-agent: *`` site-wide).
- The PoW challenge flow solves within the CPU and request budgets; an
  unrecognized shape fails explicitly as ``challenge-unknown``.
- Requests are read-only GET/POST, redirects are never followed
  (reported honestly), and hosts/paths are fixed constants:
  every URL is rebuilt by the application from a strictly validated
  numeric bug id; the host allowlist is re-checked per request.
- Usage mistakes (invalid bug id or source) raise ``UsageError``
  before any network activity (the entry maps exit 2).
- Every failure becomes a failed :class:`BtsResult` carrying a stable
  machine error code plus the per-source
  ``attempts`` provenance; nothing raises past ``fetch`` except
  ``UsageError``.

No test-only injection seams exist: tests patch
``_open_connection`` / ``_public_address`` at this module boundary
(the connection primitives are the transport-internal seams prepared
for this adapter).
"""

from __future__ import annotations

import hashlib
import http.client
import re
import socket
import ssl
import time
import urllib.parse
import urllib.robotparser
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from gleam.core.result import DiagnosticItem, UsageError
from gleam.core.transport.net import _open_connection
from gleam.core.transport.safety import FetchError, _public_address, _validate_url
from gleam.core.verbose import _quiet, robots_line

from .challenge import (
    BTS_CHALLENGE_PATH,
    detect_challenge,
    extract_anubis_challenge,
    solve_hashcash,
    solve_varnish_pow,
)
from .errors import BtsError
from .messages import (
    parse_mbox_messages,
    parse_soap_bug_log,
    soap_fault_string,
)
from .policy import (
    AUTO_FALLBACK_ELIGIBLE,
    BTS_ALLOWED_HOSTS,
    BTS_SOURCES,
    DEFAULT_BTS_WEB_BASE,
    bug_page_url,
    build_mbox_url,
    build_soap_get_bug_log,
    build_soap_url,
    policy_dict,
    validate_bug_id,
)

__all__ = ["BtsClient", "BtsPolicy", "BtsResult"]

#: Stable ``where`` marker for every diagnostic this adapter emits.
WHERE = "gleam.core.sources.bts"

#: Header names whose values are always redacted before persistence.
_COOKIE_HEADERS = ("cookie", "set-cookie", "set-cookie2", "authorization")

_NOT_FOUND_FAULT_RE = re.compile(r"no such|not exist|unknown bug|does not",
                                 re.I)


@dataclass(frozen=True)
class BtsResult:
    """Typed adapter result for one fetch.

    ``status`` is adapter-level: ``ok`` (messages usable, possibly
    empty) or ``failed`` (``errors`` non-empty). Truncation keeps
    ``ok`` and sets ``truncated``/``more_available`` -- the ENTRY
    derives the ``partial`` Result status from that flag.
    ``provenance`` is a parallel array (entry i belongs to
    ``messages[i]``). ``to_data()`` returns the ``bts.data``
    field set; the raw response bytes stay in ``body_bytes`` (never in
    ``to_data``) so managed callers can persist them through the
    evidence store (raw shapes stay out of the public schema).

    Entry recipe: ``data = {**result.to_data()}``;
    ``status = "partial" if result.truncated else result.status``;
    lift ``errors``/``diagnostics``/``warnings`` as-is; ``finalize()``.
    """

    status: str
    bug_id: int
    source: str
    messages: list[dict] = field(default_factory=list)
    message_count: int = 0
    truncated: bool = False
    more_available: bool = False
    provenance: list[str] = field(default_factory=list)
    errors: list[DiagnosticItem] = field(default_factory=list)
    diagnostics: list[DiagnosticItem] = field(default_factory=list)
    warnings: list[DiagnosticItem] = field(default_factory=list)
    # provenance / audit meta (advisory; entries and tools may surface it)
    source_selection: dict = field(default_factory=dict)
    attempts: list[dict] = field(default_factory=list)
    url: str = ""
    bug_page_url: str = ""
    http_status: int | None = None
    fetched_at: str = ""
    content_bytes: int = 0
    content_sha256: str = ""
    body_truncated: bool = False
    body_bytes: bytes = b""
    headers_redacted: dict = field(default_factory=dict)
    robots: dict | None = None
    challenge: dict = field(default_factory=lambda: {
        "encountered": False, "solved": False, "trace": []})
    parse_meta: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    envelope_bytes: int = 0

    def to_data(self) -> dict[str, Any]:
        """The ``bts.data`` fields."""
        return {
            "bug_id": self.bug_id,
            "source": self.source,
            "messages": self.messages,
            "message_count": self.message_count,
            "truncated": self.truncated,
            "more_available": self.more_available,
            "provenance": list(self.provenance),
        }


def redact_sensitive_headers(headers: dict) -> dict:
    """Redact before persistence: cookie/authorization values never
    enter any artifact."""

    out = {}
    for key, values in headers.items():
        if key.lower() in _COOKIE_HEADERS:
            out[key] = [f"<redacted:{len(v)} chars>" for v in values]
        else:
            out[key] = list(values)
    return out


def _cookie_token_safe(token: str) -> bool:
    """Cookie names/values accept printable ASCII without ``;``/``,`
    or control characters: a value carrying CRLF would make
    http.client raise ValueError (misgraded as transport-error) -- an
    unsafe cookie is dropped whole."""

    return bool(token) and all(
        0x20 <= ord(ch) <= 0x7E and ch not in ";," for ch in token)


class BtsClient:
    """Bounded BTS client (BTS-specific trusted path; the shared
    transport Fetcher and its global policy are never touched).

    The request budget and the robots fact accumulate per CLIENT
    INSTANCE (one instance may serve several fetches, e.g. the
    investigate fetch_bug tool); a caller may additionally share a
    global request budget through the ``budget_check`` hook (the
    hook raises ``BtsError("budget-exhausted", ...)`` to veto a
    request). Budgets: total requests (robots and challenge retries
    included), minimum interval per host, per-response bytes, timeout,
    message count, and PoW CPU seconds. Challenge-session cookies live
    in instance memory only and never reach logs or artifacts.
    """

    def __init__(self, policy: "BtsPolicy | None" = None,
                 clock=time.monotonic, sleep=time.sleep,
                 budget_check=None,
                 verbose: Callable[[str], None] | None = None):
        self.policy = policy_dict(policy)
        self.base = DEFAULT_BTS_WEB_BASE  # fixed; never a parameter
        self.request_count = 0
        self._last_hit: float | None = None
        self._clock = clock
        self._sleep = sleep
        self._budget_check = budget_check
        self._cookies: dict[str, str] = {}
        self.robots_fact: dict | None = None
        self.challenge_trace: list[dict] = []
        # Narration sink (``make_verbose`` callable or _quiet when
        # unset); process-stream only, never a Result payload.
        self._verbose = verbose or _quiet

    # -- low-level request (pinned IP + bounded read) -------------------

    def _reserve(self, what: str) -> None:
        if self._budget_check is not None:
            self._budget_check(what)  # may raise BtsError budget-exhausted
        cap = int(self.policy["max_requests"])
        if self.request_count >= cap:
            raise BtsError("budget-exhausted",
                           f"request cap {cap} reached before {what}")
        self.request_count += 1

    def _throttle(self) -> None:
        interval = float(self.policy["min_interval_s"])
        if interval <= 0 or self._last_hit is None:
            self._last_hit = self._clock()
            return
        wait = interval - (self._clock() - self._last_hit)
        if wait > 0:
            self._sleep(wait)
        self._last_hit = self._clock()

    def _request(self, url: str, *, method: str = "GET",
                 body: bytes | None = None,
                 extra_headers: dict | None = None,
                 max_bytes: int | None = None) -> dict:
        try:
            parsed = _validate_url(url)
        except FetchError as exc:
            raise BtsError("unsafe-destination", exc.message) from exc
        if (parsed.hostname or "").lower() not in BTS_ALLOWED_HOSTS:
            raise BtsError("unsafe-destination",
                           f"host {parsed.hostname!r} not in BTS allowlist")
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            pinned_ip = _public_address(host)
        except FetchError as exc:
            # DNS/address failures are transport failures (honest kind)
            raise BtsError("transport-error",
                           f"{exc.kind}: {exc.message}") from exc
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        self._throttle()
        conn = _open_connection(parsed.scheme, host, port, pinned_ip,
                                float(self.policy["timeout_s"]))
        headers = {
            "User-Agent": self.policy["user_agent"],
            "Accept": "*/*",
            "Connection": "close",
        }
        if body is not None:
            headers["Content-Length"] = str(len(body))
        headers.update(extra_headers or {})
        if self._cookies:
            headers["Cookie"] = "; ".join(
                f"{k}={v}" for k, v in self._cookies.items())
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            status = response.status
            resp_headers: dict = {}
            for key, value in response.getheaders():
                resp_headers.setdefault(key.lower(), []).append(value)
            for value in resp_headers.get("set-cookie", []):
                parts = value.split(";", 1)
                if "=" in parts[0]:
                    name, _, val = parts[0].partition("=")
                    name, val = name.strip(), val.strip()
                    if _cookie_token_safe(name) and _cookie_token_safe(val):
                        self._cookies[name] = val
            deadline = time.monotonic() + float(self.policy["timeout_s"])
            chunks, total, truncated = [], 0, False
            cap = int(max_bytes or self.policy["max_bytes"])
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"status": status, "headers": resp_headers,
                            "body": b"".join(chunks), "truncated": True,
                            "error": {"kind": "timeout",
                                      "message": "body read deadline "
                                                 "exceeded"}}
                if conn.sock is not None:
                    conn.sock.settimeout(remaining)
                chunk = response.read(8192)
                if not chunk:
                    break
                if total + len(chunk) > cap:
                    take = cap - total
                    if take:
                        chunks.append(chunk[:take])
                    truncated = True
                    break
                chunks.append(chunk)
                total += len(chunk)
            return {"status": status, "headers": resp_headers,
                    "body": b"".join(chunks), "truncated": truncated,
                    "error": None}
        except (socket.timeout, TimeoutError) as exc:
            raise BtsError("timeout", f"{type(exc).__name__}: {exc}") from exc
        except OSError as exc:
            raise BtsError("transport-error",
                           f"{type(exc).__name__}: {exc}") from exc
        except (http.client.HTTPException, ssl.SSLError, ValueError) as exc:
            raise BtsError("transport-error",
                           f"{type(exc).__name__}: {exc}") from exc
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # -- robots facts (fixed rules; no bypass switch) -------------------

    def robots(self) -> dict:
        """Fetch (and cache) the BTS robots.txt fact; failures are
        recorded as unknown, never extrapolated."""

        if self.robots_fact is not None:
            return self.robots_fact
        self._reserve("robots.txt fetch")
        url = self.base + "/robots.txt"
        try:
            resp = self._request(url, max_bytes=int(
                self.policy["robots_max_bytes"]))
        except BtsError as exc:
            if exc.kind in ("transport-error", "timeout"):
                self.robots_fact = {
                    "url": url, "mode": "unknown",
                    "detail": f"robots fetch failed ({exc.kind}); recorded, "
                              f"no inference made", "raw": None}
                return self.robots_fact
            raise
        status = resp["status"] or 0
        fact = {"url": url, "status": status,
                "raw": resp["body"].decode("utf-8", "replace")
                if status == 200 else None}
        if resp.get("error") is not None:
            # a mid-body robots failure abstains (unknown)
            fact.update(mode="unknown",
                        detail=f"robots fetch error mid-body (status "
                               f"{status}, {resp['error']['kind']}); "
                               f"conservative abstain")
        elif status in (404, 410):
            fact.update(mode="absent", detail="robots absent")
        elif status != 200:
            fact.update(mode="unknown",
                        detail=f"robots status {status}; recorded")
        elif resp["truncated"]:
            # a robots body truncated at robots_max_bytes may
            # have lost trailing Disallow lines -- abstain as unknown,
            # never read that as "allowed"
            fact.update(mode="unknown",
                        detail="robots body truncated at robots_max_bytes; "
                               "rules may be incomplete; conservative "
                               "abstain")
        else:
            parser = urllib.robotparser.RobotFileParser()
            try:
                parser.parse(fact["raw"].splitlines())
                fact.update(mode="rules", parser=parser,
                            detail="robots rules parsed")
            except Exception as exc:
                fact.update(mode="unknown",
                            detail=f"robots parse error: {exc}")
        self.robots_fact = fact
        return fact

    def _robots_allows(self, url: str) -> bool | None:
        """True = explicitly allowed, False = explicitly disallowed,
        None = unknown."""

        fact = self.robots()
        if fact["mode"] == "absent":
            return True
        if fact["mode"] != "rules":
            return None
        return fact["parser"].can_fetch("*", url)

    def _narrate_robots(self, url: str, allowed: bool | None) -> None:
        """One robots verdict line for the mbox gate through the shared
        ``robots_line`` helper (the transport Fetcher narrates the same
        shapes; bts has no exempt verdict, so its label never
        appears here)."""

        site = self.base + "/"
        if allowed is True:
            self._verbose(robots_line(site, "allow", None))
        elif allowed is False:
            path = urllib.parse.urlsplit(url).path or "/"
            self._verbose(robots_line(site, "disallow", path))
        else:
            self._verbose(robots_line(site, "unknown",
                                      self.robots_fact["detail"]))

    def _robots_public(self) -> dict | None:
        if self.robots_fact is None:
            return None
        return {k: v for k, v in self.robots_fact.items() if k != "parser"}

    # -- challenge handling ---------------------------------------------

    def _handle_challenge(self, challenge: dict, resp: dict, url: str,
                          method: str, body: bytes | None,
                          extra_headers: dict | None) -> dict:
        """On a challenge: solve within the documented client flow and
        retry once; unrecognized/unsolvable shapes fail honestly
        (``challenge`` / ``challenge-unknown``).

        varnish-pow (observed Debian shape): ``pow_challenge`` arrives
        with the challenge response as a cookie (when missing, the
        challenge page is fetched once to obtain it); after solving,
        the answer is returned via the ``pow_nonce`` cookie and the
        original URL is retried. Cookies stay in instance memory and
        are never persisted."""

        ctype = challenge["type"]
        if ctype == "varnish-pow":
            pow_value = self._cookies.get("pow_challenge")
            if pow_value is None:
                self._reserve("challenge page fetch")
                page_resp = self._request(
                    self.base + BTS_CHALLENGE_PATH
                    + "?original=" + urllib.parse.quote(url))
                if "pow_challenge" not in self._cookies:
                    raise BtsError(
                        "challenge-unknown",
                        "Varnish challenge presented no pow_challenge "
                        "cookie (page status "
                        f"{page_resp['status']}); refusing to guess")
                pow_value = self._cookies["pow_challenge"]
            solved = solve_varnish_pow(
                pow_value, float(self.policy["max_pow_seconds"]))
            if solved is None:
                raise BtsError(
                    "challenge",
                    f"Varnish PoW not solved within "
                    f"{self.policy['max_pow_seconds']}s CPU budget; "
                    f"honest failure")
            tries, digest, elapsed = solved
            self._cookies["pow_nonce"] = str(tries)
            self.challenge_trace.append({
                "type": "debian-varnish-pow",
                "difficulty": "sha256 first byte zero (diff=2)",
                "solved": True, "cpu_seconds": round(elapsed, 3),
                "tries": tries,
                "pow_nonce_sha256":
                    hashlib.sha256(str(tries).encode()).hexdigest(),
                "cookie_names": sorted(self._cookies),
                "note": "cookie values stay in memory only; nonce digest "
                        "logged for audit",
            })
            self._reserve("retry after challenge")
            retry = self._request(url, method=method, body=body,
                                  extra_headers=extra_headers)
            again = detect_challenge(retry["status"], retry["headers"],
                                     retry["body"])
            if again is not None:
                raise BtsError("challenge",
                               "challenge persisted after documented "
                               "solve flow; honest failure")
            return retry
        if ctype != "pow-challenge":
            raise BtsError("challenge",
                           f"{ctype}: {challenge['detail']}")
        anubis = extract_anubis_challenge(resp["body"])
        if anubis is None:
            raise BtsError(
                "challenge-unknown",
                f"PoW challenge detected ({challenge['detail']}) but its "
                f"format is not a recognized documented flow; refusing to "
                f"guess; body excerpt preserved by the managed caller")
        challenge_str = str(anubis.get("challenge", ""))
        try:
            difficulty = int(anubis.get("difficulty", 0))
        except (TypeError, ValueError):
            difficulty = 0
        if not challenge_str or difficulty <= 0:
            raise BtsError("challenge-unknown",
                           "Anubis challenge data missing challenge/"
                           "difficulty fields")
        solved = solve_hashcash(challenge_str, difficulty,
                                float(self.policy["max_pow_seconds"]))
        if solved is None:
            raise BtsError(
                "challenge",
                f"hashcash not solved within "
                f"{self.policy['max_pow_seconds']}s CPU budget "
                f"(difficulty {difficulty}); honest failure")
        nonce, digest, elapsed = solved
        self.challenge_trace.append({
            "type": "anubis-hashcash", "difficulty": difficulty,
            "solved": True, "cpu_seconds": round(elapsed, 3),
            "nonce_sha256": hashlib.sha256(nonce.encode()).hexdigest(),
            "note": "nonce value not logged; only its digest for audit",
        })
        # Anubis documented submission endpoint (same-site fixed path;
        # never model-controlled)
        pass_url = (self.base
                    + "/.within.website/x/cmd/anubis/api/pass-challenge")
        self._reserve("challenge submission")
        pass_resp = self._request(
            pass_url, method="POST",
            body=(f"challenge={urllib.parse.quote(challenge_str)}"
                  f"&nonce={urllib.parse.quote(nonce)}"
                  f"&hash={digest}"
                  f"&redir={urllib.parse.quote(url)}"
                  f"&elapsedMilliseconds=0").encode("utf-8"),
            extra_headers={"Content-Type":
                           "application/x-www-form-urlencoded"})
        self.challenge_trace.append({
            "type": "challenge-submission", "status": pass_resp["status"],
            "cookie_names": sorted(self._cookies),
        })
        if pass_resp["status"] not in (200, 302, 303):
            raise BtsError("challenge",
                           f"challenge submission rejected "
                           f"(HTTP {pass_resp['status']}); honest failure")
        self._reserve("retry after challenge")
        retry = self._request(url, method=method, body=body,
                              extra_headers=extra_headers)
        again = detect_challenge(retry["status"], retry["headers"],
                                 retry["body"])
        if again is not None:
            raise BtsError("challenge",
                           "challenge persisted after documented solve "
                           "flow; honest failure")
        return retry

    # -- main entry ------------------------------------------------------

    def fetch(self, bug_id, source: str = "auto") -> BtsResult:
        """Fetch the full public discussion of one bug.

        ``source`` is one of ``BTS_SOURCES``:

        - ``soap`` / ``mbox``: explicit single source, a failure is a
          failure (no fallback); the mbox web endpoint requires robots
          to not disallow it (an unknown robots verdict is a
          conservative refusal; there is no switch to accept it);
        - ``auto`` (default): try SOAP first (the documented machine
          interface); fall back to mbox ONLY when SOAP fails with an
          eligibility error AND robots explicitly permits the
          mbox endpoint; rate-limited / not-found / budget-exhausted
          and every other kind never fall back, and robots is never
          bypassed.

        Invalid bug id / source values raise ``UsageError`` before any
        network activity. Every fetch failure is returned as a
        failed :class:`BtsResult` carrying the per-source ``attempts``
        provenance.
        """

        bug_id = validate_bug_id(bug_id)
        if source not in BTS_SOURCES:
            raise UsageError(f"source must be one of {list(BTS_SOURCES)}")

        if source != "auto":
            selection = {"requested": source, "mode": "explicit",
                         "tried": [source], "fallback_used": False}
            self._verbose(f"source {source}")
            try:
                payload = self._fetch_single(bug_id, source)
            except BtsError as exc:
                if source == "soap":
                    self._verbose(
                        f"soap failed ({exc.kind}: {exc.message})")
                attempts = [{"source": source,
                             "error": {"kind": exc.kind,
                                       "message": exc.message}}]
                return self._failed_result(bug_id, source, exc,
                                           attempts, selection)
            attempts = [{"source": source, "error": None}]
            return self._ok_result(bug_id, source, payload, selection,
                                   attempts)

        attempts: list[dict] = []
        self._verbose("source auto: trying soap")
        try:
            payload = self._fetch_single(bug_id, "soap")
            selection = {"requested": "auto", "mode": "auto",
                         "tried": ["soap"], "fallback_used": False}
            return self._ok_result(
                bug_id, "soap", payload, selection,
                [{"source": "soap", "error": None}])
        except BtsError as exc:
            soap_error = exc
            self._verbose(f"soap failed ({exc.kind}: {exc.message})")
            attempts.append({"source": "soap",
                             "error": {"kind": exc.kind,
                                       "message": exc.message}})
        if soap_error.kind not in AUTO_FALLBACK_ELIGIBLE:
            return self._failed_result(
                bug_id, "soap", soap_error, attempts,
                {"requested": "auto", "mode": "auto", "tried": ["soap"],
                 "fallback_used": False})
        # robots gate (the robots fact is already cached from the SOAP
        # attempt -- no extra request): only an explicit robots permit
        # allows the fallback; unknown or denying verdicts never do,
        # and there is no bypass switch
        mbox_url = build_mbox_url(bug_id)
        allowed = self._robots_allows(mbox_url)
        if allowed is not True:
            # a refused fallback narrates its verdict here; on
            # permission the mbox branch narrates the same verdict
            # itself right before its GET (exactly one robots line
            # per run, never two)
            self._narrate_robots(mbox_url, allowed)
            fallback_note = (
                "auto fallback to mbox not attempted: robots does not "
                "clearly permit the web mbox endpoint "
                f"({self.robots_fact.get('mode')} / "
                f"{self.robots_fact.get('detail')}); robots is never "
                f"bypassed")
            final = BtsError(
                soap_error.kind,
                f"{soap_error.message}; [auto] {fallback_note}")
            return self._failed_result(
                bug_id, "soap", final, attempts,
                {"requested": "auto", "mode": "auto", "tried": ["soap"],
                 "fallback_used": False})
        try:
            payload = self._fetch_single(bug_id, "mbox")
            selection = {"requested": "auto", "mode": "auto",
                         "tried": ["soap", "mbox"], "fallback_used": True,
                         "fallback_reason": soap_error.kind}
            return self._ok_result(
                bug_id, "mbox", payload, selection,
                attempts + [{"source": "mbox", "error": None}])
        except BtsError as exc:
            attempts.append({"source": "mbox",
                             "error": {"kind": exc.kind,
                                       "message": exc.message}})
            final = BtsError(
                exc.kind,
                f"{exc.message}; [auto] both sources failed (soap: "
                f"{soap_error.kind}: {soap_error.message}; mbox: "
                f"{exc.kind}: {exc.message})")
            selection = {"requested": "auto", "mode": "auto",
                         "tried": ["soap", "mbox"], "fallback_used": False}
            return self._failed_result(bug_id, "mbox", final, attempts,
                                       selection)

    def _fetch_single(self, bug_id: int, source: str) -> dict:
        """Single-source fetch (source pre-checked)."""
        robots = self.robots()
        if source == "mbox":
            url = build_mbox_url(bug_id)
            allowed = self._robots_allows(url)
            self._narrate_robots(url, allowed)
            if allowed is False:
                # explicit robots disallow: no bypass switch exists (the
                # documented machine interface is SOAP)
                raise BtsError(
                    "robots-disallow",
                    "bugs.debian.org robots.txt disallows the web mbox "
                    "endpoint for generic clients (User-agent: * "
                    "Disallow: /); refusing -- use the documented SOAP "
                    "machine interface (source=soap) instead")
            if allowed is None:
                raise BtsError("robots-disallow",
                               f"robots state unknown ({robots['detail']}); "
                               f"conservative refusal for web endpoint")
            cap = int(self.policy["max_requests"])
            self._verbose(
                f"GET {url} (budget "
                f"{cap - self.request_count}/{cap})")
            self._reserve("mbox fetch")
            resp = self._request(url)
            reason = http.client.responses.get(resp["status"], "")
            status_text = (f"{resp['status']} {reason}" if reason
                           else f"{resp['status']}")
            self._verbose(f"{status_text}, {len(resp['body'])} bytes")
            extra = {}
        else:
            url = build_soap_url()
            envelope = build_soap_get_bug_log(bug_id)
            self._reserve("soap get_bug_log")
            resp = self._request(
                url, method="POST", body=envelope,
                extra_headers={
                    "Content-Type": "text/xml; charset=utf-8",
                    "SOAPAction": '"urn:Debbugs/SOAP#get_bug_log"'})
            extra = {"envelope_bytes": len(envelope)}
        # A body-read timeout returns partial body plus an error field:
        # grade it by its kind and never parse the partial body as a
        # 200 success
        if resp.get("error"):
            err = resp["error"]
            raise BtsError(err["kind"],
                           f"{err['message']}; partial body "
                           f"({len(resp['body'])} bytes) discarded, "
                           f"not parsed")

        result = {
            "bug_id": bug_id,
            "source": source,
            "url": url,
            "bug_page_url": bug_page_url(bug_id),
            "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                          time.gmtime()),
            "status": resp["status"],
            "headers_redacted": redact_sensitive_headers(resp["headers"]),
            "body_truncated": resp["truncated"],
            "content_bytes": len(resp["body"]),
            "content_sha256": hashlib.sha256(resp["body"]).hexdigest(),
            "robots": {k: v for k, v in robots.items() if k != "parser"},
            "challenge": {"encountered": False, "solved": False,
                          "trace": self.challenge_trace},
            **extra,
        }
        raw_body = resp["body"]

        # Challenge detection precedes redirect/429/status grading: the
        # Debian Varnish challenge itself appears as a 302 to
        # /.internal/challenge.html
        challenge = detect_challenge(resp["status"], resp["headers"],
                                     raw_body)
        if resp["status"] in (301, 302, 303, 307, 308) and challenge is None:
            location = (resp["headers"].get("location") or [None])[0]
            raise BtsError("redirect",
                           f"BTS returned {resp['status']} to "
                           f"{location!r}; redirects are not followed "
                           f"(honest report)")
        if resp["status"] == 429:
            retry_after = (resp["headers"].get("retry-after") or [None])[0]
            raise BtsError("rate-limited",
                           f"HTTP 429 (retry-after={retry_after}); "
                           f"no automatic retry")
        if resp["status"] == 404:
            raise BtsError("not-found", "HTTP 404 from BTS")
        if challenge is not None:
            result["challenge"]["encountered"] = True
            self.challenge_trace.append(challenge)
            resp = self._handle_challenge(challenge, resp, url,
                                          "POST" if source == "soap"
                                          else "GET",
                                          envelope if source == "soap"
                                          else None,
                                          {"Content-Type":
                                           "text/xml; charset=utf-8",
                                           "SOAPAction":
                                           '"urn:Debbugs/SOAP#get_bug_log"'}
                                          if source == "soap" else None)
            result["challenge"]["solved"] = True
            result.update(status=resp["status"],
                          headers_redacted=redact_sensitive_headers(
                              resp["headers"]),
                          body_truncated=resp["truncated"],
                          content_bytes=len(resp["body"]),
                          content_sha256=hashlib.sha256(
                              resp["body"]).hexdigest())
            raw_body = resp["body"]

        if resp["status"] != 200:
            if 400 <= resp["status"] < 500:
                raise BtsError("http-4xx",
                               f"HTTP {resp['status']} from BTS")
            if source == "soap":
                # SOAP Faults arrive with HTTP 500: grade via the
                # faultstring first
                fault_text = soap_fault_string(raw_body)
                if fault_text:
                    # classification uses the full text; the error
                    # message carries only 200 characters (server text)
                    if _NOT_FOUND_FAULT_RE.search(fault_text):
                        raise BtsError("not-found",
                                       f"SOAP Fault: {fault_text[:200]}")
                    raise BtsError("soap-fault",
                                   f"HTTP {resp['status']}; SOAP Fault: "
                                   f"{fault_text[:200]}")
            raise BtsError("http-error",
                           f"HTTP {resp['status']} from BTS")

        try:
            if source == "mbox":
                messages, meta = parse_mbox_messages(
                    raw_body, source_url=url, policy=self.policy)
            else:
                messages, meta = parse_soap_bug_log(
                    raw_body, source_url=url, policy=self.policy)
        except BtsError as exc:
            if exc.kind == "soap-fault" and _NOT_FOUND_FAULT_RE.search(
                    exc.message):
                raise BtsError("not-found", exc.message) from exc
            if exc.kind == "parse-error" and b"does not exist" in raw_body[:4096]:
                raise BtsError("not-found",
                               "BTS body says bug does not exist "
                               "(parse-detected)") from exc
            raise
        result["messages"] = messages
        result["messages_count"] = len(messages)
        result["parse_meta"] = meta
        result["body_bytes"] = raw_body
        count = len(messages)
        unit = "message" if count == 1 else "messages"
        if source == "mbox":
            self._verbose(f"mbox fetch: {count} {unit}")
        else:
            self._verbose(f"soap get_bug: {count} {unit}")
        return result

    # -- typed result assembly -------------------------------------------

    def _ok_result(self, bug_id: int, source: str, payload: dict,
                   selection: dict, attempts: list[dict]) -> BtsResult:
        messages = payload["messages"]
        meta = payload["parse_meta"]
        cap_hit = int(meta.get("messages_total", len(messages))) > len(messages)
        truncated = bool(payload["body_truncated"] or cap_hit)
        notes = list(meta.get("notes", []))
        if payload["body_truncated"]:
            notes.append("response body truncated at max_bytes; trailing "
                         "messages may be missing")
        return BtsResult(
            status="ok",
            bug_id=bug_id,
            source=source,
            messages=messages,
            message_count=len(messages),
            truncated=truncated,
            more_available=truncated,
            provenance=[f"bts:bug={bug_id}#msg={i + 1}"
                        for i in range(len(messages))],
            errors=[], diagnostics=[], warnings=[],
            source_selection=dict(selection),
            attempts=[dict(a) for a in attempts],
            url=payload["url"],
            bug_page_url=payload["bug_page_url"],
            http_status=payload["status"],
            fetched_at=payload["retrieved_at"],
            content_bytes=payload["content_bytes"],
            content_sha256=payload["content_sha256"],
            body_truncated=payload["body_truncated"],
            body_bytes=payload["body_bytes"],
            headers_redacted=payload["headers_redacted"],
            robots=payload["robots"],
            challenge=dict(payload["challenge"],
                           trace=list(payload["challenge"]["trace"])),
            parse_meta=dict(meta),
            notes=notes,
            envelope_bytes=int(payload.get("envelope_bytes", 0)),
        )

    def _failed_result(self, bug_id: int, source: str, exc: BtsError,
                       attempts: list[dict], selection: dict) -> BtsResult:
        details: dict[str, Any] = {"attempts": attempts}
        robots_public = self._robots_public()
        if robots_public is not None:
            details["robots_mode"] = robots_public.get("mode")
        error = DiagnosticItem(code=exc.kind, message=exc.message,
                               where=WHERE, details=details)
        return BtsResult(
            status="failed",
            bug_id=bug_id,
            source=source,
            errors=[error],
            source_selection=dict(selection),
            attempts=[dict(a) for a in attempts],
            robots=robots_public,
            # the trace only grows when a challenge was detected/handled
            challenge={"encountered": bool(self.challenge_trace),
                       "solved": any(t.get("solved")
                                     for t in self.challenge_trace),
                       "trace": list(self.challenge_trace)},
        )
