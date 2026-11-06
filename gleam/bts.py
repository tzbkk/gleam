"""Debian BTS 公开讨论抓取（bug_fetch）：有界、只读、经文档化公共接口。

协议事实（2026-11-02 实测 + Debian 官方文档，见 docs/research/experiments/
2026-11-02-bts-fetch/REPORT.md）：
- bugs.debian.org/robots.txt（实测原文留存）：对列名搜索机器人（Googlebot 等）
  Allow /cgi-bin/bugreport.cgi?bug=，对通用 ``User-agent: *`` 是 ``Disallow: /``。
  ⇒ 网页接口（HTML 页与 bugreport.cgi 的 mbox 视图）对通用自动客户端属
  robots 禁止路径；本工具默认不抓网页路径，绝不以伪装 UA 绕过。
- 文档化公共机器接口 = Debbugs/SOAP（https://wiki.debian.org/DebbugsSOAPInterface，
  https://bugs.debian.org/cgi-bin/soap.cgi，operation get_bug_log）：
  返回逐条消息的完整 RFC822 头 + 正文 + msg_num + 附件列表。实测可用
  （SOAP::Lite/Perl/1.27；实测 body 字段是 debbugs 扁平化文本而非原始
  MIME——multipart/signed 等结构的边界不在其中，解析回退并披露缺陷）。
  robots.txt 是爬虫排除协议，管辖对网页的自动抓取；SOAP 是 Debian 文档明确
  指给程序化访问的 API——这是"按任务边界使用被允许的文档化公共接口"的解释
  性决定，已在报告与观察记录中如实披露，操作员可用 --bts-source 显式改变。
- mbox 端点（bugreport.cgi?bug=N&mbox=yes，Content-Type: application/mbox，
  mbox=收到记录；mboxmaint=yes=维护者副本视角，devscripts bts 所用）属网页
  接口（robots 对 * 禁止），故仅当操作员显式 --bts-source mbox 且本地
  robots 判定非禁止时使用；robots 明确 disallow 即拒绝。
- PoW 挑战（2026-11-02 实测抓取到挑战页原文，证据留存）：bugs.debian.org
  前置 Varnish 对部分网页 GET 返回 302 到 /.internal/challenge.html（页面
  自述 "Adapted from haphash for Varnish PoW"，标题 "I Challenge Thee"）。
  实测触发面：Mozilla 形态 UA 的 GET 被挑战；非浏览器 UA 的 GET、HEAD、
  SOAP POST 未被挑战（触发条件可能自适应，不外推）。挑战值在
  pow_challenge cookie；解 = 找 tries 使 SHA256(challenge + ";" + tries)
  首字节为 0；答案写回 pow_nonce cookie 后重取原 URL。本工具按此文档化
  客户端流程实现有界求解（CPU 秒数与重试次数有界），未识别形态显式报
  challenge-unknown——绝不伪装成功、绝不规避 captcha/登录。2026-11-02
  本工具自身会话（诚实 UA + SOAP）未遇到挑战，如实记录，不冒充已通过。

安全模型（对应 docs/research/09 §4；不改动共享 net.Fetcher 的任何全局策略）：
- 本模块是**BTS 专用受信路径**（先例：search.py 的独立信任路径）：主机白名单
  仅 bugs.debian.org（应用侧常量，模型/调用方不可指定），URL 全部由应用侧从
  严格数字 bug_id 拼接；复用 net.py 的 SSRF/DNS 钉扎连接原语（公网地址校验、
  钉 IP、TLS 按主机名校验），请求/字节/时间/消息数/PoW CPU 时长全部有界。
- 只读 GET/POST 查询；不跟随任何重定向（如实报告）；附件**不单独提取、
  不单独写出、绝不执行**——MIME 解析只为登记元数据（文件名/类型/大小/
  传输编码/sha256 摘要）。如实披露：mbox 路径留存的**原始响应字节本身**
  天然含编码后的附件数据（QP/base64 原文），这是"保留原始证据"的必然
  结果；SOAP 响应里的 BTS attachments 数组不解析（未提取、元数据不可用，
  见 parse_soap_bug_log 的披露）。
- 挑战会话 cookie 仅存于客户端实例内存，绝不写入日志/产物；落盘的响应头里
  cookie 值一律脱敏。
- 正文文本仅做字符集解码与传输编码解码。
"""

from __future__ import annotations

import email
import email.header
import email.parser
import email.policy
import email.utils
import hashlib
import http.client
import json
import re
import socket
import ssl
import time
import urllib.parse
import urllib.robotparser
import xml.etree.ElementTree as ET

from .net import (_open_connection, _public_address, _validate_url)

# ---- 常量与预算 -----------------------------------------------------------

DEFAULT_BTS_WEB_BASE = "https://bugs.debian.org"
DEFAULT_BTS_SOAP_PATH = "/cgi-bin/soap.cgi"
DEFAULT_BTS_MBOX_PATH = "/cgi-bin/bugreport.cgi"
BTS_ALLOWED_HOSTS = {"bugs.debian.org"}  # 应用侧常量；不接受调用方传主机

