"""Frozen GitHub adapter policy: argument validation, URL assembly,
Link pagination parsing, and rate-limit header extraction. The
``forks`` action (`/repos/{owner}/{repo}/forks?sort=newest`, pushed-at
ordering) is part of the frozen action set.

Security model (trusted-endpoint routing): the API base is an
operator-configured trusted endpoint whose VALUE is owned by the config
domain and passed in as a constructor parameter -- the adapter treats
it as trusted-base routing, strictly separate from public probe URLs.
The REQUESTS themselves still ride the full public fetcher validation
chain (SSRF / DNS pinning / per-hop robots / byte and request caps) in
the transport domain.

Model-controlled input is limited to the structured
action/owner/repo/page/per_page fields: ``owner``/``repo`` pass strict
allowlist character validation and the request path is ALWAYS assembled
by :func:`build_github_url` -- a model-controlled path string never
exists anywhere in the code path.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

from gleam.core.result import UsageError

__all__ = [
    "DEFAULT_GITHUB_API_BASE",
    "GITHUB_ACTIONS",
    "GITHUB_FIXED_HEADERS",
    "GITHUB_POLICY_VERSION",
    "GithubArgError",
    "GithubPolicy",
    "OWNER_RE",
    "PAGE_MAX",
    "PER_PAGE_DEFAULT",
    "PER_PAGE_MAX",
    "README_TEXT_CAP",
    "REPO_RE",
    "build_github_url",
    "candidate_http_url",
    "parse_next_page",
    "rate_limit_info",
    "validate_api_base",
    "validate_owner",
    "validate_repo",
]

GITHUB_ACTIONS = ("repo", "readme", "releases", "commits", "forks")
DEFAULT_GITHUB_API_BASE = "https://api.github.com"
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REPO_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}$")
PER_PAGE_DEFAULT = 10
PER_PAGE_MAX = 100
PAGE_MAX = 100
README_TEXT_CAP = 12000

#: Fixed request headers, not configurable. No authentication
#: exists anywhere in this adapter -- there is no token to leak.
GITHUB_FIXED_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

#: Stable marker recorded in diagnostics provenance.
GITHUB_POLICY_VERSION = "github-adapter"


class GithubArgError(UsageError, ValueError):
    """Deterministic caller mistake: bad action / owner / repo / page /
    per_page / api base.

    Doubly rooted on purpose: it IS a usage error (the entry maps
    ``UsageError`` to exit 2) and it also satisfies callers
    that catch ``ValueError`` for argument validation failures -- both
    exception families hold for the same event.
    """

    exit_code = 2


@dataclass(frozen=True)
class GithubPolicy:
    """Configurable defaults for one client: the live values
    come from the config domain at call time
    (``GLEAM_GITHUB_PER_PAGE`` / ``GLEAM_GITHUB_README_TEXT_CAP``);
    these dataclass defaults exist only for standalone testing. The
    ``page <= 100`` / ``per_page <= 100`` hard bounds are NOT here and
    NOT configurable -- they are frozen constants above.
    """

    per_page_default: int = PER_PAGE_DEFAULT
    readme_text_cap: int = README_TEXT_CAP


def validate_owner(owner) -> str:
    """GitHub login charset allowlist (frozen regex)."""

    if not isinstance(owner, str) or not OWNER_RE.match(owner or ""):
        raise GithubArgError(
            f"owner must match {OWNER_RE.pattern} (GitHub login charset); "
            f"got {owner!r}")
    return owner


def validate_repo(repo) -> str:
    """GitHub repository name allowlist (frozen regex); the
    ``..`` sequence is additionally rejected so path traversal can
    never survive into an assembled URL."""

    if not isinstance(repo, str) or not REPO_RE.match(repo or "") \
            or ".." in repo:
        raise GithubArgError(
            f"repo must match {REPO_RE.pattern} without '..'; got {repo!r}")
    return repo


def validate_api_base(base_url: str) -> urllib.parse.ParseResult:
    """Validate the trusted API base (the config domain owns the value;
    this is defense in depth for direct library callers)."""

    if not isinstance(base_url, str):
        raise GithubArgError(
            f"api_base must be a string, got {type(base_url).__name__}")
    parsed = urllib.parse.urlsplit(base_url.strip())
    if parsed.scheme.lower() not in ("http", "https"):
        raise GithubArgError(f"github api base scheme must be http(s): "
                             f"{base_url!r}")
    if parsed.username is not None or parsed.password is not None:
        raise GithubArgError("github api base must not contain credentials")
    if not parsed.hostname:
        raise GithubArgError(f"github api base has no host: {base_url!r}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise GithubArgError(f"github api base port invalid: {exc}") from exc
    if port is not None and not (1 <= port <= 65535):
        raise GithubArgError("github api base port out of range")
    return parsed


def build_github_url(api_base: str, action: str, owner: str, repo: str,
                     page: int | None = None,
                     per_page: int | None = None) -> str:
    """Assemble the request URL: whitelisted path segments joined by
    this function only -- the caller can never pass an arbitrary path
    or base. ``page``/``per_page`` are validated for EVERY action when
    supplied (the hard 1..100 bound is action-independent) but
    are only SENT for the paginated ``releases``/``commits``/``forks``
    actions. ``forks`` always carries the
    frozen ``sort=newest`` (pushed-at ordering) and defaults to
    ``per_page=100`` at the client so one call covers the liveness
    rubric; pagination stays explicit via ``next_page``.
    """

    if action not in GITHUB_ACTIONS:
        raise GithubArgError(
            f"action must be one of {list(GITHUB_ACTIONS)}; got {action!r}")
    validate_owner(owner)
    validate_repo(repo)
    parsed = validate_api_base(api_base)
    path = (parsed.path.rstrip("/") + f"/repos/{owner}/{repo}")
    if action == "readme":
        path += "/readme"
    elif action == "releases":
        path += "/releases"
    elif action == "commits":
        path += "/commits"
    elif action == "forks":
        path += "/forks"
    query = {}
    if action == "forks":
        query["sort"] = "newest"
    if page is not None:
        if isinstance(page, bool) or not isinstance(page, int) \
                or not (1 <= page <= PAGE_MAX):
            raise GithubArgError(
                f"page must be an int in [1, {PAGE_MAX}]; got {page!r}")
        if action in ("releases", "commits", "forks"):
            query["page"] = page
    if per_page is not None:
        if isinstance(per_page, bool) or not isinstance(per_page, int) \
                or not (1 <= per_page <= PER_PAGE_MAX):
            raise GithubArgError(
                f"per_page must be an int in [1, {PER_PAGE_MAX}]; "
                f"got {per_page!r}")
        if action in ("releases", "commits", "forks"):
            query["per_page"] = per_page
    url = urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc, path,
                                   urllib.parse.urlencode(query), ""))
    return url


def candidate_http_url(value) -> str | None:
    """Filter a payload string into a candidate evidence URL: only
    absolute http(s) without credentials and with a sane port survive
    (fragments are dropped); same criteria as the extraction layer."""

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        port = parsed.port  # malformed ports raise on attribute access
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None  # credentialed URLs never enter the candidate set
    if port is not None and not (1 <= port <= 65535):
        return None
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, parsed.path or "/",
         parsed.query, ""))


def rate_limit_info(headers: dict) -> dict | None:
    """Extract rate-limit facts from the final response headers;
    ``None`` when no relevant header is present."""

    if not isinstance(headers, dict):
        return None

    def first(name):
        values = headers.get(name)
        return values[0] if isinstance(values, list) and values else None

    keys = ("x-ratelimit-limit", "x-ratelimit-remaining",
            "x-ratelimit-reset", "retry-after", "x-ratelimit-used")
    info = {k: first(k) for k in keys}
    return info if any(v is not None for v in info.values()) else None


def parse_next_page(link_header, request_url: str) -> int | None:
    """Parse the ``page`` parameter of a ``Link rel="next"`` header.

    The page number is adopted ONLY when the Link target shares the
    request's origin (scheme/host/port) AND sits under the request's
    path prefix (segment-aware: ``/repos/o/r`` must not match
    ``/repos/o/rx/...``). Anything else -- cross-origin targets,
    malformed URLs, out-of-bound pages -- is silently dropped. The
    returned number is metadata for the NEXT EXPLICIT call; it is
    never followed automatically.
    """

    if not isinstance(link_header, str) or not link_header:
        return None
    for part in link_header.split(","):
        fields = part.split(";")
        if len(fields) < 2:
            continue
        url_field = fields[0].strip()
        rel = ";".join(fields[1:]).strip().lower()
        if 'rel="next"' not in rel and "'next'" not in rel:
            continue
        if not (url_field.startswith("<") and url_field.endswith(">")):
            continue
        try:
            next_parsed = urllib.parse.urlsplit(url_field[1:-1])
            req_parsed = urllib.parse.urlsplit(request_url)
            next_port = next_parsed.port  # bad ports raise here
            req_port = req_parsed.port
        except ValueError:
            return None
        same_origin = (next_parsed.scheme.lower() == req_parsed.scheme.lower()
                       and (next_parsed.hostname or "").lower()
                       == (req_parsed.hostname or "").lower()
                       and next_port == req_port)
        if not same_origin:
            continue
        prefix = (req_parsed.path or "/").rsplit("/", 1)[0] or "/"
        next_path = next_parsed.path or ""
        if next_path != prefix and not next_path.startswith(prefix + "/"):
            continue
        try:
            params = urllib.parse.parse_qs(next_parsed.query)
            page_values = params.get("page")
            if page_values:
                page = int(page_values[0])
                if 1 <= page <= PAGE_MAX:
                    return page
        except (ValueError, TypeError):
            return None
    return None
