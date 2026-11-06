"""Typed SearXNG client: one bounded JSON GET per search, over the
package's own trusted-base transport.

The base is an operator-configured trusted endpoint (constructor
parameter; the config domain owns the value) with its own bounded
request path -- redirects are NEVER followed, the connect+read time
is deadline-bounded, and the response body is byte-capped. No robots
judging applies (the target is the operator's own local service).

Honest grading (README data semantics: failure = unknown, never
"no results"):
- a real empty result set with responsive engines is a CLEAN EMPTY
  (``status="ok"``, zero results, ``ambiguous=False``);
- zero results together with engine failures is EXPLICIT AMBIGUITY
  (``status="failed"``, ``ambiguous=True``, stable code
  ``all-engines-failed-or-empty``) -- distinct from both a clean
  empty and a failure-without-notice;
- 403 (JSON format not enabled) / 429 (rate limited) / captcha /
  malformed JSON each carry their own stable failure grade;
- the raw search JSON is retained on the typed result (``raw_body``)
  for observation archival by the managed entry, never inside
  ``to_data()``;
- snippets are third-party search summaries, weaker than fetched page
  content -- that provenance note rides in the result metadata and
  the rendered text.

Usage mistakes (empty/non-string/over-long query, malformed base URL)
raise ``UsageError`` (exit 2 at the entry). Tests patch
``gleam.core.sources.searxng.client.transport`` (module boundary);
no injection seams exist.
"""

from __future__ import annotations

import http.client
import json
import math
import time
import urllib.parse
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from gleam.core.user_agent import USER_AGENT
from gleam.core.result import DiagnosticItem, UsageError

from . import transport
from .normalize import (
    norm_unresponsive,
    normalize_results,
    render_results_text,
)

__all__ = [
    "DEFAULT_SEARCH_BASE",
    "SEARCH_MAX_BYTES",
    "SEARCH_TIMEOUT_S",
    "SearxngClient",
    "SearxngPolicy",
    "SearxngResult",
]

DEFAULT_SEARCH_BASE = "http://127.0.0.1:8888"
SEARCH_TIMEOUT_S = 10.0
SEARCH_MAX_BYTES = 1 << 20  # 1 MiB: ample for a JSON search response
DEFAULT_MAX_QUERY_CHARS = 400
#: Shared User-Agent; the version is spliced in from the single version
#: source at import.
DEFAULT_USER_AGENT = USER_AGENT

_REDIRECT_STATUSES = (301, 302, 303, 307, 308)

#: Stable ``where`` marker for every diagnostic this adapter emits.
WHERE = "gleam.core.sources.searxng"


def _reject_constant(name: str) -> float:
    raise ValueError(f"non-finite JSON constant {name}")


def _parse_float_strict(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"non-finite JSON number {text}")
    return value


def _loads_strict(text: str):
    """Strict JSON: NaN/Infinity constants and overflowed floats are
    rejected. Local definition on purpose, so this module never
    depends on the model domain."""

    return json.loads(text, parse_constant=_reject_constant,
                      parse_float=_parse_float_strict)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class SearxngPolicy:
    """Numeric bounds for one client. Live values come from the config
    domain at call time (``GLEAM_SEARCH_TIMEOUT``-style keys); these
    dataclass defaults exist only for standalone testing."""

    timeout_s: float = SEARCH_TIMEOUT_S
    max_bytes: int = SEARCH_MAX_BYTES
    max_query_chars: int = DEFAULT_MAX_QUERY_CHARS
    user_agent: str = DEFAULT_USER_AGENT


@dataclass(frozen=True)
class SearxngResult:
    """Typed adapter result for one search.

    ``status`` is adapter-level: ``ok`` (results usable, possibly a
    clean empty) or ``failed`` (errors non-empty; every failure means
    UNKNOWN -- never "no results"). ``ambiguous`` marks the explicit
    zero-results-plus-engine-failures state so it can never be
    confused with a clean empty. ``raw_body`` keeps the exact
    response bytes for observation archival; ``to_data()`` never
    emits raw shapes.
    """

    status: str
    query: str
    final_url: str = ""
    http_status: int | None = None
    results: list = field(default_factory=list)
    unresponsive_engines: list = field(default_factory=list)
    number_of_results: float | None = None
    deduped_count: int = 0
    ambiguous: bool = False
    raw_body: bytes = b""
    rendered_text: str = ""
    note: str = ""
    errors: list[DiagnosticItem] = field(default_factory=list)
    diagnostics: list[DiagnosticItem] = field(default_factory=list)
    warnings: list[DiagnosticItem] = field(default_factory=list)
    fetched_at: str = ""
    duration_s: float = 0.0

    def to_data(self) -> dict[str, Any]:
        """Stable data field set; raw bytes stay out."""
        return {
            "query": self.query,
            "final_url": self.final_url,
            "http_status": self.http_status,
            "results": self.results,
            "result_count": len(self.results),
            "deduped_count": self.deduped_count,
            "unresponsive_engines": self.unresponsive_engines,
            "number_of_results": self.number_of_results,
            "ambiguous": self.ambiguous,
            "note": self.note,
        }