BUG_ID_MAX = 9_999_999
BUG_ID_RE = re.compile(r"^[1-9][0-9]{0,6}$")  # 严格数字、无前导零

DEFAULT_POLICY = {
    "timeout_s": 20.0,
    "max_bytes": 8 * 1024 * 1024,   # 单响应上限（mbox/SOAP 都可能较大）
    "max_requests": 6,              # 单个客户端实例的累计请求上限（含 robots/挑战；一个实例可服务多次 fetch_bug）
    "min_interval_s": 2.0,          # 对 BTS 的最小请求间隔（礼貌限速）
    "max_messages": 200,            # 解析消息数上限
    "max_msg_body_chars": 20000,    # 单消息正文展示上限
    "max_urls_per_message": 20,
    "max_pow_seconds": 20.0,        # PoW CPU 时长上限
    "robots_max_bytes": 65536,
    "user_agent": "gleam-bts-fetch/0.1 (bounded read-only BTS discussion "
                  "fetcher; research tool)",
}

BTS_SOURCES = ("auto", "soap", "mbox")

# auto 模式的回退资格：SOAP 尝试只有失败在这些 kind 上才考虑 mbox 回退；
# rate-limited / not-found / budget-exhausted / robots-disallow /
# unsafe-destination / argument-invalid 一律**不回退**（原始错误直接上抛）。
AUTO_FALLBACK_ELIGIBLE = frozenset({
    "transport-error", "timeout", "http-error", "http-4xx",
    "soap-fault", "parse-error", "redirect", "challenge",
    "challenge-unknown",
})

CHALLENGE_MARKERS = (
    "i challenge thee",              # Debian Varnish PoW 挑战页标题（实测）
    "pow_challenge",                 # Debian 挑战的 cookie 名
    "haphash",                       # Debian 挑战页自述来源（haphash 改编）
    "anubis",                        # Anubis（.within.website 一族）
    ".within.website",
    "proof-of-work", "proof of work", "pow challenge",
    "checking your browser",
    "making sure you're not a bot",
)
BTS_CHALLENGE_PATH = "/.internal/challenge.html"
POW_TRIES_CAP = 10_000_000          # 挑战页 JS 自身的循环上限，如实沿用
_COOKIE_HEADERS = ("cookie", "set-cookie", "set-cookie2", "authorization")


class BtsError(Exception):
    """带 kind 的 BTS 抓取错误；kind 词表：
    argument-invalid / budget-exhausted / robots-disallow / unsafe-destination /
    transport-error / timeout / http-error / http-4xx / rate-limited /
    not-found / challenge / challenge-unknown / parse-error / redirect

    auto 模式下 attempts（若无则 None）保留逐来源尝试的 provenance：
    [{"source": "soap", "error": {kind, message}|None}, ...]
    """

    def __init__(self, kind: str, message: str):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message
        self.attempts: list[dict] | None = None


# ---- 参数校验与 URL/请求构造 ----------------------------------------------

def validate_bug_id(value) -> int:
    """bug_id 只接受严格数字（int 或纯数字 str），范围 1..BUG_ID_MAX。
    bool/float/带前导零/非数字一律拒绝——URL 永远由应用侧从该数字重建。"""

    if isinstance(value, bool):
        raise BtsError("argument-invalid", f"bug_id must be numeric, got bool")
    if isinstance(value, int):
        bug_id = value
    elif isinstance(value, str):
        if not BUG_ID_RE.match(value or ""):
            raise BtsError(
                "argument-invalid",
                f"bug_id must be digits without leading zeros "
                f"(1..{BUG_ID_MAX}); got {value!r}")
        bug_id = int(value)
    else:
        raise BtsError("argument-invalid",
                       f"bug_id must be an int or numeric string; "
                       f"got {type(value).__name__}")
    if not (1 <= bug_id <= BUG_ID_MAX):
        raise BtsError("argument-invalid",
                       f"bug_id out of range 1..{BUG_ID_MAX}: {bug_id}")
    return bug_id


def _check_base(base_url: str) -> str:
    parsed = _validate_url(base_url)
    host = (parsed.hostname or "").lower()
    if host not in BTS_ALLOWED_HOSTS:
        raise BtsError("argument-invalid",
                       f"BTS base host must be one of "
                       f"{sorted(BTS_ALLOWED_HOSTS)}; got {host!r}")
    if parsed.path not in ("", "/"):
        raise BtsError("argument-invalid",
                       "BTS base must not carry a path")
    return f"{parsed.scheme.lower()}://{parsed.netloc}"


def build_mbox_url(bug_id: int, base_url: str = DEFAULT_BTS_WEB_BASE) -> str:
    bug_id = validate_bug_id(bug_id)
    base = _check_base(base_url)
    return f"{base}{DEFAULT_BTS_MBOX_PATH}?bug={bug_id}&mbox=yes"


def build_soap_url(base_url: str = DEFAULT_BTS_WEB_BASE) -> str:
    base = _check_base(base_url)
    return base + DEFAULT_BTS_SOAP_PATH


