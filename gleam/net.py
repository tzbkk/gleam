"""有界只读 HTTP(S) 观测探针：SSRF 防护、DNS 重绑定防护、重定向逐跳校验、robots 尊重。

安全模型（对应 docs/research/09 §4 与 06 §3）：
- 仅 http(s)，拒绝 userinfo/凭据；每一跳重定向都重新走完整校验。
- 先 socket.getaddrinfo 解析并验证为公网单播地址，拒绝环回/内网/链路本地/
  保留/组播段；随后用 http.client 连接"钉住的 IP"，TLS 仍按原主机名做
  SNI + 证书校验（DNS 重绑定防护）。绝不使用代理环境变量（http.client
  不读 http_proxy，代理出口会绕过本校验）。
- 上限：重定向跳数、响应体字节、每次请求超时、整个 Fetcher 的请求总数、
  同主机最小间隔。
- robots.txt：同策略抓取并缓存；disallow / 获取失败（unknown）/ 401/403/5xx
  一律弃权（ABSTAIN）；404/410 视为无限制。
- 本模块仅处理"不可信目标 URL"。受信任的本地模型客户端（操作员显式配置）
  在 models.py，二者不共享代码路径。
- 探针错误 = 未知，不是死亡判决；字节哈希变化只是表示层变化，不是活性结论。
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import socket
import ssl
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field

ALLOWED_SCHEMES = ("http", "https")
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
DEFAULT_POLICY = {
    "max_bytes": 262144,
    "timeout_s": 15.0,
    "max_redirects": 3,
    "max_requests": 10,
    "min_interval_s": 1.0,
    "robots_max_bytes": 65536,
    "user_agent": "gleam-probe/0.1 (read-only evidence collector; +https://qa.debian.org)",
}

# RFC 6052 众所周知 NAT64 前缀：内嵌 IPv4 位于低 32 位
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")


class FetchError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message


@dataclass
class HopResult:
    url: str
    status: int | None = None
    pinned_ip: str | None = None
    headers: dict = field(default_factory=dict)
    body: bytes = b""
    body_truncated: bool = False
    error: "FetchError | None" = None  # 已取得响应后的读失败：保留部分响应


def _public_address(host: str) -> str:
    """解析 host 并返回第一个通过公网校验的地址；全不合规则抛 FetchError。"""

    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FetchError("dns-error", f"resolution failed for {host}: {exc}") from exc
    problems = []
    for info in infos:
        raw = info[4][0]
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            problems.append(f"unparseable {raw}")
            continue
        if isinstance(addr, ipaddress.IPv6Address):
            embedded = (addr.ipv4_mapped            # ::ffff:0:0/96
                        or addr.sixtofour           # 2002::/16（6to4）
                        or (ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
                            if addr in _NAT64_PREFIX else None))
            if embedded is not None:
                addr = embedded  # 拆掉 v4 过渡包装再验，防止借道绕过
        if (addr.is_loopback or addr.is_private or addr.is_link_local
                or addr.is_multicast or addr.is_reserved or addr.is_unspecified
                or not addr.is_global):
            problems.append(f"non-public {raw}")
            continue
        return str(addr)
    raise FetchError("unsafe-destination", "no public address for host "
                     + host + "; rejected: " + "; ".join(problems[:4]))


def _validate_url(url: str) -> urllib.parse.ParseResult:
    """校验 URL 形态；一切畸形（含 IPv6/端口解析 ValueError）统一转 FetchError，
    单个坏端点不得以未捕获异常中断整轮运行。"""

    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname  # 畸形 IPv6 字面量在此抛 ValueError
        port = parsed.port      # 非数字/越界端口在此抛 ValueError
    except ValueError as exc:
        raise FetchError("unsafe-destination", f"malformed URL: {exc}") from exc
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise FetchError("unsafe-destination",
                         f"scheme {parsed.scheme!r} not in {ALLOWED_SCHEMES}")
    # 统一小写一次：origin_key/端口默认/连接工厂均为大小写敏感比较
    parsed = parsed._replace(scheme=parsed.scheme.lower())
    if parsed.username is not None or parsed.password is not None:
        raise FetchError("unsafe-destination", "credentials in URL are not allowed")
    if not host:
        raise FetchError("unsafe-destination", "URL has no hostname")
    if port is not None and not (1 <= port <= 65535):
        raise FetchError("unsafe-destination",
                         f"port {port!r} out of range 1-65535")
    return parsed


def origin_key(url_or_parsed) -> str | None:
    """规范化 origin 键（scheme://host:port）；畸形输入返回 None。"""

    try:
        parsed = (_validate_url(url_or_parsed)
                  if isinstance(url_or_parsed, str) else url_or_parsed)
    except FetchError:
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return "%s://%s:%d" % (parsed.scheme.lower(), parsed.hostname, port)


