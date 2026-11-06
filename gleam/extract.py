"""正文抽取：trafilatura（仅作用于已抓取的字节）+ 标准库回退 + 出处捕获。

不变量：
- trafilatura 永远只接收**已经过 Fetcher 安全层抓回的字节/解码文本**，
  绝不使用其自带下载器（trafilatura.fetch_url 等）——公网 SSRF 边界
  完全由 net.Fetcher 承担。
- 在 boilerplate 清理**之前**，用独立的标准库 HTMLParser 捕获出处信号：
  锚文本+绝对 URL（去重保序）、mailto 链接、footer 文本、版权行——
  这些信号 trafilatura 清理后会丢失，属页面自证出处，必须先留痕。
- trafilatura 缺失（ImportError）/ 抛错 / 输出为空时，回退到既有
  标准库抽取（_PageParser 语义），并在结果中**如实标注** extractor
  标签（"trafilatura" / "fallback-stdlib" / "fallback-empty-stdlib"），
  绝不把回退结果冒充 trafilatura 输出。
- JSON / 纯文本按原文返回（不破坏内容）；元数据（author/title/date/
  sitename）只在 trafilatura 实际给出时携带，缺失为 null，不发明。
- 短作者页不隐藏：抽取文本无论长短都保留；attribution（锚点/版权/
  mailto）独立于正文长度始终携带（有界）。
"""

from __future__ import annotations

import base64
import re
import urllib.parse
from html.parser import HTMLParser

try:  # 仅探测可用性；缺失时全部走标准库回退并如实标注
    import trafilatura  # type: ignore
    TRAFILATURA_VERSION = getattr(trafilatura, "__version__", "unknown")
    HAVE_TRAFILATURA = True
except ImportError:  # pragma: no cover - 取决于环境
    trafilatura = None
    TRAFILATURA_VERSION = None
    HAVE_TRAFILATURA = False


COPYRIGHT_RE = re.compile(r"(©|\(c\)|copyright)", re.I)
ANCHOR_TEXT_MAX = 120
PROVENANCE_CAP = 60          # anchors/mailto/footer 行数上限（有界披露）
METADATA_KEYS = ("author", "title", "date", "sitename", "pagename")