def build_soap_get_bug_log(bug_id: int) -> bytes:
    """构造 get_bug_log 的 SOAP 信封（应用侧模板，bug_id 只以数字插入）。"""

    bug_id = validate_bug_id(bug_id)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
        'soap:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        '<soap:Body>'
        '<ns1:get_bug_log xmlns:ns1="urn:Debbugs/SOAP">'
        f'<bug_number xsi:type="xsd:int">{bug_id}</bug_number>'
        '</ns1:get_bug_log>'
        '</soap:Body></soap:Envelope>'
    ).encode("utf-8")


def bug_page_url(bug_id: int, base_url: str = DEFAULT_BTS_WEB_BASE) -> str:
    """人类可读的 bug 页 URL（仅作为出处链接返回，绝不抓取）。"""

    bug_id = validate_bug_id(bug_id)
    base = _check_base(base_url)
    return f"{base}/{bug_id}"


# ---- mbox / SOAP 解析 ------------------------------------------------------

_MBOX_FROM_RE = re.compile(
    rb"^From \S+ +(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
    rb"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    rb"[ 0-9]\d \d{2}:\d{2}:\d{2} \d{4}", re.M)


def split_mbox(data: bytes) -> tuple[list[tuple[int, int]], bool]:
    """按 mbox ``From `` 分隔行切块；返回 [(offset, length)] 与是否疑似多块。

    只认“From 发件人 星期 月 日 时:分:秒 年”形态的分隔行；BTS mbox 实测为
    该形态（mboxo）。无法识别任何分隔行时按单块处理（保底不丢消息边界信息：
    调用方据此标注 boundary=blob）。"""

    starts = [m.start() for m in _MBOX_FROM_RE.finditer(data)]
    if not starts:
        return [(0, len(data))], False
    blocks = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(data)
        blocks.append((start, end - start))
    return blocks, True


def _decode_rfc2047(raw: str | None) -> str | None:
    if raw is None:
        return None
    try:
        parts = email.header.decode_header(raw)
        out = []
        for text, charset in parts:
            if isinstance(text, bytes):
                out.append(text.decode(charset or "utf-8", "replace"))
            else:
                out.append(text)
        return "".join(out)
    except Exception:
        return raw


def _part_text(part) -> str | None:
    """解码一个 text/* 部分为 str；传输编码（QP/base64）经 stdlib 解码，
    字符集按声明 → utf-8 → cp1252/latin-1 替换链（记录实际使用的字符集）。"""

    try:
        raw = part.get_payload(decode=True)
    except Exception:
        return None
    if raw is None:
        raw = (part.get_payload() or "").encode("utf-8", "replace")
    charset = (part.get_content_charset() or "").strip().lower()
    for attempt in (charset, "utf-8", "cp1252", "latin-1"):
        if not attempt:
            continue
        try:
            return raw.decode(attempt)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _strip_html_bounded(html_text: str, cap: int) -> str:
    """有界 HTML→文本（仅当消息只有 text/html 时用于可读性；标注 html_only）。
    不做完整 HTML 语义解析，保守去标签 + 实体解引用，长度有界。
    正则前先预截断到 4*cap：调用方传入的是未预截断的完整解码正文
    （可达 max_bytes），未闭合的 <script/<style 会触发 O(k·n) 回溯。"""

    if len(html_text) > 4 * cap:
        html_text = (html_text[:4 * cap]
                     + f" […html truncated at {4 * cap} chars]")
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html_text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = (text.replace("&lt;", "<").replace("&gt;", ">")
                .replace("&amp;", "&").replace("&quot;", '"')
                .replace("&#39;", "'"))
    text = " ".join(text.split())
    return text[:cap]


_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)


