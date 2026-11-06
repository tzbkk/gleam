"""Pure payload shapers for the GitHub adapter: API JSON -> honest
view dicts plus candidate evidence URLs (html_url / homepage etc.).

Every shaper is a total function over untrusted JSON: wrong shapes
produce an ``{"error": ...}`` view and no evidence (the client converts
that to an explicit ``malformed-json`` failure -- never a fabricated
success). API facts are records of one hosting account, not official
proof of upstream identity; the ``note`` on the repo view says so.
"""

from __future__ import annotations

from .policy import PER_PAGE_MAX, candidate_http_url

__all__ = [
    "shape_commits",
    "shape_forks",
    "shape_readme_view",
    "shape_releases",
    "shape_repo",
]


def shape_repo(payload: dict) -> tuple[dict, list[str]]:
    """Repository view + candidate evidence URLs (html_url/homepage)."""

    if not isinstance(payload, dict):
        return {"error": "repo response is not an object"}, []
    license_ = payload.get("license") or {}
    owner = payload.get("owner") or {}
    view = {
        "full_name": payload.get("full_name"),
        "html_url": payload.get("html_url"),
        "homepage": payload.get("homepage") or None,
        "description": payload.get("description"),
        "archived": bool(payload.get("archived")),
        "disabled": bool(payload.get("disabled")),
        "fork": bool(payload.get("fork")),
        "created_at": payload.get("created_at"),
        "updated_at": payload.get("updated_at"),
        "pushed_at": payload.get("pushed_at"),
        "default_branch": payload.get("default_branch"),
        "language": payload.get("language"),
        "stargazers_count": payload.get("stargazers_count"),
        "owner_login": owner.get("login") if isinstance(owner, dict) else None,
        "license_spdx": license_.get("spdx_id")
        if isinstance(license_, dict) else None,
        "note": "GitHub API facts are records of one hosting account; not "
                "official proof of upstream identity",
    }
    urls = [u for u in (candidate_http_url(payload.get("html_url")),
                        candidate_http_url(payload.get("homepage"))) if u]
    return view, urls


def shape_releases(payload) -> tuple[dict, list[str]]:
    if not isinstance(payload, list):
        return {"error": "releases response is not an array"}, []
    releases = []
    urls = []
    for item in payload[:PER_PAGE_MAX]:
        if not isinstance(item, dict):
            continue
        author = item.get("author") or {}
        releases.append({
            "tag_name": item.get("tag_name"),
            "name": item.get("name"),
            "published_at": item.get("published_at"),
            "created_at": item.get("created_at"),
            "prerelease": bool(item.get("prerelease")),
            "author_login": author.get("login")
            if isinstance(author, dict) else None,
            "html_url": item.get("html_url"),
        })
        url = candidate_http_url(item.get("html_url"))
        if url:
            urls.append(url)
    return {"releases": releases, "count": len(releases)}, urls


def shape_commits(payload) -> tuple[dict, list[str]]:
    if not isinstance(payload, list):
        return {"error": "commits response is not an array"}, []
    commits = []
    urls = []
    for item in payload[:PER_PAGE_MAX]:
        if not isinstance(item, dict):
            continue
        commit = item.get("commit") or {}
        author_block = commit.get("author") or {} \
            if isinstance(commit, dict) else {}
        author = item.get("author") or {}
        sha = item.get("sha") or ""
        view = {
            "sha": sha[:12] if isinstance(sha, str) else None,
            "commit_author_name": author_block.get("name"),
            "commit_author_date": author_block.get("date"),
            "committer_date": (commit.get("committer") or {}).get("date")
            if isinstance(commit.get("committer"), dict) else None,
            "message_first_line": (
                str(commit.get("message") or "").splitlines() or [""])[0],
            "author_login": author.get("login")
            if isinstance(author, dict) else None,
            "html_url": item.get("html_url"),
        }
        commits.append(view)
        url = candidate_http_url(item.get("html_url"))
        if url:
            urls.append(url)
    return {"commits": commits, "count": len(commits)}, urls


def shape_forks(payload) -> tuple[dict, list[str]]:
    """Fork-list view: per fork the liveness rubric signals --
    ``pushed_at``/``created_at`` (single-day-pulse vs pure-mirror
    fingerprints) and ``stargazers_count`` -- plus identity fields.
    The API returns them sorted by most recently pushed first
    (``sort=newest`` is frozen in the URL). Every fork's
    ``html_url`` becomes a candidate evidence URL (anti-forgery: the
    model may only cite URLs a tool result actually showed)."""

    if not isinstance(payload, list):
        return {"error": "forks response is not an array"}, []
    forks = []
    urls = []
    for item in payload[:PER_PAGE_MAX]:
        if not isinstance(item, dict):
            continue
        forks.append({
            "full_name": item.get("full_name"),
            "html_url": item.get("html_url"),
            "description": item.get("description"),
            "fork": bool(item.get("fork")),
            "pushed_at": item.get("pushed_at"),
            "created_at": item.get("created_at"),
            "stargazers_count": item.get("stargazers_count"),
        })
        url = candidate_http_url(item.get("html_url"))
        if url:
            urls.append(url)
    return {"forks": forks, "count": len(forks)}, urls


def shape_readme_view(payload: dict, readme_text: str, decoding: str) \
        -> tuple[dict, list[str]]:
    """Readme view from an already-decoded payload (the client runs
    :func:`gleam.core.transport.decode_readme` first because the text
    cap is policy-driven)."""

    html_url = candidate_http_url(payload.get("html_url"))
    view = {
        "readme_text": readme_text,
        "decoding": decoding,
        "path": payload.get("path"),
        "sha": payload.get("sha"),
        "size": payload.get("size"),
        "html_url": payload.get("html_url"),
        "provenance_note": (
            "readme fetched via /repos/{owner}/{repo}/readme (default "
            "branch); contents provenance as returned by API "
            f"({decoding})"),
    }
    return view, ([html_url] if html_url else [])