class _PageParser(HTMLParser):
    """标准库回退抽取：可见文本与 <a href>；忽略 script/style/noscript/template。"""

    _SKIP = {"script", "style", "noscript", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.links: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        if tag == "a":
            for key, value in attrs:
                if key == "href" and isinstance(value, str):
                    self.links.append(value)

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0 and data.strip():
            self.text_parts.append(data)

    @property
    def text(self) -> str:
        return " ".join(" ".join(self.text_parts).split())


def _charset_from_content_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    match = re.search(r"charset=([^\s;]+)", content_type, re.I)
    return match.group(1).strip("\"'") if match else None


def extract_page(body: bytes, content_type: str | None, base_url: str):
    """返回 (body_text, links, kind)。HTML→HTMLParser；JSON/纯文本→原文。"""

    if body is None:
        return "", [], "empty"
    charset = _charset_from_content_type(content_type)
    try:
        text = body.decode(charset or "utf-8", "replace")
    except LookupError:
        # 未知/奇异 charset：退回 utf-8（非文本编解码器在 Py3 本就拒绝）
        text = body.decode("utf-8", "replace")
    ct = (content_type or "").lower()
    if "json" in ct:
        return text, [], "json"
    if "html" in ct or not ct:
        parser = _PageParser()
        try:
            parser.feed(text)
            parser.close()
        except Exception:
            return text, [], "html-parse-error"
        links = []
        for href in parser.links:
            absolute = urllib.parse.urljoin(base_url, href)
            parsed = urllib.parse.urlsplit(absolute)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                links.append(absolute)
        return parser.text, links, "html"
    return text, [], "text"


class _ProvenanceParser(HTMLParser):
    """boilerplate 清理前的独立捕获：锚点、mailto、footer、版权行。

    行为保持保守：忽略 script/style/noscript/template；块级标签换行，
    使版权行判定有行边界可用。
    """

    _SKIP = {"script", "style", "noscript", "template"}
    _BLOCK = {"p", "div", "li", "br", "tr", "section", "article",
              "header", "footer", "nav", "aside", "blockquote",
              "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._current: list[str] = []
        self.anchors: list[dict] = []
        self.mailtos: list[str] = []
        self.footer_lines: list[str] = []
        self._skip_depth = 0
        self._footer_depth = 0
        self._anchor_depth = 0
        self._anchor_text: list[str] = []
        self._anchor_href: str | None = None

    def _flush_line(self):
        line = "".join(self._current).strip()
        if line:
            self.lines.append(line)
            if self._footer_depth > 0:
                self.footer_lines.append(line)
        self._current = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
            return
        if tag in self._BLOCK:
            self._flush_line()
        if tag == "footer":
            self._footer_depth += 1
        if tag == "a":
            self._anchor_depth += 1
            if self._anchor_depth == 1:
                self._anchor_text = []
                self._anchor_href = None
                for key, value in attrs:
                    if key == "href" and isinstance(value, str):
                        self._anchor_href = value

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if tag in self._BLOCK:
            self._flush_line()
        if tag == "footer" and self._footer_depth > 0:
            self._footer_depth -= 1
        if tag == "a" and self._anchor_depth > 0:
            self._anchor_depth -= 1
            if self._anchor_depth == 0 and self._anchor_href is not None:
                text = " ".join("".join(self._anchor_text).split())[
                    :ANCHOR_TEXT_MAX]
                self.anchors.append({"text": text,
                                     "href": self._anchor_href})
                self._anchor_href = None

    def handle_data(self, data):
        if self._skip_depth > 0:
            return
        if data.strip() or self._current:
            self._current.append(data)
        if self._anchor_depth > 0:
            self._anchor_text.append(data)

    def close(self):
        super().close()
        self._flush_line()

    @property
    def copyright_lines(self) -> list[str]:
        return [line for line in self.lines if COPYRIGHT_RE.search(line)][:8]


def _absolute_links(anchors: list[dict], base_url: str) -> list[str]:
    seen, out = set(), []
    for anchor in anchors:
        absolute = _absolute_anchor_url(anchor.get("href"), base_url)
        if absolute and absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out


def _absolute_anchor_url(href, base_url: str) -> str | None:
    """锚点 href → 规范化绝对 http(s) URL；不合规（含凭据/非 http）→ None。"""

    if not isinstance(href, str) or not href.strip():
        return None
    try:
        absolute = urllib.parse.urljoin(base_url, href.strip())
        parsed = urllib.parse.urlsplit(absolute)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if port is not None and not (1 <= port <= 65535):
        return None
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, parsed.path or "/",
         parsed.query, ""))


def _collect_mailtos(anchors: list[dict]) -> list[str]:
    seen, out = set(), []
    for anchor in anchors:
        href = anchor.get("href") or ""
        if href.lower().startswith("mailto:"):
            addr = href[7:].split("?")[0].strip()
            if addr and addr not in seen:
                seen.add(addr)
                out.append(addr)
    return out


def _trafilatura_extract(text: str, base_url: str) -> tuple[str | None,
                                                             dict]:
    """调用安装版 trafilatura 的受支持 API；仅传已解码文本。

    任何异常都按"不可用"回退（由调用方标注），不让抽取失败中断观测。
    """

    metadata: dict = {}
    content: str | None = None
    try:
        meta_record = trafilatura.extract_metadata(text)
        for key in METADATA_KEYS:
            value = getattr(meta_record, key, None) if meta_record else None
            if isinstance(value, str) and value.strip():
                metadata[key] = value.strip()
    except Exception:
        metadata = {}
    try:
        kwargs = dict(url=base_url, output_format="markdown",
                      include_comments=True, include_tables=True,
                      deduplicate=True)
        try:
            content = trafilatura.extract(text, **kwargs)
        except TypeError:
            # 版本差异（旧版无部分参数）：退回最小受支持签名
            content = trafilatura.extract(text, url=base_url,
                                          output_format="markdown")
    except Exception:
        content = None
    return content, metadata