def structure_message(raw: bytes, *, index: int, offset: int,
                      source_url: str, policy: dict,
                      msg_num: int | None = None) -> dict:
    """把一条原始 RFC822 消息字节结构化：头/正文/附件元数据/出处。

    附件不单独提取、不单独写出、绝不执行——只登记元数据（文件名、类型、
    大小、传输编码、sha256 摘要）；编码后的附件原文仍随原始响应留存
    （mbox 路径的必然结果，见模块 docstring 披露）。正文展示有界并如实
    标注截断。"""

    cap = int(policy["max_msg_body_chars"])
    url_cap = int(policy["max_urls_per_message"])
    # mbox From_ 分隔行不是头；剥掉再喂解析器（保边界信息已由 offset 承载）
    if raw.startswith(b"From "):
        nl = raw.find(b"\n")
        raw_msg = raw[nl + 1:] if nl >= 0 else raw
    else:
        raw_msg = raw
    # 深层嵌套 MIME 会同时引爆 stdlib 解析器与下面的 walk 递归：
    # 在解析入口统一转成诚实的 parse-error，绝不以 RecursionError 裸崩
    try:
        msg = email.parser.BytesParser(
            policy=email.policy.compat32).parsebytes(raw_msg)
    except RecursionError as exc:
        raise BtsError("parse-error",
                       "MIME nesting too deep for the stdlib parser "
                       "(RecursionError); honest parse failure") from exc

    subject_raw = msg.get("Subject")
    from_raw = msg.get("From")
    date_raw = msg.get("Date")
    date_iso = None
    if date_raw:
        try:
            dt = email.utils.parsedate_to_datetime(date_raw)
            date_iso = dt.isoformat()
        except (TypeError, ValueError):
            date_iso = None

    body_text, charsets, html_only, attach_meta = "", [], False, []

    def walk(part):
        nonlocal body_text, html_only
        if part.is_multipart():
            for sub in part.get_payload():
                walk(sub)
            return
        disp = (part.get("Content-Disposition") or "").lower()
        filename = part.get_filename()
        ctype = (part.get_content_type() or "text/plain").lower()
        is_attachment = ("attachment" in disp or bool(filename))
        if is_attachment:
            # 附件：不单独提取/写出/执行；摘要对已读入内存的载荷计算
            # （响应整体本就有界）。编码后的附件原文仍随原始响应留存
            # （mbox 路径的必然结果，见模块 docstring 披露）。
            try:
                payload = part.get_payload(decode=True) or b""
            except Exception:
                payload = b""
            attach_meta.append({
                "filename": filename,
                "content_type": ctype,
                "bytes": len(payload),
                "transfer_encoding":
                    (part.get("Content-Transfer-Encoding") or "").strip(),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "note": "not separately extracted, written, or executed; "
                        "encoded data may remain inside the preserved raw "
                        "response artifact",
            })
            return
        if ctype == "text/plain" and not body_text:
            body_text = _part_text(part) or ""
            cs = (part.get_content_charset() or "undetected")
            if cs not in charsets:
                charsets.append(cs)
        elif ctype == "text/html":
            if not body_text:
                html_text = _part_text(part) or ""
                body_text = _strip_html_bounded(html_text, cap)
                html_only = True
                cs = part.get_content_charset() or "undetected"
                if cs not in charsets:
                    charsets.append(cs)

    try:
        walk(msg)
    except RecursionError as exc:
        raise BtsError("parse-error",
                       "MIME nesting too deep to walk "
                       "(RecursionError); honest parse failure") from exc
    if not body_text and not attach_meta:
        # SOAP 接口的 body 字段是 debbugs 扁平化后的文本体（非原始 MIME：
        # multipart/signed 等结构的边界线不在 body 里，缺陷如实披露）。
        # 解析器对此类消息给出 StartBoundaryNotFoundDefect 且不切分——
        # 回退为顶层文本解码。
        payload = None
        try:
            payload = msg.get_payload(decode=True)
        except Exception:
            payload = None
        if payload is None:
            payload = (msg.get_payload() or "").encode("utf-8", "replace")
        charset = msg.get_content_charset() or ""
        for attempt in (charset, "utf-8", "cp1252", "latin-1"):
            if not attempt:
                continue
            try:
                body_text = payload.decode(attempt)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        else:
            body_text = payload.decode("utf-8", "replace")
        if charset and charset not in charsets:
            charsets.append(charset)
    defects = [type(d).__name__ for d in getattr(msg, "defects", [])]
    truncated = len(body_text) > cap
    body_shown = body_text[:cap]
    if truncated:
        body_shown += f" […body truncated at {cap} chars]"
    urls, seen = [], set()
    for m in _URL_RE.finditer(body_text):
        u = m.group(0).rstrip(".,;")
        if u not in seen:
            seen.add(u)
            urls.append(u)
        if len(urls) >= url_cap:
            break
    return {
        "index": index,
        "msg_num": msg_num,
        "message_id": (msg.get("Message-ID") or "").strip() or None,
        "date": date_raw,
        "date_iso": date_iso,
        "from": from_raw,
        "subject": subject_raw,
        "subject_decoded": _decode_rfc2047(subject_raw),
        "body_text": body_shown,
        "body_chars": len(body_text),
        "body_truncated": truncated,
        "html_only": html_only,
        "charsets": charsets,
        "attachments": attach_meta,
        "parse_defects": defects,
        "urls": urls,
        "source_url": source_url,
        "raw": {
            "offset": offset,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "boundary": "mbox-from-line" if raw.startswith(b"From ")
            else "soap-header-body",
        },
        "provenance_note": (
            "parsed from preserved raw artifact bytes at offset "
            f"{offset}; full raw response retained by the run store"),
    }


def parse_mbox_messages(data: bytes, *, source_url: str, policy: dict
                        ) -> tuple[list[dict], dict]:
    blocks, separated = split_mbox(data)
    notes = []
    if not separated:
        notes.append("no mbox From_ separator recognized; parsed as one "
                     "blob (boundary information not available)")
    messages = []
    max_messages = int(policy["max_messages"])
    for index, (offset, length) in enumerate(blocks):
        if len(messages) >= max_messages:
            notes.append(f"message cap {max_messages} reached; "
                         f"{len(blocks) - max_messages} more message(s) "
                         f"not parsed")
            break
        messages.append(structure_message(
            data[offset:offset + length], index=index, offset=offset,
            source_url=source_url, policy=policy))
    return messages, {"source_format": "mbox", "notes": notes,
                      "messages_total": len(blocks)}


