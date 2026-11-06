"""Typed Repology client: two read-only API operations over the
transport domain's public fetcher.

Trusted-base routing (github-client style): the API base comes
from the operator via the config domain (constructor parameter, default
``https://repology.org``) and every request still rides the FULL
public-URL validation chain (SSRF / DNS pinning / per-hop robots / byte
and request caps) because the fetch is performed by
:class:`gleam.core.transport.Fetcher`.

The ONE deliberate carve-out:
this client constructs its fetcher with ``exempt_hosts =
operator_set UNION {host of the configured base}`` -- Repology's own
API terms (https://repology.org/api) authorize programmatic access on
exactly this API surface at <=1 request/second, so the tool IS the
specific permission that beats the site's blanket robots disallow. The
union lives ONLY in this client's fetcher: the investigation's main
fetcher keeps exactly the operator-file exemption set, so manual
fetches of the same host stay governed by the operator file. Two
fetcher configurations coexist by design; every fetch's audit record
discloses its own effective ``robots_exempt`` list (the existing
mechanism -- every exempt fetch is marked robots ``"exempt"``).

Honest epistemic grading (failure = unknown, never "no results"):

- project: HTTP 200 + empty JSON array is the honest "no such project"
  answer (an ``ok`` result with ``total_entries`` 0 -- NEVER an error
  and never 404-equivalent);
- search: 200 + empty object is the honest zero-match answer;
- 429 / 5xx / timeout / transport / malformed-JSON failures are
  explicit failures (errors mean unknown);
- 404 is an unexpected explicit failure from this API, never graded as
  "no such project".

Budget (BTS precedent): a per-client request cap
(default 6) plus an optional ``budget_check`` hook -- the investigate
entry wires it to the SHARED HTTP request budget so every repology
request additionally reserves from the investigation-wide cap. The
min-interval policy (default 1.1s, API terms require <=1 rps) rides
the internal fetcher's same-host throttle.

Digests (computed here, never raw arrays): ``search_digest`` resolves
a name to at most 12 project slugs (plus a ``more`` count and a
case-insensitive ``exact_match`` flag); ``project_digest`` builds the
version ladder -- entries grouped by the stripped ``version``,
descending by the tolerant dotted-numeric comparator
(:func:`version_sort_key`), capped at 15 tiers with the remainder
aggregated into one disclosed tier, debian_* repos marked with a
``deb:`` prefix, and a ``debian`` block listing every debian_*
repo/version pair.

Usage mistakes (bad action/query/slug, over-length values, malformed
base) raise :class:`RepologyArgError` (a ``UsageError``, exit 2 at the
entry) BEFORE any network call; the two URL builders are the only
place a query or slug enters a request path (always percent-encoded
by the application).

Tests patch ``gleam.core.sources.repology.transport`` (the module
boundary); no injection seams exist.
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from gleam.core import transport
from gleam.core.result import DiagnosticItem, UsageError

__all__ = [
    "DEFAULT_REPOLOGY_BASE",
    "LADDER_TIER_CAP",
    "QUERY_MAX_CHARS",
    "REPOLOGY_ACTIONS",
    "REPOLOGY_POLICY_VERSION",
    "SLUG_MAX_CHARS",
    "SEARCH_PROJECTS_CAP",
    "TIER_REPOS_CAP",
    "RepologyArgError",
    "RepologyClient",
    "RepologyError",
    "RepologyPolicy",
    "RepologyResult",
    "WHERE",
    "build_project_url",
    "build_search_url",
    "project_digest",
    "search_digest",
    "validate_api_base",
    "validate_query",
    "validate_slug",
    "version_sort_key",
]

#: Stable ``where`` marker for every diagnostic this adapter emits.
WHERE = "gleam.core.sources.repology"

#: Operator-configured trusted base (the config domain owns
#: the live value through ``GLEAM_REPOLOGY_BASE``).
DEFAULT_REPOLOGY_BASE = "https://repology.org"

#: Action selector values (part of the frozen tool schema).
REPOLOGY_ACTIONS = ("search", "project")

#: Hard input bounds (frozen, part of the tool schema; never policy).
QUERY_MAX_CHARS = 200
SLUG_MAX_CHARS = 200

#: Digest caps (design values).
SEARCH_PROJECTS_CAP = 12
LADDER_TIER_CAP = 15
TIER_REPOS_CAP = 8

#: Fixed request header, not configurable.
REPOLOGY_FIXED_HEADERS = {"Accept": "application/json"}

#: Stable marker recorded in diagnostics provenance.
REPOLOGY_POLICY_VERSION = "repology-adapter"

#: Internal policy dictionary (live values arrive from the config
#: domain at call time; these defaults exist for standalone testing).
DEFAULT_POLICY = {
    "max_requests": 6,       # per client instance (BTS precedent)
    "min_interval_s": 1.1,   # API terms require <=1 request/second
    "timeout_s": 15.0,
}

SEARCH_NOTE = (
    "search results are unordered and may omit the exact slug (the API "
    "caps results); when exact_match is false but the name looks "
    "canonical, try action=project with the plain name; statuses are "
    "relative to the globally newest version"
)
PROJECT_NOTE = (
    "statuses are relative to the globally newest version across all "
    "repositories; repos lists mark debian_* repositories with a "
    "'deb:' prefix"
)
EMPTY_PROJECT_NOTE = "200 with an empty entry list means no such project"


class RepologyError(Exception):
    """Internal fetch error with a stable ``kind`` label (BtsError
    style). Kind vocabulary: budget-exhausted / transport-error /
    timeout / http-error / http-4xx / http-404 / rate-limited /
    parse-error. Never crosses the public adapter boundary:
    :meth:`RepologyClient.query` converts every failure into a failed
    :class:`RepologyResult`; only usage mistakes raise the public
    ``UsageError`` before any network activity.
    """

    def __init__(self, kind: str, message: str):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message


class RepologyArgError(UsageError, ValueError):
    """Deterministic caller mistake: bad action / query / slug /
    api base. Doubly rooted on purpose (UsageError for the exit-2
    entry path, ValueError for value-validation callers)."""

    exit_code = 2


@dataclass(frozen=True)
class RepologyPolicy:
    """Numeric bounds for one client (live values come from
    the config domain at call time -- these defaults exist only for
    standalone testing). The base URL is deliberately absent: it is a
    trusted-endpoint value, not policy."""

    max_requests: int = 6
    min_interval_s: float = 1.1
    timeout_s: float = 15.0


# ---------------------------------------------------------------------------
# Input validation and URL assembly (the only place raw text enters a URL)
# ---------------------------------------------------------------------------

def validate_api_base(base_url: str) -> urllib.parse.SplitResult:
    """Validate the trusted API base (the config domain owns the
    value; this is defense in depth for direct library callers)."""

    if not isinstance(base_url, str):
        raise RepologyArgError(
            f"api base must be a string, got {type(base_url).__name__}")
    parsed = urllib.parse.urlsplit(base_url.strip())
    if parsed.scheme.lower() not in ("http", "https"):
        raise RepologyArgError(
            f"api base scheme must be http(s): {base_url!r}")
    if parsed.username is not None or parsed.password is not None:
        raise RepologyArgError("api base must not contain credentials")
    if not parsed.hostname:
        raise RepologyArgError(f"api base has no host: {base_url!r}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise RepologyArgError(f"api base port invalid: {exc}") from exc
    if port is not None and not (1 <= port <= 65535):
        raise RepologyArgError("api base port out of range")
    return parsed


def validate_query(query) -> str:
    """Search query: a non-empty string of at most 200 chars."""

    if not isinstance(query, str) or not query.strip():
        raise RepologyArgError(
            f"query must be a non-empty string, got {query!r}")
    if len(query) > QUERY_MAX_CHARS:
        raise RepologyArgError(
            f"query too long (max {QUERY_MAX_CHARS} chars)")
    return query


def validate_slug(slug) -> str:
    """Project slug: a non-empty string of at most 200 chars (Repology
    slugs may contain ``prefix:name`` forms; the value is always
    percent-encoded into the path by :func:`build_project_url`)."""

    if not isinstance(slug, str) or not slug.strip():
        raise RepologyArgError(
            f"slug must be a non-empty string, got {slug!r}")
    if len(slug) > SLUG_MAX_CHARS:
        raise RepologyArgError(
            f"slug too long (max {SLUG_MAX_CHARS} chars)")
    return slug


def build_project_url(base: str, slug: str) -> str:
    """``GET {base}/api/v1/project/{slug}``; the slug is percent-
    encoded with nothing safe (``/`` and ``:`` can never inject path
    structure)."""

    validate_slug(slug)
    parsed = validate_api_base(base)
    root = urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, parsed.path.rstrip("/"),
         "", "")).rstrip("/")
    return root + "/api/v1/project/" + urllib.parse.quote(slug, safe="")


def build_search_url(base: str, query: str) -> str:
    """``GET {base}/api/v1/projects/?search={urlencoded query}``."""

    validate_query(query)
    parsed = validate_api_base(base)
    root = urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, parsed.path.rstrip("/"),
         "", "")).rstrip("/")
    return root + "/api/v1/projects/?" + urllib.parse.urlencode(
        {"search": query})


# ---------------------------------------------------------------------------
# Version comparator (pure) and digests (pure)
# ---------------------------------------------------------------------------

_VERSION_TOKEN_RE = re.compile(r"\d+|\D+")

#: Sorts below every version that contains a digit: no-digit version
#: strings are "unknown formats" and fall to the tail of the DESCENDING
#: ladder (newest first), keeping first-seen order among equals via
#: stable sorting.
_UNKNOWN_FORMAT_KEY: tuple[tuple[int, int, str], ...] = ((-1, 0, ""),)


def version_sort_key(version: str) -> tuple[tuple[int, int, str], ...]:
    """Tolerant dotted-numeric ordering key (pure function).

    Each dot-separated part is tokenized into digit / non-digit runs;
    numeric tokens compare numerically (``2.9.2 < 2.10`` and
    ``1.22 > 1.9`` -- never lexicographic), a non-numeric token sorts
    after a numeric token at the same position, a shorter key sorts
    before its extensions (``1.22 < 1.22a``), and a version containing
    no digit at all is an unknown format that sorts below everything
    numeric -- so it lands at the tail of the descending ladder in
    first-seen order (stable sort keeps equals in arrival order).
    A version whose FIRST token is non-numeric (for example a
    ``v``-prefixed VCS snapshot such as ``v2.8.2.r0.g5b96a48``) is an
    unknown format too: ranking it by its numeric tail would place
    noise above real releases, and ranking it above numeric keys would
    put it at the head of the descending ladder -- both wrong, so it
    joins the tail bucket.
    """

    text = str(version)
    if not any(ch.isdigit() for ch in text):
        return _UNKNOWN_FORMAT_KEY
    key = []
    for part in text.split("."):
        for token in _VERSION_TOKEN_RE.findall(part):
            if token.isdigit():
                key.append((0, int(token), ""))
            else:
                key.append((1, 0, token.lower()))
    if not key or key[0][0] != 0:
        return _UNKNOWN_FORMAT_KEY
    return tuple(key)


def _newest_entry(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """The newest-status entry, else the first (design rule for the
    search digest's display fields); an empty entry list yields the
    empty mapping so the display fields fall back to the slug."""

    for entry in entries:
        if entry.get("status") == "newest":
            return entry
    return entries[0] if entries else {}


def search_digest(query: str,
                  projects: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Digest of ``GET /api/v1/projects/?search=...``: the returned
    mapping (slug -> entry list) becomes at most
    :data:`SEARCH_PROJECTS_CAP` project rows plus a ``more`` count;
    ``exact_match`` is the case-insensitive presence of the query
    itself among the returned slugs. ``slug`` echoes the query (the
    looked-up name) per the digest contract."""

    rows = []
    slugs = list(projects)
    for slug in slugs[:SEARCH_PROJECTS_CAP]:
        entries = projects[slug]
        pick = _newest_entry(entries)
        rows.append({
            "slug": slug,
            "visiblename": pick.get("visiblename") or slug,
            "newest_version": pick.get("version") or "",
            "repo_count": len(entries),
        })
    return {
        "action": "search",
        "query": query,
        "slug": query,
        "exact_match": any(slug.lower() == query.lower()
                           for slug in slugs),
        "projects": rows,
        "more": max(0, len(slugs) - SEARCH_PROJECTS_CAP),
        "total_entries": sum(len(projects[slug]) for slug in slugs),
        "note": SEARCH_NOTE,
    }


def _repos_list(entries: list[dict[str, Any]]) -> list[str]:
    """Unique repo names in first-seen order, debian_* marked with the
    ``deb:`` prefix, capped at :data:`TIER_REPOS_CAP` names plus one
    ``+K more`` marker."""

    names: list[str] = []
    for entry in entries:
        repo = str(entry.get("repo", ""))
        name = "deb:" + repo if repo.startswith("debian") else repo
        if name not in names:
            names.append(name)
    if len(names) > TIER_REPOS_CAP:
        hidden = len(names) - TIER_REPOS_CAP
        return names[:TIER_REPOS_CAP] + [f"+{hidden} more"]
    return names


def _tier(version: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": version,
        "repo_count": len(entries),
        "repos": _repos_list(entries),
        "statuses": sorted({str(e.get("status", "")) for e in entries}),
        "vulnerable_repos": sum(1 for e in entries if e.get("vulnerable")),
    }


def project_digest(slug: str,
                   entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Digest of ``GET /api/v1/project/{slug}``: the version ladder.

    Entries are grouped by the stripped ``version`` (never
    ``origversion``), tiers sort newest-first by
    :func:`version_sort_key` (stable: equal keys keep first-seen
    order), the ladder is capped at :data:`LADDER_TIER_CAP` tiers and
    the remainder aggregates into one disclosed tier; the ``debian``
    block lists every debian_* entry (repo + version, all of them)."""

    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        groups.setdefault(str(entry.get("version", "")), []).append(entry)
    ordered = sorted(groups.items(),
                     key=lambda kv: version_sort_key(kv[0]),
                     reverse=True)
    ladder = [_tier(version, group) for version, group in
              ordered[:LADDER_TIER_CAP]]
    remainder = ordered[LADDER_TIER_CAP:]
    if remainder:
        rest = [entry for _, group in remainder for entry in group]
        ladder.append({
            "version": f"({len(remainder)} older versions)",
            "repo_count": len(rest),
            "repos": _repos_list(rest),
            "statuses": sorted({str(e.get("status", ""))
                                for e in rest}),
            "vulnerable_repos": sum(1 for e in rest
                                    if e.get("vulnerable")),
        })
    return {
        "action": "project",
        "slug": slug,
        "total_entries": len(entries),
        "versions": ladder,
        "debian": [{"repo": e.get("repo"), "version": e.get("version")}
                   for e in entries
                   if str(e.get("repo", "")).startswith("debian")],
        "note": EMPTY_PROJECT_NOTE if not entries else PROJECT_NOTE,
    }


# ---------------------------------------------------------------------------
# Typed result and client
# ---------------------------------------------------------------------------

def _reject_constant(name: str) -> float:
    raise ValueError(f"non-finite JSON constant {name}")


def _parse_float_strict(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"non-finite JSON number {text}")
    return value


def _loads_strict(text: str):
    """Strict JSON (NaN/Infinity/overflow rejected), github-client
    style local copy."""

    return json.loads(text, parse_constant=_reject_constant,
                      parse_float=_parse_float_strict)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class RepologyResult:
    """Typed adapter result for one query.

    ``status`` is adapter-level: ``ok`` (digest usable, including the
    honest empty project answer) or ``failed`` (``errors`` non-empty;
    every failure means UNKNOWN, never a verdict about the project).
    ``digest`` carries the computed search/project summary --
    never raw arrays; ``body`` keeps the raw response bytes for
    observation archival by the managed entry; ``to_data()`` returns
    exactly the digest (the CLI Result data and the model tool payload
    carry the same shape).
    """

    status: str
    action: str
    query: str | None = None
    slug: str | None = None
    api_url: str = ""
    final_url: str | None = None
    http_status: int | None = None
    digest: dict[str, Any] = field(default_factory=dict)
    content_sha256: str = ""
    content_bytes: int = 0
    body: bytes = b""
    note: str = ""
    robots: dict[str, Any] | None = None
    errors: list[DiagnosticItem] = field(default_factory=list)
    diagnostics: list[DiagnosticItem] = field(default_factory=list)
    warnings: list[DiagnosticItem] = field(default_factory=list)
    fetched_at: str = ""
    duration_s: float = 0.0

    def to_data(self) -> dict[str, Any]:
        """The ``repology`` data field set (the digest itself)."""
        return dict(self.digest)


class RepologyClient:
    """Two read-only Repology API queries over the public fetcher.

    Constructor shape (stable for the repology entry and the
    investigate ``repology`` tool): ``RepologyClient(base=...,
    policy=None, fetch_policy=None, operator_exempt_hosts=frozenset(),
    budget_check=None, verbose=None)``. The internal :class:`Fetcher`
    is created here with the tool-intrinsic robots exemption (operator
    set UNION the configured base host); one request budget per client
    instance (``request_count`` exposes the reservation count); no
    fetcher can be injected. ``verbose`` (a ``make_verbose`` callable)
    is passed straight through to that fetcher together with the
    exemption's narration label -- the client itself narrates nothing.
    ``budget_check`` (BTS wiring) is invoked before every request and
    may veto it by raising :class:`RepologyError`.
    """

    def __init__(self, base: str = DEFAULT_REPOLOGY_BASE,
                 policy: RepologyPolicy | None = None,
                 fetch_policy: dict[str, Any] | None = None,
                 operator_exempt_hosts: frozenset[str] = frozenset(),
                 budget_check=None,
                 verbose: Callable[[str], None] | None = None):
        parsed = validate_api_base(base)
        self.base = base.strip().rstrip("/")
        self.policy = dict(DEFAULT_POLICY)
        if policy is not None:
            self.policy.update({
                "max_requests": int(policy.max_requests),
                "min_interval_s": float(policy.min_interval_s),
                "timeout_s": float(policy.timeout_s),
            })
        self.request_count = 0
        self._budget_check = budget_check
        merged_fetch_policy = {
            "timeout_s": float(self.policy["timeout_s"]),
            # API terms require <=1 request/second: the same-host
            # throttle rides the internal fetcher's own policy.
            "min_interval_s": float(self.policy["min_interval_s"]),
            "max_requests": int(self.policy["max_requests"]),
        }
        if fetch_policy:
            merged_fetch_policy.update(fetch_policy)
        # Tool-intrinsic robots exemption:
        # Repology's own API terms authorize programmatic access on
        # this API surface despite the blanket robots disallow -- the
        # tool IS the specific permission. The union of the operator
        # set and the configured base host lives ONLY in this client's
        # fetcher; the investigation's main fetcher (manual fetch_url)
        # keeps exactly the operator-file exemption set.
        base_host = (parsed.hostname or "").lower()
        self._fetcher = transport.Fetcher(
            merged_fetch_policy,
            exempt_hosts=(frozenset(
                str(host).lower() for host in operator_exempt_hosts)
                | {base_host}),
            verbose=verbose,
            exempt_note="tool-intrinsic, API terms")

    # -- budget (BTS precedent) -----------------------------------------

    @property
    def fetcher_policy(self) -> dict[str, Any]:
        """The internal fetcher's effective policy (disclosure view:
        carries this client's own ``robots_exempt`` list for the
        per-fetch audit record)."""

        return dict(self._fetcher.policy)

    def _reserve(self, what: str) -> None:
        if self._budget_check is not None:
            self._budget_check(what)  # may raise RepologyError
        cap = int(self.policy["max_requests"])
        if self.request_count >= cap:
            raise RepologyError(
                "budget-exhausted",
                f"request cap {cap} reached before {what}")
        self.request_count += 1

    # -- one structured query --------------------------------------------

    def query(self, action: str, query: str | None = None,
              slug: str | None = None) -> RepologyResult:
        """Fetch one action. Usage mistakes raise
        :class:`RepologyArgError` before any network call; every fetch
        failure is returned as a failed :class:`RepologyResult`."""

        if action not in REPOLOGY_ACTIONS:
            raise RepologyArgError(
                f"action must be one of {list(REPOLOGY_ACTIONS)}; "
                f"got {action!r}")
        if action == "search":
            query = validate_query(query)
            url = build_search_url(self.base, query)
            what = f"search {query!r}"
        else:
            slug = validate_slug(slug)
            url = build_project_url(self.base, slug)
            what = f"project {slug!r}"
        base: dict[str, Any] = dict(
            action=action,
            query=query if action == "search" else None,
            slug=slug if action == "project" else None,
            api_url=url,
        )
        started = time.monotonic()
        fetched_at = _utc_now()
        try:
            self._reserve(what)
        except RepologyError as exc:
            return self._failed(base, exc, fetched_at, 0.0)
        outcome = self._fetcher.fetch(
            url, headers=dict(REPOLOGY_FIXED_HEADERS))
        duration_s = time.monotonic() - started
        base.update(
            final_url=outcome.final_url,
            http_status=outcome.status,
            content_sha256=outcome.content_sha256 or "",
            content_bytes=outcome.content_bytes or 0,
            body=outcome.body or b"",
            # the transport's per-fetch robots verdict rides the typed
            # result so the managed caller's observation record shows
            # the exempt path (disclosure requirement)
            robots=outcome.robots,
        )
        if not outcome.ok:
            return self._transport_failure(base, outcome, fetched_at,
                                           duration_s)
        # ok implies a completed exchange: the status is an int (the
        # ``or 0`` only satisfies the Optional annotation)
        status = outcome.status or 0
        if status == 429:
            return self._rate_limited(base, fetched_at, duration_s)
        if status != 200:
            return self._http_error(base, status, fetched_at,
                                    duration_s)
        return self._success(base, action, fetched_at, duration_s)

    # -- grading helpers --------------------------------------------------

    def _error_item(self, code: str, message: str,
                    base: dict[str, Any]) \
            -> DiagnosticItem:
        return DiagnosticItem(
            code=code, message=message, where=WHERE,
            details={"api_url": base["api_url"],
                     "http_status": base.get("http_status")})

    def _failed(self, base: dict[str, Any], exc: RepologyError,
                fetched_at: str,
                duration_s: float) -> RepologyResult:
        return RepologyResult(
            status="failed", errors=[self._error_item(
                exc.kind, exc.message, base)],
            note="failure means unknown; never 'no results' and never "
                 "proof that the project is absent",
            fetched_at=fetched_at, duration_s=duration_s, **base)

    def _transport_failure(self, base: dict[str, Any], outcome,
                           fetched_at: str, duration_s: float) \
            -> RepologyResult:
        error = outcome.error or {"kind": "transport-error",
                                  "message": "?"}
        item = DiagnosticItem(
            code=f"fetch-{error.get('kind', 'unknown')}",
            message=str(error.get("message", "fetch failed")),
            where=WHERE,
            details={"api_url": base["api_url"],
                     "fetch_error": error},
        )
        return RepologyResult(
            status="failed", errors=[item],
            note="transport/robots failure means unknown; never proof "
                 "that the project is absent",
            fetched_at=fetched_at, duration_s=duration_s, **base)

    def _rate_limited(self, base: dict[str, Any], fetched_at: str,
                      duration_s: float) -> RepologyResult:
        message = ("HTTP 429 from Repology API; no automatic retry "
                   "(min-interval policy already paces requests)")
        return RepologyResult(
            status="failed", errors=[self._error_item(
                "rate-limited", message, base)],
            note="rate limit means unknown; never 'no results'",
            fetched_at=fetched_at, duration_s=duration_s, **base)

    def _http_error(self, base: dict[str, Any], status: int,
                     fetched_at: str,
                    duration_s: float) -> RepologyResult:
        code = "http-404" if status == 404 else (
            "http-4xx" if 400 <= status < 500 else "http-error")
        note = ("404 means unknown (unexpected from this API, which "
                "answers unknown projects with 200 and an empty "
                "list); never graded as 'no such project'"
                if status == 404 else
                f"HTTP {status} means unknown; never 'no results'")
        return RepologyResult(
            status="failed", errors=[self._error_item(
                code, f"HTTP {status} from Repology API", base)],
            note=note, fetched_at=fetched_at, duration_s=duration_s,
            **base)

    def _malformed(self, base: dict[str, Any], message: str,
                    fetched_at: str,
                   duration_s: float) -> RepologyResult:
        return RepologyResult(
            status="failed", errors=[self._error_item(
                "malformed-json", message, base)],
            note="malformed response means unknown; never 'no results'",
            fetched_at=fetched_at, duration_s=duration_s, **base)

    def _success(self, base: dict[str, Any], action: str,
                 fetched_at: str,
                 duration_s: float) -> RepologyResult:
        try:
            parsed = _loads_strict(base["body"].decode("utf-8", "replace"))
        except (json.JSONDecodeError, ValueError,
                UnicodeDecodeError) as exc:
            return self._malformed(
                base, f"Repology response is not valid strict JSON: {exc}",
                fetched_at, duration_s)
        if action == "search":
            if not isinstance(parsed, dict) or not all(
                    isinstance(value, list)
                    and all(isinstance(entry, dict) for entry in value)
                    for value in parsed.values()):
                return self._malformed(
                    base, "search response is not a JSON object of "
                          "slug -> entry arrays "
                          "(expected {slug: [entries...]})",
                    fetched_at, duration_s)
            digest = search_digest(base["query"], parsed)
        else:
            if not isinstance(parsed, list) or not all(
                    isinstance(entry, dict) for entry in parsed):
                return self._malformed(
                    base, "project response is not a JSON array of "
                          "per-repo entry objects",
                    fetched_at, duration_s)
            digest = project_digest(base["slug"], parsed)
        return RepologyResult(
            status="ok", digest=digest,
            note="cross-distro packaging facts (Repology); weaker than "
                 "fetched page content for identity claims",
            fetched_at=fetched_at, duration_s=duration_s, **base)