def robots_raw_name(key: str) -> str:
    """robots 原文的落盘名：对 origin 键取哈希，IDN/特殊字符主机名安全。"""

    return "robots-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16] + ".txt"


class PinnedHTTPConnection(http.client.HTTPConnection):
    """连接钉住的 IP；Host 头与虚拟主机仍用原主机名（HTTP 层不变）。"""

    def __init__(self, host: str, port: int, pinned_ip: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self):
        self.sock = socket.create_connection((self._pinned_ip, self.port),
                                             timeout=self.timeout)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """连接钉住的 IP；TLS 的 SNI 与主机名校验仍针对原主机名（防 DNS 重绑定）。"""

    def __init__(self, host: str, port: int, pinned_ip: str, timeout: float,
                 context: ssl.SSLContext):
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip

    def connect(self):
        raw = socket.create_connection((self._pinned_ip, self.port),
                                       timeout=self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


_SSL_CONTEXT: ssl.SSLContext | None = None


def _ssl_context() -> ssl.SSLContext:
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        _SSL_CONTEXT = ssl.create_default_context()
    return _SSL_CONTEXT


def _open_connection(scheme: str, host: str, port: int, pinned_ip: str,
                     timeout: float):
    """连接工厂；测试通过替换本函数注入假连接。"""

    if scheme == "https":
        return PinnedHTTPSConnection(host, port, pinned_ip, timeout,
                                     _ssl_context())
    return PinnedHTTPConnection(host, port, pinned_ip, timeout)


def _response_headers(response) -> dict:
    out = {}
    try:
        for key, value in response.getheaders():
            out.setdefault(key.lower(), []).append(value)
    except Exception:  # 某些伪响应对象无头可读
        pass
    return out


def _single_request(url: str, timeout: float, max_bytes: int, user_agent: str,
                    method: str = "GET",
                    extra_headers: dict | None = None) -> HopResult:
    parsed = _validate_url(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    pinned_ip = _public_address(host)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    conn = _open_connection(parsed.scheme, host, port, pinned_ip, timeout)
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
        "Accept-Language": "en",
        "Connection": "close",
    }
    # extra_headers 仅由工具代码写死（如 GitHub API 版本头）传入；
    # 绝不来自模型参数，模型无法注入任意头
    for key, value in (extra_headers or {}).items():
        headers[str(key)] = str(value)
    try:
        conn.request(method, path, headers=headers)
        response = conn.getresponse()
        status = response.status
        headers = _response_headers(response)
        deadline = time.monotonic() + timeout
        chunks, total, truncated = [], 0, False
        body_error: FetchError | None = None
        # 响应已开始：此后任何读失败都保留已取得的 status/headers/部分 body
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FetchError("timeout",
                                     f"body read deadline exceeded for {url}")
                if conn.sock is not None:
                    conn.sock.settimeout(remaining)
                chunk = response.read(8192)
                if not chunk:
                    break
                if total + len(chunk) > max_bytes:
                    take = max_bytes - total
                    if take:
                        chunks.append(chunk[:take])
                    total = max_bytes
                    truncated = True
                    break
                chunks.append(chunk)
                total += len(chunk)
        except FetchError as exc:
            body_error = exc
        except (socket.timeout, TimeoutError) as exc:
            body_error = FetchError("timeout", f"{type(exc).__name__}: {exc}")
        except OSError as exc:
            body_error = FetchError("connection-error",
                                    f"{type(exc).__name__}: {exc}")
        return HopResult(url=url, status=status, pinned_ip=pinned_ip,
                         headers=headers, body=b"".join(chunks),
                         body_truncated=truncated, error=body_error)
    except FetchError:
        raise
    except (socket.timeout, TimeoutError) as exc:
        raise FetchError("timeout", f"{type(exc).__name__}: {exc}") from exc
    except OSError as exc:
        raise FetchError("connection-error", f"{type(exc).__name__}: {exc}") from exc
    except (http.client.HTTPException, ssl.SSLError, ValueError) as exc:
        raise FetchError("transport-error", f"{type(exc).__name__}: {exc}") from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass


@dataclass
class FetchOutcome:
    ok: bool
    status: int | None = None
    final_url: str | None = None
    redirect_chain: list = field(default_factory=list)
    body: bytes = b""
    body_truncated: bool = False
    content_sha256: str | None = None
    content_bytes: int = 0
    error: dict | None = None
    robots: dict | None = None
    hops: list = field(default_factory=list)  # 每一跳的 HopResult（重定向历史）

    def __post_init__(self):
        if self.ok and self.body is not None:
            self.content_sha256 = hashlib.sha256(self.body).hexdigest()
            self.content_bytes = len(self.body)


class Fetcher:
    """带全局预算与按 origin 缓存 robots 规则的有界抓取器。"""

    def __init__(self, policy: dict | None = None, offline: bool = False,
                 clock=time.monotonic, sleep=time.sleep):
        self.policy = dict(DEFAULT_POLICY)
        if policy:
            self.policy.update(policy)
        self.offline = offline
        self.request_count = 0
        self._last_hit: dict[str, float] = {}
        # 按 origin 缓存"解析后的规则/缺席/未知"，而非按路径缓存决定：
        # 同一 origin 换路径（含重定向目标）重新判定时不再发请求
        self._robots_rules: dict[str, dict] = {}
        self._robots_bodies: dict[str, bytes] = {}
        self._clock = clock
        self._sleep = sleep

    def _reserve_request(self) -> None:
        if self.request_count >= self.policy["max_requests"]:
            raise FetchError("budget-exhausted",
                             f"request cap {self.policy['max_requests']} reached")
        self.request_count += 1

    def _throttle(self, netloc: str) -> None:
        interval = float(self.policy["min_interval_s"])
        if interval <= 0:
            return
        last = self._last_hit.get(netloc)
        now = self._clock()
        if last is not None:
            wait = interval - (now - last)
            if wait > 0:
                self._sleep(wait)
        self._last_hit[netloc] = self._clock()

    def _request_counted(self, url: str, *, max_bytes=None, method="GET",
                         headers: dict | None = None) -> HopResult:
        self._reserve_request()
        parsed = urllib.parse.urlsplit(url)
        netloc = parsed.netloc.lower()
        self._throttle(netloc)
        # 失败但已保留部分响应的 hop 原样返回，由调用方判定语义
        return _single_request(
            url, float(self.policy["timeout_s"]),
            int(max_bytes or self.policy["max_bytes"]),
            self.policy["user_agent"], method=method,
            extra_headers=headers)

    def _fetch_follow(self, url: str, *, max_bytes=None) -> HopResult:
        """受限重定向跟随（robots.txt 等辅助资源用）；每跳独立计费并重验。"""

        current = url
        for _ in range(int(self.policy["max_redirects"]) + 1):
            hop = self._request_counted(current, max_bytes=max_bytes)
            if hop.error is not None:
                raise hop.error
            if hop.status not in REDIRECT_STATUSES:
                return hop
            location = (hop.headers.get("location") or [None])[0]
            if not location:
                raise FetchError("transport-error",
                                 "robots redirect without Location header")
            target = urllib.parse.urljoin(current, location)
            _validate_url(target)  # 每跳重验 scheme/userinfo，地址校验在请求内完成
            current = target
        raise FetchError("redirect-cap",
                         f"robots fetch exceeded max redirects "
                         f"({self.policy['max_redirects']})")

    def _robots_rules_for(self, parsed) -> dict:
        """取（或抓取并解析）该 origin 的 robots 规则；只缓存规则，不缓存路径决定。"""

        key = origin_key(parsed)
        robots_url = key + "/robots.txt"
        cached = self._robots_rules.get(key)
        if cached is not None:
            return dict(cached, cached=True)
        rules = {"mode": "unknown", "detail": "not fetched", "url": robots_url}
        try:
            hop = self._fetch_follow(
                robots_url, max_bytes=int(self.policy["robots_max_bytes"]))
        except FetchError as exc:
            if exc.kind in ("dns-error", "connection-error",
                            "unsafe-destination", "budget-exhausted"):
                # 目标主机级失败（含地址不安全）：页面抓取也会同样失败，
                # 按原始错误呈现，不把不可达/不安全主机误标成 robots 弃权；
                # 预算耗尽同样按自身错误上抛（与页面请求侧一致），不吞成未知
                raise
            rules = {"mode": "unknown",
                     "detail": f"robots fetch failed ({exc.kind}: {exc.message}); "
                               f"conservative abstain",
                     "url": robots_url}
            if exc.kind not in ("timeout", "redirect-cap"):
                # 超时/重定向超限是瞬态失败：unknown 不按 origin 缓存，下次
                # 判定重新抓取，不把瞬态未知变成粘性弃权
                self._robots_rules[key] = rules
            return dict(rules, cached=False)
        status = hop.status or 0
        if hop.error is not None:
            rules = {"mode": "unknown",
                     "detail": f"robots fetch error mid-body (status {status}, "
                               f"{hop.error.kind}); conservative abstain",
                     "url": robots_url}
        elif status in (404, 410):
            rules = {"mode": "absent", "detail": f"robots absent ({status})",
                     "url": robots_url}
        elif status in (401, 403) or status >= 500 or not (200 <= status < 300):
            rules = {"mode": "unknown",
                     "detail": f"robots status {status}; conservative abstain",
                     "url": robots_url}
        else:
            if hop.body_truncated:
                # robots 正文在 robots_max_bytes 处被截断：尾部 Disallow 可能
                # 丢失，规则不完整，按未知弃权，不得读成"允许"
                rules = {"mode": "unknown",
                         "detail": "robots body truncated at robots_max_bytes; "
                                   "rules may be incomplete; conservative abstain",
                         "url": robots_url}
            else:
                text = hop.body.decode("utf-8", "replace")
                self._robots_bodies[key] = hop.body
                parser = urllib.robotparser.RobotFileParser()
                try:
                    parser.parse(text.splitlines())
                except Exception as exc:  # 解析器异常按未知处理
                    rules = {"mode": "unknown",
                             "detail": f"robots parse error: {exc}",
                             "url": robots_url}
                else:
                    rules = {"mode": "rules", "parser": parser,
                             "detail": "robots rules parsed", "url": robots_url}
        self._robots_rules[key] = rules
        return dict(rules, cached=False)

    def robots_decision(self, url: str) -> dict:
        """对该 URL 做 robots 判定；规则按 origin 缓存，路径每次重新判定。"""

        parsed = _validate_url(url)
        rules = self._robots_rules_for(parsed)
        base = {"detail": rules["detail"], "url": rules.get("url"),
                "cached": bool(rules.get("cached"))}
        if rules["mode"] == "absent":
            return dict(base, result="allowed")
        if rules["mode"] == "unknown":
            return dict(base, result="unknown")
        allowed = rules["parser"].can_fetch("*", url)
        return dict(base, result="allowed" if allowed else "disallow",
                    path=parsed.path or "/")

    def fetch(self, url: str, *, headers: dict | None = None) -> FetchOutcome:
        """抓取一个 URL：每一跳（含重定向目标）先做 robots 判定，再受限请求。

        headers 仅允许工具代码写死的固定头（如 GitHub API 版本头）；
        与重定向语义无交互——每一跳都重新校验。
        """

        try:
            _validate_url(url)
        except FetchError as exc:
            return FetchOutcome(ok=False, error={"kind": exc.kind,
                                                 "message": exc.message})
        chain, hops = [], []
        current = url
        last_robots = None
        try:
            for depth in range(self.policy["max_redirects"] + 1):
                robots = self.robots_decision(current)
                last_robots = robots
                if robots["result"] != "allowed":
                    return FetchOutcome(
                        ok=False, final_url=current, redirect_chain=chain,
                        hops=hops, robots=robots,
                        error={"kind": f"robots-{robots['result']}",
                               "message": robots["detail"]})
                hop = self._request_counted(current, headers=headers)
                hops.append(hop)
                if hop.error is not None:
                    # 已取得（部分）响应的失败：保留 status/headers/部分 body
                    return FetchOutcome(
                        ok=False, status=hop.status, final_url=current,
                        redirect_chain=chain, hops=hops, robots=robots,
                        error={"kind": hop.error.kind,
                               "message": hop.error.message})
                if hop.status in REDIRECT_STATUSES:
                    location = (hop.headers.get("location") or [None])[0]
                    if not location:
                        return FetchOutcome(
                            ok=False, status=hop.status, final_url=current,
                            redirect_chain=chain, hops=hops, robots=robots,
                            error={"kind": "transport-error",
                                   "message": "redirect without Location header"})
                    target = urllib.parse.urljoin(current, location)
                    # 每一跳都完整重验（scheme/userinfo/DNS/公网），任何一跳不安全即中止
                    _validate_url(target)
                    chain.append({"from": current, "to": target,
                                  "status": hop.status})
                    current = target
                    continue
                if hop.status >= 500:
                    return FetchOutcome(
                        ok=False, status=hop.status, final_url=current,
                        redirect_chain=chain, hops=hops, robots=robots,
                        error={"kind": "http-5xx",
                               "message": f"server error status {hop.status}"})
                # 2xx/3xx 终点/4xx：HTTP 交换完成（4xx 的语义分级由 observe 层判定）
                return FetchOutcome(
                    ok=True, status=hop.status, final_url=current,
                    redirect_chain=chain, hops=hops, robots=robots,
                    body=hop.body, body_truncated=hop.body_truncated)
            return FetchOutcome(
                ok=False, status=hops[-1].status if hops else None,
                final_url=current, redirect_chain=chain, hops=hops,
                robots=last_robots,
                error={"kind": "redirect-cap",
                       "message": f"exceeded max redirects "
                                  f"({self.policy['max_redirects']})"})
        except FetchError as exc:
            return FetchOutcome(
                ok=False, status=(hops[-1].status if hops else None),
                final_url=current, redirect_chain=chain, hops=hops,
                error={"kind": exc.kind, "message": exc.message})