_SOAP_TEXT = re.compile(r"<[^>]+>")


def parse_soap_bug_log(data: bytes, *, source_url: str, policy: dict
                       ) -> tuple[list[dict], dict]:
    """解析 get_bug_log 响应：逐 item 的 header/body/msg_num。

    XML 仅用 stdlib ElementTree（不解析外部实体）；**实体只经 ET 解码一次**
    ——正文里字面的 `&amp;` 等 HTML 实体文本必须原样保留（回归测试锁定）。
    item 里的 BTS attachments 数组**不解析**（无文档化结构依据，不做投机
    解析器）：若非空，只在 meta.notes 披露"未提取、元数据不可用"。
    非 2xx 语义由调用方先判，这里只处理合法信封；SOAP Fault 由调用方分级。
    """

    notes = []
    attachments_array_items = 0
    messages = []
    max_messages = int(policy["max_messages"])
    try:
        root = ET.fromstring(data.decode("utf-8", "replace"))
    except ET.ParseError as exc:
        raise BtsError("parse-error",
                       f"SOAP response is not well-formed XML: {exc}") from exc
    fault = root.find(".//{http://schemas.xmlsoap.org/soap/envelope/}Fault")
    if fault is not None:
        faultstring = ""
        for child in fault:
            if child.tag.endswith("faultstring"):
                faultstring = (child.text or "").strip()
        # faultstring 来自服务端（可达 max_bytes）：错误消息里截到 200 字符
        raise BtsError("soap-fault",
                       f"SOAP Fault from BTS: "
                       f"{faultstring[:200] or 'unknown'}")
    # 命名空间宽松匹配：item/header/body/msg_num 出现在 urn:Debbugs/SOAP 响应里；
    # 排除 attachments 数组内部的嵌套 item（它们不是消息）
    parent_map = {child: parent for parent in root.iter() for child in parent}

    def under_attachments(el) -> bool:
        node = parent_map.get(el)
        while node is not None:
            if node.tag.rsplit("}", 1)[-1] == "attachments":
                return True
            node = parent_map.get(node)
        return False

    items = [el for el in root.iter()
             if (el.tag.endswith("}item") or el.tag == "item")
             and not under_attachments(el)]
    if not items:
        raise BtsError("parse-error",
                       "get_bug_log response contains no message items")
    for index, item in enumerate(items):
        if len(messages) >= max_messages:
            notes.append(f"message cap {max_messages} reached; "
                         f"{len(items) - max_messages} more message(s) "
                         f"not parsed")
            break
        header_text = body_text = ""
        msg_num = None
        for child in item:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "header":
                # itertext 已做一次（且仅一次）XML 实体解码；不得再解
                header_text = "".join(child.itertext())
            elif tag == "body":
                body_text = "".join(child.itertext())
            elif tag == "msg_num":
                try:
                    msg_num = int((child.text or "").strip())
                except ValueError:
                    msg_num = None
            elif tag == "attachments":
                if len(child) > 0:
                    attachments_array_items += 1
        header_text = _strip_mbox_from_lines(header_text)
        raw_msg = (header_text + "\n\n" + body_text).encode("utf-8", "replace")
        messages.append(structure_message(
            raw_msg, index=index, offset=-1, source_url=source_url,
            policy=policy, msg_num=msg_num))
    if attachments_array_items:
        notes.append(
            f"{attachments_array_items} item(s) carry a BTS attachments "
            f"array in the SOAP response which this parser does NOT "
            f"extract (structure undocumented here); attachment metadata "
            f"unavailable via this path — honest disclosure, no "
            f"speculative parsing")
    return messages, {"source_format": "soap-get-bug-log", "notes": notes,
                      "messages_total": len(items)}


def _strip_mbox_from_lines(header_text: str) -> str:
    """SOAP header 字符串里混有 mbox From_ 行（实测如此）；剥掉以免被
    email 解析器当成畸形头。"""

    out = []
    for line in header_text.splitlines():
        if line.startswith("From ") and _MBOX_FROM_RE.match(
                line.encode("utf-8", "replace")):
            continue
        out.append(line)
    return "\n".join(out)


def _soap_fault_string(data: bytes) -> str | None:
    try:
        root = ET.fromstring(data.decode("utf-8", "replace"))
    except ET.ParseError:
        return None
    fault = root.find(".//{http://schemas.xmlsoap.org/soap/envelope/}Fault")
    if fault is None:
        return None
    for child in fault:
        if child.tag.endswith("faultstring"):
            return (child.text or "").strip()
    return ""


# ---- 挑战检测与解答 --------------------------------------------------------

