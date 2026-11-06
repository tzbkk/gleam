"""Typed GitHub source adapter -- structured REST queries with frozen
semantics.

Public surface (consumed by the investigate github tool; stable
constructor shape): ``GithubClient(api_base=..., policy=None,
fetch_policy=None)`` with per-action methods ``repo`` / ``readme`` /
``releases`` / ``commits`` / ``forks`` (all delegating to ``query``),
returning a typed :class:`GithubResult`.

Contract summary:
- actions are exactly ``repo`` / ``readme`` / ``releases`` /
  ``commits`` / ``forks``; ``owner``/``repo`` pass frozen
  allowlist validation and
  the request path is ALWAYS assembled by the application (a
  model-controlled path string never exists);
- ``page``/``per_page`` are hard-bounded to 1..100 and
  validated before any network call;
- ``Link rel="next"`` becomes a page number ONLY for same-origin,
  same-path-prefix targets, returned as ``next_page`` metadata for the
  next explicit call -- never followed automatically;
- requests ride the transport public fetcher (full public-URL
  validation chain) with the fixed headers
  ``Accept: application/vnd.github+json`` and
  ``X-GitHub-Api-Version: 2022-11-28``; there is no authentication;
- 403/429 surface ``x-ratelimit-*`` / ``retry-after`` in the typed
  result and never auto-retry; 404 is an explicit unknown (the repo
  may be private/renamed -- never inferred dead); ``html_url`` /
  ``homepage`` are extracted as candidate evidence URLs;
- the API base is an operator-configured trusted endpoint passed in
  as a parameter (the config domain owns the value), routed strictly
  separately from public probe URLs.

Usage mistakes raise :class:`GithubArgError` (a ``UsageError`` AND a
``ValueError``): the entry maps exit 2, and value-validation callers
keep their except clause. Tests patch
``gleam.core.sources.github.client.transport`` (module boundary);
no injection seams exist.
"""

from __future__ import annotations

from .client import GithubClient, GithubResult, WHERE
from .policy import (
    DEFAULT_GITHUB_API_BASE,
    GITHUB_ACTIONS,
    GITHUB_FIXED_HEADERS,
    GITHUB_POLICY_VERSION,
    GithubArgError,
    GithubPolicy,
    OWNER_RE,
    PAGE_MAX,
    PER_PAGE_DEFAULT,
    PER_PAGE_MAX,
    README_TEXT_CAP,
    REPO_RE,
    build_github_url,
    candidate_http_url,
    parse_next_page,
    rate_limit_info,
    validate_api_base,
    validate_owner,
    validate_repo,
)
from .shapes import (
    shape_commits,
    shape_forks,
    shape_readme_view,
    shape_releases,
    shape_repo,
)

__all__ = [
    "DEFAULT_GITHUB_API_BASE",
    "GITHUB_ACTIONS",
    "GITHUB_FIXED_HEADERS",
    "GITHUB_POLICY_VERSION",
    "GithubArgError",
    "GithubClient",
    "GithubPolicy",
    "GithubResult",
    "OWNER_RE",
    "PAGE_MAX",
    "PER_PAGE_DEFAULT",
    "PER_PAGE_MAX",
    "README_TEXT_CAP",
    "REPO_RE",
    "WHERE",
    "build_github_url",
    "candidate_http_url",
    "parse_next_page",
    "rate_limit_info",
    "shape_commits",
    "shape_forks",
    "shape_readme_view",
    "shape_releases",
    "shape_repo",
    "validate_api_base",
    "validate_owner",
    "validate_repo",
]