def extract_rich(body: bytes, content_type: str | None,
                 base_url: str) -> dict:
    """fetch_url 的正文/链接/出处抽取入口。

    返回字段：text、links、kind、extractor、metadata、anchors、mailtos、
    copyright_lines、footer_lines、notes。JSON/纯文本原样（不破坏），
    HTML 走 trafilatura→标准库回退。
    """

    result = {"text": "", "links": [], "kind": "empty", "extractor": None,
              "metadata": {}, "anchors": [], "mailtos": [],
              "copyright_lines": [], "footer_lines": [], "notes": []}
    if body is None:
        return result
    charset = _charset_from_content_type(content_type)
    try:
        text = body.decode(charset or "utf-8", "replace")
    except LookupError:
        text = body.decode("utf-8", "replace")
    ct = (content_type or "").lower()
    if "json" in ct:
        result.update(text=text, kind="json", extractor="passthrough")
        return result
    if "html" not in ct and ct:
        result.update(text=text, kind="text", extractor="passthrough")
        return result

    # 1) boilerplate 清理前的独立出处捕获（trafilatura 会丢弃这些信号）
    parser = _ProvenanceParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        result["notes"].append("provenance pre-parse failed (partial)")
    anchors = [
        {"text": anchor["text"],
         "url": _absolute_anchor_url(anchor.get("href"), base_url)}
        for anchor in parser.anchors[:PROVENANCE_CAP]
    ]
    result["anchors"] = anchors
    result["mailtos"] = _collect_mailtos(parser.anchors)[:PROVENANCE_CAP]
    result["copyright_lines"] = parser.copyright_lines
    result["footer_lines"] = parser.footer_lines[:PROVENANCE_CAP]
    result["links"] = _absolute_links(parser.anchors, base_url)

    # 2) trafilatura（仅已解码文本；无下载器）；缺失/失败/空 → 标准库回退
    fallback_text, _, fallback_kind = extract_page(body, content_type,
                                                   base_url)
    if not HAVE_TRAFILATURA:
        result.update(text=fallback_text, kind="html",
                      extractor="fallback-stdlib")
        result["notes"].append(
            "trafilatura not installed in this environment; used stdlib "
            "HTMLParser extraction (labeled; not trafilatura output)")
        return result
    content, metadata = _trafilatura_extract(text, base_url)
    if content is not None and content.strip():
        result.update(text=content, kind="html", extractor="trafilatura",
                      metadata=metadata)
        if not metadata:
            result["notes"].append("trafilatura returned no metadata fields")
        return result
    result.update(text=fallback_text, kind="html",
                  extractor="fallback-empty-stdlib")
    result["notes"].append(
        "trafilatura produced no content for this page; stdlib extraction "
        "used instead (labeled fallback-empty-stdlib)")
    return result


def decode_readme(payload: dict, cap: int) -> tuple[str, str]:
    """GitHub readme：按 encoding 解码（base64）或原文；返回 (text, mode)。"""

    encoding = payload.get("encoding")
    raw = payload.get("content")
    if not isinstance(raw, str):
        return "", "missing-content"
    if encoding == "base64":
        try:
            text = base64.b64decode(raw, validate=False).decode(
                "utf-8", "replace")
        except Exception as exc:
            return (f"[readme base64 decode failed: {type(exc).__name__}]",
                    "decode-failed")
        mode = "decoded-from-base64"
    else:
        text = raw
        mode = f"encoding-{encoding or 'none'}"
    if len(text) > cap:
        text = text[:cap] + f"\n[…readme truncated at {cap} chars]"
    return text, mode