def detect_challenge(status: int | None, headers: dict, body: bytes
                     ) -> dict | None:
    """检测响应是否是反爬挑战。判据保守：误判宁可放行给解析器报
    parse-error，也不把正常内容当挑战。返回 {"type", "detail"} 或 None。

    type 词表：
    - varnish-pow：Debian 实测形态——302 到 /.internal/challenge.html，
      或含 pow_challenge/I Challenge Thee/haphash 标记的 HTML 页；
    - pow-challenge：Anubis 形态（.within.website）等其他已知 PoW；
    - forbidden-html：403+HTML（疑似挑战变体，按挑战分级但不猜测解法）。
    """

    if status in (301, 302, 303, 307, 308):
        location = (headers.get("location") or [None])[0] or ""
        path = urllib.parse.urlsplit(location).path or location
        if path.split("?")[0] == BTS_CHALLENGE_PATH \
                or location.startswith(BTS_CHALLENGE_PATH):
            return {"type": "varnish-pow",
                    "detail": f"302 to {BTS_CHALLENGE_PATH} "
                              f"(Debian Varnish PoW challenge)"}
        return None
    content_type = ""
    for values in [headers.get("content-type", [])]:
        if values:
            content_type = values[0].lower()
            break
    head = body[:65536].decode("utf-8", "replace").lower()
    is_html = "html" in content_type \
        or head.lstrip().startswith("<!doctype html") or "<html" in head
    if not is_html:
        return None
    lowered = head
    if ("pow_challenge" in lowered or "i challenge thee" in lowered
            or "haphash" in lowered):
        return {"type": "varnish-pow",
                "detail": "Debian Varnish PoW challenge markers in HTML"}
    for marker in CHALLENGE_MARKERS:
        if marker in lowered:
            return {"type": "pow-challenge",
                    "detail": f"challenge marker {marker!r} in HTML response"}
    title = re.search(r"<title[^>]*>(.*?)</title>", lowered, re.S | re.I)
    if title and re.search(r"challenge|verify|checking|not a bot|puzzle",
                           title.group(1)):
        return {"type": "pow-challenge",
                "detail": f"challenge-like title {title.group(1).strip()!r}"}
    if status == 403 and is_html:
        return {"type": "forbidden-html",
                "detail": "403 with HTML body (possible challenge variant)"}
    return None


_ANUBIS_CHALLENGE_JSON_RE = re.compile(
    r'<script[^>]*id="anubis_challenge_data"[^>]*type="application/json"'
    r"[^>]*>(.*?)</script>", re.S | re.I)


