"""GitHub REST API 工具辅助：路径构造（模型不可控 base/path）、响应整形、
Link 分页校验、限流头暴露。

安全模型：
- API base 仅来自操作员配置（--github-api-base / GLEAM_GITHUB_API_BASE，
  默认 https://api.github.com）；模型参数只有结构化 action/owner/repo/
  page/per_page——owner/repo 严格白名单字符校验，路径由本模块拼接，
  模型无法注入任意 API 路径或 base。
- 请求本身经 net.Fetcher（公网路径：SSRF/DNS 钉扎/robots/字节与请求
  预算全复用）；固定头 Accept: application/vnd.github+json 与
  X-GitHub-Api-Version: 2022-11-28 由本模块写死，User-Agent 沿用
  Fetcher 策略。无认证（无 secret 可查、可暴露）。
- 403/429：如实暴露限流头（x-ratelimit-limit/remaining/reset、
  retry-after）与原因；不自动重试。
- 404（尤其 /releases）：明确"未知"——可能是仓库不存在/私有/改名或
  （releases）确无发布，绝不据此推断项目死亡或无发布。
- 分页：解析 Link rel="next" 仅当其 origin/path 前缀与本次请求一致才
  采纳为 next_page 页码；绝不自动跟随（每页由模型显式请求，且受
  工具/请求预算约束）。
- API 事实（html_url/homepage/发布/提交时间）只是 GitHub 侧记录，
  不构成上游身份的官方证明——身份判断留给人工复核。
"""

from __future__ import annotations

import re
import urllib.parse

GITHUB_ACTIONS = ("repo", "readme", "releases", "commits")
DEFAULT_GITHUB_API_BASE = "https://api.github.com"
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REPO_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}$")
PER_PAGE_DEFAULT = 10
PER_PAGE_MAX = 100
PAGE_MAX = 100
README_TEXT_CAP = 12000

GITHUB_FIXED_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


class GithubArgError(ValueError):
    pass


def validate_owner(owner) -> str:
    if not isinstance(owner, str) or not OWNER_RE.match(owner or ""):
        raise GithubArgError(
            f"owner must match {OWNER_RE.pattern} (GitHub login charset); "
            f"got {owner!r}")
    return owner


def validate_repo(repo) -> str:
    if not isinstance(repo, str) or not REPO_RE.match(repo or "") \
            or ".." in repo:
        raise GithubArgError(
            f"repo must match {REPO_RE.pattern} without '..'; got {repo!r}")
    return repo


def validate_api_base(base_url: str) -> str:
    """CLI 侧校验（创建任何运行目录之前）；返回错误信息或空串。"""

    try:
        validate_github_base(base_url)
    except ValueError as exc:
        return str(exc)
    return ""


def validate_github_base(base_url: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlsplit((base_url or "").strip())
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError(f"github api base scheme must be http(s): "
                         f"{base_url!r}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("github api base must not contain credentials")
    if not parsed.hostname:
        raise ValueError(f"github api base has no host: {base_url!r}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"github api base port invalid: {exc}") from exc
    if port is not None and not (1 <= port <= 65535):
        raise ValueError("github api base port out of range")
    return parsed


def build_github_url(api_base: str, action: str, owner: str, repo: str,
                     page: int | None = None,
                     per_page: int | None = None) -> str:
    """构造请求 URL：路径白名单拼接，模型无法传入任意 path/base。"""

    if action not in GITHUB_ACTIONS:
        raise GithubArgError(
            f"action must be one of {list(GITHUB_ACTIONS)}; got {action!r}")
    validate_owner(owner)
    validate_repo(repo)
    parsed = validate_github_base(api_base)
    path = (parsed.path.rstrip("/") + f"/repos/{owner}/{repo}")
    if action == "readme":
        path += "/readme"
    elif action == "releases":
        path += "/releases"
    elif action == "commits":
        path += "/commits"
    query = {}
    if action in ("releases", "commits"):
        if page is not None:
            if isinstance(page, bool) or not isinstance(page, int) \
                    or not (1 <= page <= PAGE_MAX):
                raise GithubArgError(
                    f"page must be an int in [1, {PAGE_MAX}]")
            query["page"] = page
        if per_page is not None:
            if isinstance(per_page, bool) or not isinstance(per_page, int) \
                    or not (1 <= per_page <= PER_PAGE_MAX):
                raise GithubArgError(
                    f"per_page must be an int in [1, {PER_PAGE_MAX}]")
            query["per_page"] = per_page
    netloc = parsed.netloc
    url = urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, path,
                                   urllib.parse.urlencode(query), ""))
    return url


def _http_url(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        port = parsed.port  # 非数字/越界端口在属性访问时才抛 ValueError
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None  # 带凭据 URL 不入候选集（与 extract.py 口径一致）
    if port is not None and not (1 <= port <= 65535):
        return None
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, parsed.path or "/",
         parsed.query, ""))


def rate_limit_info(headers: dict) -> dict | None:
    """从最终响应头提取限流信息；无相关头返回 None。"""

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
    """解析 Link rel="next" 的 page 参数；仅当同 origin 且路径同前缀才采纳。

    绝不自动跟随：返回页码供模型显式请求下一页；恶意/跨源 Link 丢弃。
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
            next_port = next_parsed.port  # 越界端口在此抛 ValueError
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
        # 段感知前缀："/repos/o/r" 不得匹配 "/repos/o/rx/..."
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


def shape_repo(payload: dict) -> tuple[dict, list[str]]:
    """repo 视图 + 候选证据 URL（html_url/homepage 等）。"""

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
    urls = [u for u in (_http_url(payload.get("html_url")),
                        _http_url(payload.get("homepage"))) if u]
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
        url = _http_url(item.get("html_url"))
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
        url = _http_url(item.get("html_url"))
        if url:
            urls.append(url)
    return {"commits": commits, "count": len(commits)}, urls