class SearxngClient:
    """One bounded ``GET /search?format=json`` against the configured
    SearXNG instance per :meth:`search` call.

    Constructor shape (stable for the investigate search_web tool):
    ``SearxngClient(base=..., timeout=...)`` with optional
    ``policy=SearxngPolicy(...)`` for the remaining numeric bounds; an
    explicit ``timeout`` overrides the policy value.
    """

    def __init__(self, base: str = DEFAULT_SEARCH_BASE,
                 timeout: float | None = None,
                 policy: SearxngPolicy | None = None):
        policy = policy or SearxngPolicy()
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
                    or not math.isfinite(timeout) or timeout <= 0:
                raise UsageError(
                    f"timeout must be a positive finite number; "
                    f"got {timeout!r}")
            policy = replace(policy, timeout_s=float(timeout))
        self.policy = policy
        if not isinstance(base, str):
            raise UsageError(
                f"base must be a string, got {type(base).__name__}")
        self.base = (base or DEFAULT_SEARCH_BASE).strip()
        parsed = urllib.parse.urlsplit(self.base)
        if parsed.scheme.lower() not in ("http", "https"):
            raise UsageError(f"search base scheme must be http(s): "
                             f"{self.base!r}")
        if parsed.username is not None or parsed.password is not None:
            raise UsageError("search base must not contain credentials")
        self.scheme = parsed.scheme.lower()
        self.host = parsed.hostname
        if not self.host:
            raise UsageError(f"search base has no host: {self.base!r}")
        try:
            port = parsed.port
        except ValueError as exc:
            raise UsageError(f"search base port invalid: {exc}") from exc
        self.port = port or (443 if self.scheme == "https" else 80)
        self.base_path = parsed.path.rstrip("/")

    def request_url(self, query: str) -> str:
        qs = urllib.parse.urlencode({"q": query, "format": "json",
                                     "language": "en"})
        return (f"{self.scheme}://{self.host}:{self.port}{self.base_path}"
                f"/search?{qs}")

    def _search_path(self, query: str) -> str:
        return self.base_path + "/search?" + urllib.parse.urlencode(
            {"q": query, "format": "json", "language": "en"})

    def _validate_query(self, query) -> str:
        if not isinstance(query, str) or not query.strip():
            raise UsageError("query must be a non-empty string")
        if len(query) > self.policy.max_query_chars:
            raise UsageError(
                f"query too long (max {self.policy.max_query_chars} chars; "
                f"got {len(query)})")
        return query

    def search(self, query: str) -> SearxngResult:
        query = self._validate_query(query)
        url = self.request_url(query)
        started = time.monotonic()
        fetched_at = _utc_now()
        try:
            status, headers, body = transport.request(
                self.scheme, self.host, self.port, self._search_path(query),
                timeout_s=self.policy.timeout_s,
                max_bytes=self.policy.max_bytes,
                user_agent=self.policy.user_agent)
        except TimeoutError as exc:
            return self._failed(
                query=query, final_url=url, fetched_at=fetched_at,
                duration_s=time.monotonic() - started,
                code="timeout",
                message=f"search timed out after "
                        f"{self.policy.timeout_s}s: {exc}",
                details={"final_url": url})
        except (OSError, http.client.HTTPException) as exc:
            # BadStatusLine/IncompleteRead etc. are HTTPExceptions, not
            # OSErrors: both are graded connection-error with the
            # original exception text kept, never leaked as a crash.
            return self._failed(
                query=query, final_url=url, fetched_at=fetched_at,
                duration_s=time.monotonic() - started,
                code="connection-error",
                message=f"{type(exc).__name__}: {exc}",
                details={"final_url": url})
        duration_s = time.monotonic() - started
        base = dict(query=query, final_url=url, http_status=status,
                    raw_body=body, fetched_at=fetched_at,
                    duration_s=duration_s)
        if status in _REDIRECT_STATUSES:
            location = (headers.get("location") or [None])[0]
            return self._failed(
                **base, code="unexpected-redirect",
                message=f"search endpoint returned {status}; redirects "
                        f"are not followed (trusted base is pinned); "
                        f"location={location!r}",
                details={"location": location})
        if status == 403:
            return self._failed(
                **base, code="format-forbidden",
                message="HTTP 403: JSON format likely not enabled on "
                        "this SearXNG instance (search.formats must "
                        "include json)")
        if status == 429:
            return self._failed(
                **base, code="rate-limited",
                message="HTTP 429 rate limited",
                details={"retry_after": (headers.get("retry-after")
                                         or [None])[0]})
        if status >= 500:
            return self._failed(
                **base, code="http-5xx",
                message=f"HTTP {status} from search endpoint")
        if status >= 400:
            return self._failed(
                **base, code="http-error",
                message=f"HTTP {status} from search endpoint")
        return self._parse_success(**base)

    @staticmethod
    def _failure_note() -> str:
        return ("search failure is explicit and means unknown; do not "
                "fabricate results or read it as 'no results'")

    @staticmethod
    def _provenance_note() -> str:
        return ("result URLs are eligible candidates; snippets are "
                "third-party search summaries (weaker evidence than "
                "fetched page content); verify by fetching before "
                "concluding identity")

    def _failed(self, *, code: str, message: str, details=None,
                **fields) -> SearxngResult:
        item = DiagnosticItem(
            code=code, message=message, where=WHERE,
            details=details if details is not None else {})
        return SearxngResult(
            status="failed", note=self._failure_note(), errors=[item],
            **fields)

    def _parse_success(self, *, query, final_url, http_status, raw_body,
                       fetched_at, duration_s) -> SearxngResult:
        base = dict(query=query, final_url=final_url,
                    http_status=http_status, raw_body=raw_body,
                    fetched_at=fetched_at, duration_s=duration_s)
        try:
            parsed = _loads_strict(raw_body.decode("utf-8", "replace"))
        except (json.JSONDecodeError, ValueError,
                UnicodeDecodeError) as exc:
            return self._failed(
                **base, code="malformed-json",
                message=f"search response is not valid strict JSON: {exc}")
        if not isinstance(parsed, dict) or not isinstance(
                parsed.get("results"), list):
            return self._failed(
                **base, code="malformed-json",
                message="search JSON missing results array")
        unresponsive = norm_unresponsive(
            parsed.get("unresponsive_engines"))
        results, deduped = normalize_results(parsed.get("results") or [])
        number = parsed.get("number_of_results")
        # Non-finite values (NaN/Infinity, however they slipped in)
        # become None; they never leak out of the adapter.
        number = (float(number)
                  if isinstance(number, (int, float))
                  and not isinstance(number, bool)
                  and math.isfinite(number) else None)
        rendered = render_results_text(query, results, unresponsive)
        captcha_engines = [
            e for e in unresponsive
            if isinstance(e, list) and len(e) > 1
            and "captcha" in str(e[1]).lower()]
        common = dict(query=query, final_url=final_url,
                      http_status=http_status, raw_body=raw_body,
                      fetched_at=fetched_at, duration_s=duration_s,
                      results=results, unresponsive_engines=unresponsive,
                      number_of_results=number, deduped_count=deduped,
                      rendered_text=rendered)
        if not results and unresponsive:
            # Explicit ambiguity: zero results AND engine failures --
            # this can never prove "no results".
            item = DiagnosticItem(
                code="all-engines-failed-or-empty",
                message="no results and some engines failed; this is "
                        "ambiguous (cannot prove 'no results'); "
                        "unresponsive engines are listed",
                where=WHERE,
                details={"unresponsive_engines": unresponsive,
                         "captcha_engines": captcha_engines or None})
            return SearxngResult(
                status="failed", ambiguous=True,
                note=self._failure_note(), errors=[item], **common)
        if not results:
            # Clean empty: engines responded, nothing found.
            return SearxngResult(
                status="ok",
                note="engines responded but returned no results for "
                     "this query",
                **common)
        warnings = []
        if captcha_engines:
            # Partial error with usable results: results stay usable
            # and the captcha fact rides as a warning (the entry maps
            # it to partial); never marked as an overall failure.
            warnings.append(DiagnosticItem(
                code="captcha-reported",
                message="some engines reported captcha; listed in "
                        "unresponsive_engines; usable results kept",
                where=WHERE,
                details={"captcha_engines": captcha_engines}))
        return SearxngResult(status="ok", note=self._provenance_note(),
                             warnings=warnings, **common)