def extract_anubis_challenge(body: bytes) -> dict | None:
    """提取 Anubis 形态挑战的参数（文档化客户端流程，
    https://techaro.qa/anubis/）：页面内嵌 script#anubis_challenge_data
    JSON，含 challenge 串与 difficulty。识别失败返回 None（诚实报
    challenge-unknown，绝不盲试）。"""

    text = body.decode("utf-8", "replace")
    m = _ANUBIS_CHALLENGE_JSON_RE.search(text)
    if not m:
        return None
    try:
        payload = json.loads(m.group(1).strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or "challenge" not in payload:
        return None
    return payload


def solve_varnish_pow(challenge: str, max_seconds: float) \
        -> tuple[int, str, float] | None:
    """Debian Varnish PoW（haphash 改编，与挑战页 JS 逐字对应）：
    找 tries ∈ [0, 10^7) 使 SHA256(challenge + ";" + tries) 首字节为 0
    （diff=2，约 256 次期望）。返回 (tries, digest_hex, elapsed_s)；
    超时/超次数返回 None——绝不无限烧 CPU。这是挑战方公开要求的
    正常客户端计算；captcha/登录墙永远不在此列。"""

    started = time.monotonic()
    prefix = f"{challenge};".encode("utf-8")
    for tries in range(POW_TRIES_CAP):
        if (tries & 0xFF) == 0 and time.monotonic() - started > max_seconds:
            return None
        digest = hashlib.sha256(prefix + str(tries).encode("ascii")).digest()
        if digest[0] == 0:
            return tries, digest.hex(), time.monotonic() - started
    return None


def solve_hashcash(challenge: str, difficulty: int,
                   max_seconds: float, hash_name: str = "sha256") \
        -> tuple[str, str, float] | None:
    """有界哈希现金求解（Anubis 形态）：找 nonce 使 H(challenge:nonce)
    十六进制前缀有 >= difficulty 个 '0'。超时返回 None。返回
    (nonce, hexdigest, elapsed_s)。"""

    import secrets
    started = time.monotonic()
    prefix = "0" * max(0, int(difficulty))
    while time.monotonic() - started < max_seconds:
        nonce = secrets.token_hex(8)
        digest = hashlib.new(hash_name,
                             f"{challenge}:{nonce}".encode("utf-8")
                             ).hexdigest()
        if digest.startswith(prefix):
            return nonce, digest, time.monotonic() - started
    return None


def redact_sensitive_headers(headers: dict) -> dict:
    """落盘前脱敏：cookie/authorization 值绝不进入任何产物。"""

    out = {}
    for key, values in headers.items():
        if key.lower() in _COOKIE_HEADERS:
            out[key] = [f"<redacted:{len(v)} chars>" for v in values]
        else:
            out[key] = list(values)
    return out


def _cookie_token_safe(token: str) -> bool:
    """cookie 名/值只收可打印 ASCII 且不含 ; , 与控制字符：含 CRLF 等
    的值一旦拼进 Cookie 头会让 http.client 抛 ValueError（被误分级为
    transport-error）——不安全的 cookie 整体丢弃。"""

    return bool(token) and all(
        0x20 <= ord(ch) <= 0x7E and ch not in ";," for ch in token)


# ---- 有界客户端 ------------------------------------------------------------

class BtsClient:
    """BTS 有界客户端（BTS 专用受信路径；不触碰共享 Fetcher）。请求预算
    与 robots 事实按**客户端实例**累计（一个实例可先后服务多次 fetch_bug，
    如 investigate 的 bug_fetch 工具）；investigate 侧另经 budget_check
    钩子共享 --max-requests 总预算。

    预算（全部应用侧常量，可经 policy 收紧）：请求总数、同主机最小间隔、
    单响应字节、超时、消息数、PoW CPU 秒数（挑战重试由请求预算承载）。
    挑战会话 cookie 只存实例内存。
    """

    def __init__(self, policy: dict | None = None,
                 base_url: str = DEFAULT_BTS_WEB_BASE,
                 clock=time.monotonic, sleep=time.sleep,
                 budget_check=None):
        self.policy = dict(DEFAULT_POLICY)
        if policy:
            self.policy.update(policy)
        self.base = _check_base(base_url)
        self.request_count = 0
        self._last_hit: float | None = None
        self._clock = clock
        self._sleep = sleep
        self._budget_check = budget_check
        self._cookies: dict[str, str] = {}
        self.robots_fact: dict | None = None
        self.challenge_trace: list[dict] = []

    # -- 底层请求（钉 IP + 有界读，模式沿用 net._single_request）----------

    def _reserve(self, what: str) -> None:
        if self._budget_check is not None:
            self._budget_check(what)  # 可抛 BtsError budget-exhausted
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
        parsed = _validate_url(url)
        if (parsed.hostname or "").lower() not in BTS_ALLOWED_HOSTS:
            raise BtsError("unsafe-destination",
                           f"host {parsed.hostname!r} not in BTS allowlist")
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        pinned_ip = _public_address(host)
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

    # -- robots 事实 -------------------------------------------------------

    def robots(self) -> dict:
        """取（并缓存）BTS robots.txt 事实；失败按 unknown 记录，不外推。"""

        if self.robots_fact is not None:
            return self.robots_fact
        self._reserve("robots.txt fetch")
        url = self.base + "/robots.txt"
        try:
            resp = self._request(url, max_bytes=int(
                self.policy["robots_max_bytes"]))
        except BtsError as exc:
            if exc.kind in ("transport-error", "timeout", "dns-error"):
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
        if status in (404, 410):
            fact.update(mode="absent", detail="robots absent")
        elif status != 200:
            fact.update(mode="unknown",
                        detail=f"robots status {status}; recorded")
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
        fact = self.robots()
        if fact["mode"] == "absent":
            return True
        if fact["mode"] != "rules":
            return None
        return fact["parser"].can_fetch("*", url)

    # -- 挑战处理 -----------------------------------------------------------

    def _handle_challenge(self, challenge: dict, resp: dict, url: str,
                          method: str, body: bytes | None,
                          extra_headers: dict | None) -> dict:
        """遇到挑战：按文档化客户端流程有界求解并重试一次；
        识别不了/解不出 → 诚实失败（challenge / challenge-unknown）。

        varnish-pow（Debian 实测形态）：pow_challenge 随挑战响应入
        cookie（若缺则 GET 挑战页一次补取）；求解后以 pow_nonce cookie
        回答并重试原 URL。cookie 只存实例内存、绝不落盘。
        """

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
                f"guess; body excerpt preserved in run raw/")
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
        # Anubis 文档化提交端点（同站固定路径；非模型可控）
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

    # -- 主入口 -------------------------------------------------------------

    def fetch_bug(self, bug_id, source: str = "auto",
                  allow_robots_disallowed_mbox: bool = False) -> dict:
        """抓取一个 bug 的完整公开讨论。

        source 三选（BTS_SOURCES）：
        - 'soap'/'mbox'：显式单来源，失败即失败（无回退），robots 门槛语义
          与历史版本一致（mbox 默认拒绝、robots 明确 disallow 无绕过开关、
          仅 allow_robots_disallowed_mbox 显式接受"robots 状态未知"）；
        - 'auto'（默认）：先试 SOAP（文档化机器接口），仅当 SOAP 以
          AUTO_FALLBACK_ELIGIBLE 内的 kind 失败**且** robots 明确允许
          mbox 端点（User-agent: *）时回退 mbox；rate-limited /
          not-found / budget-exhausted 等不回退；绝不绕过 robots。
          auto 模式忽略 allow_robots_disallowed_mbox（自动路径不接受
          未知 robots 态）。

        成功结果带 attempts（逐来源 provenance）与 source_selection；
        失败抛 BtsError（auto 全失败时 kind 取最后失败的来源，attempts
        保留完整轨迹；请求预算在同一客户端实例内共享）。
        """

        bug_id = validate_bug_id(bug_id)
        if source not in BTS_SOURCES:
            raise BtsError("argument-invalid",
                           f"source must be one of {list(BTS_SOURCES)}")
        if source != "auto":
            result = self._fetch_bug_single(
                bug_id, source,
                allow_robots_disallowed_mbox=allow_robots_disallowed_mbox)
            result["source_selection"] = {
                "requested": source, "mode": "explicit",
                "tried": [source], "fallback_used": False}
            result["attempts"] = [{"source": source, "error": None}]
            return result

        attempts: list[dict] = []
        try:
            result = self._fetch_bug_single(bug_id, "soap")
            result["source_selection"] = {
                "requested": "auto", "mode": "auto",
                "tried": ["soap"], "fallback_used": False}
            result["attempts"] = [{"source": "soap", "error": None}]
            return result
        except BtsError as exc:
            soap_error = exc
            attempts.append({"source": "soap",
                             "error": {"kind": exc.kind,
                                       "message": exc.message}})
        if soap_error.kind not in AUTO_FALLBACK_ELIGIBLE:
            soap_error.attempts = attempts
            raise soap_error
        # robots 门槛（robots 事实已随 SOAP 尝试缓存，不新增请求）：
        # 只有 robots 明确允许 mbox 端点才回退；未知/拒绝都不回退，
        # 且绝无绕过开关
        allowed = self._robots_allows(
            build_mbox_url(bug_id, self.base))
        if allowed is not True:
            fallback_note = (
                "auto fallback to mbox not attempted: robots does not "
                "clearly permit the web mbox endpoint "
                f"({self.robots_fact.get('mode')} / "
                f"{self.robots_fact.get('detail')}); robots is never "
                f"bypassed")
            final = BtsError(
                soap_error.kind,
                f"{soap_error.message}; [auto] {fallback_note}")
            final.attempts = attempts
            raise final
        try:
            result = self._fetch_bug_single(bug_id, "mbox")
            result["source_selection"] = {
                "requested": "auto", "mode": "auto",
                "tried": ["soap", "mbox"], "fallback_used": True,
                "fallback_reason": soap_error.kind}
            result["attempts"] = attempts + [{"source": "mbox",
                                              "error": None}]
            return result
        except BtsError as exc:
            attempts.append({"source": "mbox",
                             "error": {"kind": exc.kind,
                                       "message": exc.message}})
            final = BtsError(
                exc.kind,
                f"{exc.message}; [auto] both sources failed (soap: "
                f"{soap_error.kind}: {soap_error.message}; mbox: "
                f"{exc.kind}: {exc.message})")
            final.attempts = attempts
            raise final

    def _fetch_bug_single(self, bug_id: int, source: str,
                          allow_robots_disallowed_mbox: bool = False
                          ) -> dict:
        """单来源抓取（原 fetch_bug 主体；来源已校验）。"""
        robots = self.robots()
        if source == "mbox":
            url = build_mbox_url(bug_id, self.base)
            allowed = self._robots_allows(url)
            if allowed is False:
                # robots 明确 disallow：不提供任何绕过开关（任务边界：
                # robots 禁止的机器接口不绕过；文档化机器接口是 soap）
                raise BtsError(
                    "robots-disallow",
                    "bugs.debian.org robots.txt disallows the web mbox "
                    "endpoint for generic clients (User-agent: * Disallow: "
                    "/); refusing — use the documented SOAP machine "
                    "interface (source=soap) instead")
            if allowed is None and not allow_robots_disallowed_mbox:
                raise BtsError("robots-disallow",
                               f"robots state unknown ({robots['detail']}); "
                               f"conservative refusal for web endpoint")
            self._reserve("mbox fetch")
            resp = self._request(url)
            extra = {}
        else:
            url = build_soap_url(self.base)
            envelope = build_soap_get_bug_log(bug_id)
            self._reserve("soap get_bug_log")
            resp = self._request(
                url, method="POST", body=envelope,
                extra_headers={
                    "Content-Type": "text/xml; charset=utf-8",
                    "SOAPAction": '"urn:Debbugs/SOAP#get_bug_log"'})
            extra = {"envelope_bytes": len(envelope)}
        # _request 读体超时时带部分 body 与 error 字段返回：如实按其 kind
        # 分级上抛，绝不把部分 body 当 200 成功解析
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
            "bug_page_url": bug_page_url(bug_id, self.base),
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

        # 挑战检测先于重定向/429/状态判定：Debian 的 Varnish 挑战本身以
        # 302 到 /.internal/challenge.html 的形态出现
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
                # SOAP Fault 常伴随 HTTP 500 送达：先解析 faultstring 分级
                fault_text = _soap_fault_string(raw_body)
                if fault_text:
                    # 分类用全文，错误消息只带前 200 字符（服务端可控文本）
                    if re.search(r"no such|not exist|unknown bug|does not",
                                 fault_text, re.I):
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
            if exc.kind == "soap-fault" and re.search(
                    r"no such|not exist|unknown bug|does not",
                    exc.message, re.I):
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
        return result

