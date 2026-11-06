"""受信任的本地 SearXNG 搜索适配器（与公网 Fetcher 严格分离的代码路径）。

信任模型（与 models.py 的受信任本地模型端点一致）：
- base URL 仅来自操作员配置（--search-base / GLEAM_SEARCH_BASE，默认
  http://127.0.0.1:8888），绝不来自模型参数、网页或 CSV 数据。
- 校验：scheme 限 http(s)、拒绝 URL 内凭据、host 必须存在；请求路径
  （/search）与查询参数由本模块构造，不接受调用方传入任意 path。
- 直连 http.client（不读代理环境变量、不走公网 Fetcher 的 SSRF 校验——
  目标本就是操作员指定的本地/可信服务）；**不跟随重定向**：任何 3xx
  一律按 unexpected-redirect 显式报错（含 Location 诊断），绝不静默
  跟到任意 origin。
- 有界：响应字节上限、总超时（连接+读取共享同一 deadline）。
- 返回值诚实分级：成功 / 空结果但引擎有响应 / 全引擎失败或空（歧义，
  显式不等于"无结果"）/ captcha / 403（JSON 未启用）/ 429 限流 /
  畸形 JSON / 连接错误。绝不伪造结果。

结果规范化（保守、无信息发明）：
- 只保留 http(s) 绝对 URL；去 fragment；按规范化 URL 去重保序（保留
  首次出现的名次，合并 engine 归属）。
- title/snippet 去 HTML 标签（SearXNG 高亮 <b> 等），仅做标签剥离与
  实体反转，不生成新句子；剥离后为空的字段按空串保留。
- 逐结果保留 engine 归属；响应的 unresponsive_engines 原样透传。

本模块不做 robots 判定：目标是操作员自托管的本机服务（非第三方爬取）。
第三方引擎的抓取发生在 SearXNG 容器内，由其自身策略负责；本仓不在
客户端绕过任何 robots 或 captcha。
"""

from __future__ import annotations

import http.client
import json
import math
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser

from . import models as models_mod

DEFAULT_SEARCH_BASE = "http://127.0.0.1:8888"
SEARCH_TIMEOUT_S = 10.0
SEARCH_MAX_BYTES = 1 << 20  # 1 MiB：JSON 搜索响应足够且防失控
DEFAULT_SEARCH_ENGINES = "searxng"


