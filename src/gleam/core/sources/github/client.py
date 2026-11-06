"""Typed GitHub client: one structured REST query per call, riding the
transport domain's public fetcher.

Trusted-base routing: the API base comes from the operator via the
config domain (constructor parameter, default ``https://api.github.com``)
and is kept strictly separate from public probe URLs -- yet every
request still goes through the FULL public-URL validation chain
(SSRF / DNS pinning / per-hop robots / byte and request caps) because
the fetch is performed by :class:`gleam.core.transport.Fetcher`. The
fixed headers ``Accept: application/vnd.github+json`` and
``X-GitHub-Api-Version: 2022-11-28`` are written by this module only;
there is no authentication and no token plumbing anywhere.

Honest epistemic grading (README data semantics: failure = unknown):
- 403/429 surface the ``x-ratelimit-*`` / ``retry-after`` headers in
  the typed result and NEVER retry automatically;
- 404 is an explicit unknown (the repository may be private, renamed,
  or without releases) -- never proof of death;
- transport/robots failures are unknowns, not verdicts;
- a 200 whose body is not strict JSON, or whose shape does not match
  the action, is an explicit ``malformed-json`` failure.

Pagination: ``Link rel="next"`` is parsed into a page number ONLY for
same-origin, same-path-prefix targets and returned as ``next_page``
metadata for the next EXPLICIT call -- it is never followed
automatically.

Usage mistakes (bad action/owner/repo, out-of-range page/per_page,
malformed api base) raise :class:`GithubArgError` (a ``UsageError``,
exit 2 at the entry) BEFORE any network call.

Tests patch ``gleam.core.sources.github.client.transport`` (the module
boundary); no injection seams exist.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from gleam.core import transport
from gleam.core.result import DiagnosticItem

from .policy import (
    GITHUB_FIXED_HEADERS,
    GITHUB_POLICY_VERSION,
    DEFAULT_GITHUB_API_BASE,
    PER_PAGE_MAX,
    GithubArgError,
    GithubPolicy,
    build_github_url,
    parse_next_page,
    rate_limit_info,
    validate_api_base,
)
from .shapes import (
    shape_commits,
    shape_forks,
    shape_readme_view,
    shape_releases,
    shape_repo,
)

__all__ = [
    "GITHUB_POLICY_VERSION",
    "GithubClient",
    "GithubResult",
]

#: Stable ``where`` marker for every diagnostic this adapter emits.
WHERE = "gleam.core.sources.github"


def _reject_constant(name: str) -> float:
    raise ValueError(f"non-finite JSON constant {name}")


def _parse_float_strict(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):  # overflowed literals like 1e999
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
class GithubResult:
    """Typed adapter result for one structured query.

    ``status`` is adapter-level: ``ok`` (view usable) or ``failed``
    (``errors`` non-empty; every failure means UNKNOWN, never a
    verdict about the repository). ``view`` carries the shaped payload
    for the action; ``evidence_urls`` are the candidate http(s) URLs
    extracted from ``html_url``/``homepage`` fields (hosting-account
    records, weaker than fetched page content). ``next_page`` is
    Link-derived metadata for the next explicit call. ``body`` keeps
    the raw response bytes for observation archival by the managed
    entry -- ``to_data()`` never emits raw shapes.
    """

    status: str
    action: str
    owner: str
    repo: str
    page: int | None = None
    per_page: int | None = None
    api_url: str = ""
    final_url: str | None = None
    http_status: int | None = None
    view: dict = field(default_factory=dict)
    evidence_urls: list[str] = field(default_factory=list)
    next_page: int | None = None
    rate_limit: dict | None = None
    content_sha256: str = ""
    content_bytes: int = 0
    body: bytes = b""
    note: str = ""
    errors: list[DiagnosticItem] = field(default_factory=list)
    diagnostics: list[DiagnosticItem] = field(default_factory=list)
    warnings: list[DiagnosticItem] = field(default_factory=list)
    fetched_at: str = ""
    duration_s: float = 0.0

    def to_data(self) -> dict[str, Any]:
        """Stable data field set; raw bytes stay out."""
        return {
            "action": self.action,
            "owner": self.owner,
            "repo": self.repo,
            "page": self.page,
            "per_page": self.per_page,
            "api_url": self.api_url,
            "final_url": self.final_url,
            "http_status": self.http_status,
            "view": self.view,
            "next_page": self.next_page,
            "rate_limit": self.rate_limit,
            "evidence_urls": list(self.evidence_urls),
            "content_sha256": self.content_sha256,
            "content_bytes": self.content_bytes,
            "note": self.note,
        }


class GithubClient:
    """Structured GitHub REST queries over the public fetcher.

    Constructor shape (stable for the investigate entry / github
    tool): ``GithubClient(api_base=..., policy=None, fetch_policy=None,
    verbose=None)``. ``api_base`` is the operator-configured trusted
    endpoint; ``policy`` carries the configurable defaults;
    ``fetch_policy`` is a plain dict of fetcher policy overrides
    (config data, not a transport object). ``verbose`` is the caller's
    narration sink, passed straight through to the internal fetcher
    (the caller's prefix rides the callable; this client hardcodes no
    module label). The internal :class:`Fetcher` is created here --
    one request budget per client instance (``request_count`` exposes
    the observation point); no fetcher can be injected.
    """

    def __init__(self, api_base: str = DEFAULT_GITHUB_API_BASE,
                 policy: GithubPolicy | None = None,
                 fetch_policy: dict | None = None,
                 verbose: Callable[[str], None] | None = None):
        validate_api_base(api_base)
        self.api_base = api_base
        self.policy = policy or GithubPolicy()
        self._fetcher = transport.Fetcher(fetch_policy, verbose=verbose)

    @property
    def request_count(self) -> int:
        """HTTP requests issued through the internal fetcher so far
        (robots fetches included, as counted by the transport domain)."""

        return self._fetcher.request_count

    # -- per-action convenience surface --------------------------------

    def repo(self, owner: str, repo: str, page: int | None = None,
             per_page: int | None = None) -> GithubResult:
        return self.query("repo", owner, repo, page=page,
                          per_page=per_page)

    def readme(self, owner: str, repo: str, page: int | None = None,
               per_page: int | None = None) -> GithubResult:
        return self.query("readme", owner, repo, page=page,
                          per_page=per_page)

    def releases(self, owner: str, repo: str, page: int | None = None,
                 per_page: int | None = None) -> GithubResult:
        return self.query("releases", owner, repo, page=page,
                          per_page=per_page)

    def commits(self, owner: str, repo: str, page: int | None = None,
                per_page: int | None = None) -> GithubResult:
        return self.query("commits", owner, repo, page=page,
                          per_page=per_page)

    def forks(self, owner: str, repo: str, page: int | None = None,
              per_page: int | None = None) -> GithubResult:
        return self.query("forks", owner, repo, page=page,
                          per_page=per_page)

    # -- one structured query ------------------------------------------

    def query(self, action: str, owner: str, repo: str,
              page: int | None = None,
              per_page: int | None = None) -> GithubResult:
        """Fetch one action for ``owner/repo``. ``page``/``per_page``
        are validated whenever supplied (hard 1..100) and defaulted
        only for the paginated actions (``forks``
        defaults to ``per_page=100`` so one call covers the fork
        liveness rubric). Usage mistakes raise
        :class:`GithubArgError` before any network call."""

        if action in ("releases", "commits", "forks"):
            if page is None:
                page = 1
            if per_page is None:
                per_page = (PER_PAGE_MAX if action == "forks"
                            else self.policy.per_page_default)
        url = build_github_url(self.api_base, action, owner, repo,
                               page, per_page)
        started = time.monotonic()
        fetched_at = _utc_now()
        outcome = self._fetcher.fetch(url, headers=dict(GITHUB_FIXED_HEADERS))
        duration_s = time.monotonic() - started
        headers = outcome.hops[-1].headers if outcome.hops else {}
        rate = rate_limit_info(headers)
        base = dict(
            action=action, owner=owner, repo=repo, page=page,
            per_page=per_page, api_url=url, final_url=outcome.final_url,
            http_status=outcome.status, fetched_at=fetched_at,
            duration_s=duration_s, rate_limit=rate,
            content_sha256=outcome.content_sha256 or "",
            content_bytes=outcome.content_bytes or 0,
            body=outcome.body or b"",
        )
        if not outcome.ok:
            return self._transport_failure(base, outcome)
        if outcome.status == 404:
            return self._unknown_404(base)
        if outcome.status in (403, 429):
            return self._rate_limited(base, outcome.status)
        if outcome.status == 200:
            return self._success(base, action, url, headers)
        return self._http_error(base, outcome.status)

    # -- grading helpers ------------------------------------------------

    def _transport_failure(self, base: dict, outcome) -> GithubResult:
        error = outcome.error or {"kind": "transport-error",
                                  "message": "?"}
        item = DiagnosticItem(
            code=f"fetch-{error.get('kind', 'unknown')}",
            message=str(error.get("message", "fetch failed")),
            where=WHERE,
            details={"api_url": base["api_url"], "fetch_error": error},
        )
        return GithubResult(
            status="failed", note="transport/robots failure means unknown; "
            "never proof that the repository is dead",
            errors=[item], **base)

    def _unknown_404(self, base: dict) -> GithubResult:
        note = ("404 means unknown: the repository may not exist, may be "
                "private or renamed, or (releases) may simply have none; "
                "NOT proof of death and NOT proof of no releases")
        item = DiagnosticItem(
            code="http-404", message="404 from GitHub API", where=WHERE,
            details={"api_url": base["api_url"],
                     "http_status": base["http_status"], "note": note},
        )
        return GithubResult(status="failed", note=note, errors=[item],
                            **base)

    def _rate_limited(self, base: dict, status: int) -> GithubResult:
        code = "rate-limited" if status == 429 else "http-403"
        message = (f"HTTP {status}: rate limit or forbidden "
                   f"(unauthenticated); see rate_limit headers "
                   f"(retry_after / reset); no automatic retry")
        item = DiagnosticItem(
            code=code, message=message, where=WHERE,
            details={"api_url": base["api_url"], "http_status": status,
                     "rate_limit": base["rate_limit"] or {}},
        )
        return GithubResult(status="failed", note=message, errors=[item],
                            **base)

    def _http_error(self, base: dict, status: int) -> GithubResult:
        item = DiagnosticItem(
            code="http-error", message=f"unexpected HTTP {status}",
            where=WHERE,
            details={"api_url": base["api_url"], "http_status": status},
        )
        return GithubResult(
            status="failed",
            note="unexpected HTTP status means unknown; never proof that "
            "the repository is dead",
            errors=[item], **base)

    def _malformed(self, base: dict, message: str) -> GithubResult:
        item = DiagnosticItem(
            code="malformed-json", message=message, where=WHERE,
            details={"api_url": base["api_url"],
                     "http_status": base["http_status"]},
        )
        return GithubResult(status="failed", errors=[item], **base)

    def _success(self, base: dict, action: str, url: str,
                 headers: dict) -> GithubResult:
        try:
            parsed = _loads_strict(base["body"].decode("utf-8", "replace"))
        except (json.JSONDecodeError, ValueError,
                UnicodeDecodeError) as exc:
            return self._malformed(
                base, f"GitHub response is not valid strict JSON: {exc}")
        view: dict = {}
        evidence_urls: list[str] = []
        if action == "repo":
            view, evidence_urls = shape_repo(parsed)
        elif action == "releases":
            view, evidence_urls = shape_releases(parsed)
        elif action == "commits":
            view, evidence_urls = shape_commits(parsed)
        elif action == "forks":
            view, evidence_urls = shape_forks(parsed)
        elif action == "readme":
            if not isinstance(parsed, dict):
                return self._malformed(
                    base, "GitHub readme response is not a JSON object; "
                    "cannot decode README")
            text, mode = transport.decode_readme(
                parsed, self.policy.readme_text_cap)
            view, evidence_urls = shape_readme_view(parsed, text, mode)
        if isinstance(view, dict) and set(view) == {"error"}:
            # Wrong payload shape for the action (repo got a non-object,
            # releases/commits/forks got a non-array): an honest failure --
            # never disguised as success data.
            return self._malformed(base, str(view["error"]))
        next_page = None
        link_values = headers.get("link") or []
        next_page = parse_next_page(
            link_values[0] if link_values else None, url)
        if next_page:
            view["next_page"] = next_page
            view["pagination_note"] = (
                "more pages exist; request them explicitly with page=N "
                "(bounded); not followed automatically")
        # The identity-dissent note rides as prose only (a clean
        # success must stay ok; diagnostics would downgrade it).
        return GithubResult(
            status="ok", view=view, evidence_urls=evidence_urls,
            next_page=next_page,
            note="GitHub API facts are hosting-account records, not "
            "official-identity proof; verify by fetching pages when "
            "needed",
            **base)