class _TagStripper(HTMLParser):
    """仅保留可见文本（转义实体按 convert_charrefs 还原）。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data):
        self.parts.append(data)


def strip_html(text: str) -> str:
    """剥离 HTML 标签并还原实体；空白压缩。非字符串输入原样返回。"""

    if not isinstance(text, str):
        return ""
    stripper = _TagStripper()
    try:
        stripper.feed(text)
        stripper.close()
    except Exception:
        return " ".join(text.split())
    return " ".join("".join(stripper.parts).split())


def canonical_url(url) -> str | None:
    """规范化结果 URL：仅 http(s) 绝对 URL、去 fragment；其余返回 None。"""

    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parsed = urllib.parse.urlsplit(url.strip())
        port = parsed.port  # 畸形端口在此抛 ValueError
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None  # 结果 URL 内嵌凭据：不作为候选
    if port is not None and not (1 <= port <= 65535):
        return None
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if port:
        netloc = f"{host}:{port}"
    else:
        netloc = host
    return urllib.parse.urlunsplit(
        (scheme, netloc, parsed.path or "/", parsed.query, ""))


@dataclass
class SearchResponse:
    """search() 的结果：ok=True 时 payload/results 可用；否则 error 分级。"""

    ok: bool
    status: int | None = None
    final_url: str | None = None
    raw_body: bytes = b""
    error: dict | None = None
    query: str = ""
    results: list = field(default_factory=list)       # 规范化后的结果
    unresponsive_engines: list = field(default_factory=list)
    number_of_results: float | None = None
    deduped_count: int = 0
    rendered_text: str = ""                            # 证据渲染全文


def render_results_text(query: str, results: list,
                        unresponsive_engines: list) -> str:
    """把结果渲染为确定性文本（read_evidence / 引用校验的全文来源）。

    每条结果一行标题 + 一行 URL + snippet + 归属引擎；末尾附 unresponsive。
    """

    lines = [f"search results for query: {query}"]
    for i, item in enumerate(results, 1):
        lines.append(f"{i}. {item['title']}")
        lines.append(f"   url: {item['url']}")
        if item.get("snippet"):
            lines.append(f"   snippet: {item['snippet']}")
        engines = ", ".join(item.get("engines") or [])
        lines.append(f"   engines: {engines}")
    if unresponsive_engines:
        lines.append("unresponsive engines: "
                     + json.dumps(unresponsive_engines, ensure_ascii=False))
    else:
        lines.append("unresponsive engines: none reported")
    lines.append("note: snippets are third-party search summaries, weaker "
                 "evidence than fetched page content; fetch before "
                 "concluding identity")
    return "\n".join(lines)


class SearxngSearch:
    """对配置的 SearXNG 实例做一次有界 GET /search?format=json。"""

    def __init__(self, base_url: str = DEFAULT_SEARCH_BASE,
                 timeout_s: float = SEARCH_TIMEOUT_S,
                 max_bytes: int = SEARCH_MAX_BYTES,
                 user_agent: str = "gleam-search/0.1 (local searxng client)"):
        self.base_url = (base_url or DEFAULT_SEARCH_BASE).strip()
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme.lower() not in ("http", "https"):
            raise ValueError(f"search base scheme must be http(s): "
                             f"{self.base_url!r}")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("search base must not contain credentials")
        self.scheme = parsed.scheme.lower()
        self.host = parsed.hostname
        if not self.host:
            raise ValueError(f"search base has no host: {self.base_url!r}")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError(f"search base port invalid: {exc}") from exc
        self.port = port or (443 if self.scheme == "https" else 80)
        self.base_path = parsed.path.rstrip("/")
        self.timeout_s = float(timeout_s)
        self.max_bytes = int(max_bytes)
        self.user_agent = user_agent

    # -- 请求 --------------------------------------------------------------

    def request_url(self, query: str) -> str:
        qs = urllib.parse.urlencode({"q": query, "format": "json",
                                     "language": "en"})
        return (f"{self.scheme}://{self.host}:{self.port}{self.base_path}"
                f"/search?{qs}")

    def _request(self, query: str) -> tuple[int, dict, bytes]:
        """一次有界 GET；不跟随重定向（3xx 由调用方按错误分级）。"""

        path = self.base_path + "/search?" + urllib.parse.urlencode(
            {"q": query, "format": "json", "language": "en"})
        if self.scheme == "https":
            conn = http.client.HTTPSConnection(self.host, self.port,
                                               timeout=self.timeout_s)
        else:
            conn = http.client.HTTPConnection(self.host, self.port,
                                              timeout=self.timeout_s)
        deadline = time.monotonic() + self.timeout_s
        try:
            conn.request("GET", path, headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
                "Connection": "close",
            })
            response = conn.getresponse()
            status = response.status
            headers = {}
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
                if total + len(chunk) > self.max_bytes:
                    chunks.append(chunk[:self.max_bytes - total])
                    break  # 截断的 JSON 会按畸形分级——显式而非静默
                chunks.append(chunk)
                total += len(chunk)
            return status, headers, b"".join(chunks)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # -- 响应解析 ----------------------------------------------------------

    def search(self, query: str) -> SearchResponse:
        url = self.request_url(query)
        base = SearchResponse(ok=False, query=query, final_url=url)
        try:
            status, headers, body = self._request(query)
        except (TimeoutError, OSError, http.client.HTTPException) as exc:
            # BadStatusLine/IncompleteRead 等是 HTTPException 而非 OSError：
            # 显式按 connection-error 分级（保留原始异常信息），绝不外溢
            base.error = {"kind": "timeout" if isinstance(
                exc, TimeoutError) else "connection-error",
                "message": f"{type(exc).__name__}: {exc}"}
            return base
        base.status, base.raw_body = status, body
        if status in (301, 302, 303, 307, 308):
            location = (headers.get("location") or [None])[0]
            # 信任边界：绝不跟随重定向；显式拒绝任意 origin
            base.error = {
                "kind": "unexpected-redirect",
                "message": f"search endpoint returned {status}; redirects "
                           f"are not followed (trusted base is pinned); "
                           f"location={location!r}"}
            return base
        if status == 403:
            base.error = {"kind": "format-forbidden",
                          "message": "HTTP 403: JSON format likely not "
                                     "enabled on this SearXNG instance "
                                     "(search.formats must include json)"}
            return base
        if status == 429:
            base.error = {"kind": "rate-limited",
                          "message": "HTTP 429 rate limited",
                          "retry_after": (headers.get("retry-after")
                                          or [None])[0]}
            return base
        if status >= 500:
            base.error = {"kind": "http-5xx",
                          "message": f"HTTP {status} from search endpoint"}
            return base
        if status >= 400:
            base.error = {"kind": "http-error",
                          "message": f"HTTP {status} from search endpoint"}
            return base
        try:
            parsed = models_mod._loads_strict(
                body.decode("utf-8", "replace"))
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
            base.error = {"kind": "malformed-json",
                          "message": f"search response is not valid strict "
                                     f"JSON: {exc}"}
            return base
        if not isinstance(parsed, dict) or not isinstance(
                parsed.get("results"), list):
            base.error = {"kind": "malformed-json",
                          "message": "search JSON missing results array"}
            return base

        raw_results = parsed.get("results") or []
        unresponsive = self._norm_unresponsive(
            parsed.get("unresponsive_engines"))
        normalized, deduped = self._normalize_results(raw_results)
        base.ok = True
        base.results = normalized
        base.deduped_count = deduped
        base.unresponsive_engines = unresponsive
        number = parsed.get("number_of_results")
        # 非有限值（NaN/Infinity，无论以何途径混入）一律归 None，不得外泄
        base.number_of_results = (float(number)
                                  if isinstance(number, (int, float))
                                  and not isinstance(number, bool)
                                  and math.isfinite(number) else None)
        base.rendered_text = render_results_text(query, normalized, unresponsive)
        # 歧义显式化：0 结果 + 有引擎失败 ≠ "确认无结果"
        if not normalized and unresponsive:
            base.error = {
                "kind": "all-engines-failed-or-empty",
                "message": "no results and some engines failed; this is "
                           "ambiguous (cannot prove 'no results'); "
                           "unresponsive engines are listed",
                "unresponsive_engines": unresponsive}
        elif not normalized:
            base.error = {
                "kind": "no-results",
                "message": "engines responded but returned no results for "
                           "this query"}
        captcha_engines = [e for e in unresponsive
                           if isinstance(e, list) and len(e) > 1
                           and "captcha" in str(e[1]).lower()]
        if captcha_engines:
            base.error = base.error or {"kind": "captcha-reported",
                                        "message": "some engines reported "
                                                   "captcha; listed in "
                                                   "unresponsive_engines"}
            if base.ok and not base.results:
                base.ok = False
        return base

    @staticmethod
    def _norm_unresponsive(raw) -> list:
        out = []
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, (list, tuple)) and item:
                    out.append([str(x) for x in item[:2]])
                elif item is not None:
                    out.append([str(item)])
        return out

    @staticmethod
    def _normalize_results(raw_results: list) -> tuple[list, int]:
        """去重保序（保名次）、规范化 URL、剥离 HTML、合并引擎归属。"""

        by_url: dict[str, dict] = {}
        order: list[str] = []
        deduped = 0
        for raw in raw_results:
            if not isinstance(raw, dict):
                deduped += 1
                continue
            url = canonical_url(raw.get("url"))
            if url is None:
                deduped += 1
                continue
            engines = raw.get("engines")
            if not isinstance(engines, list):
                engines = ([engines] if isinstance(engines, str) else [])
            entry = {
                "title": strip_html(raw.get("title")),
                "url": url,
                "snippet": strip_html(raw.get("content")),
                "engines": [str(e) for e in engines if isinstance(
                    e, str) and e] or (
                    [str(raw["engine"])] if isinstance(
                        raw.get("engine"), str) and raw["engine"] else []),
            }
            if url in by_url:
                # 重复 URL：合并引擎归属，保留首见名次与字段
                existing = by_url[url]
                for engine in entry["engines"]:
                    if engine not in existing["engines"]:
                        existing["engines"].append(engine)
                if not existing["title"] and entry["title"]:
                    existing["title"] = entry["title"]
                if not existing["snippet"] and entry["snippet"]:
                    existing["snippet"] = entry["snippet"]
                deduped += 1
                continue
            by_url[url] = entry
            order.append(url)
        return [by_url[u] for u in order], deduped


def validate_search_base(base_url: str) -> str:
    """CLI 侧校验（创建任何运行目录之前）；返回规范化错误信息或空串。"""

    try:
        SearxngSearch(base_url)
    except ValueError as exc:
        return str(exc)
    return ""
