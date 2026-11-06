"""Content extraction: trafilatura (fed only already-fetched bytes) +
    standard-library fallback + provenance capture.

Invariants:
- trafilatura only ever receives **bytes/text already fetched through the
  Fetcher safety layer**; its own downloader (trafilatura.fetch_url etc.)
  is never used -- the public-network SSRF boundary is owned entirely by
  the transport fetcher.
- *Before* boilerplate cleanup, an independent stdlib HTMLParser captures
  provenance signals: anchor text + absolute URLs (deduplicated, order
  preserved), mailto links, footer text, copyright lines. These signals
  would be lost in trafilatura's cleanup; they are the page's own
  attribution evidence and must be recorded first.
- When trafilatura is missing (ImportError), raises, or outputs nothing,
  extraction falls back to the stdlib parser and the result honestly
  carries the extractor tag ("trafilatura" / "fallback-stdlib" /
  "fallback-empty-stdlib") -- fallback output is never passed off as
  trafilatura output. Availability is checked at call time as well as
  import time so simulating a missing trafilatura via
  ``sys.modules["trafilatura"] = None`` flips extraction to the labeled
  fallback without re-importing this module.
- JSON / plain text are returned as-is (content untouched); metadata
  (author/title/date/sitename) is carried only when trafilatura actually
  produced it, null otherwise -- nothing is invented.
- Short author pages are not hidden: extracted text is kept regardless of
  length; attribution (anchors/copyright/mailto) is always carried
  alongside (bounded).
"""

from __future__ import annotations

import base64
import re
import sys
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser

from markdown_it import MarkdownIt

try:  # availability probe only; without it everything falls back to stdlib
    import trafilatura  # type: ignore
    TRAFILATURA_VERSION = getattr(trafilatura, "__version__", "unknown")
    HAVE_TRAFILATURA = True
except ImportError:  # pragma: no cover - depends on environment
    trafilatura = None
    TRAFILATURA_VERSION = None
    HAVE_TRAFILATURA = False


COPYRIGHT_RE = re.compile(r"(©|\(c\)|copyright)", re.I)
ANCHOR_TEXT_MAX = 120
PROVENANCE_CAP = 60          # cap on anchors/mailto/footer lines (bounded disclosure)
METADATA_KEYS = ("author", "title", "date", "sitename", "pagename")


class _PageParser(HTMLParser):
    """Stdlib fallback extraction: visible text and <a href>; ignores
    script/style/noscript/template."""

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
    """Return (body_text, links, kind). HTML -> HTMLParser; JSON/plain
    text -> content as-is."""

    if body is None:
        return "", [], "empty"
    charset = _charset_from_content_type(content_type)
    try:
        text = body.decode(charset or "utf-8", "replace")
    except LookupError:
        # unknown/exotic charset: fall back to utf-8 (non-text codecs are
        # rejected by Py3 anyway)
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
    """Independent capture before boilerplate cleanup: anchors, mailto,
    footer, copyright lines.

    Behavior stays conservative: script/style/noscript/template are
    ignored; block-level tags break lines so copyright-line detection has
    line boundaries to work with.
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
    """Anchor href -> normalized absolute http(s) URL; anything unfit
    (credentials, non-web scheme) -> None. Without a base URL only hrefs
    that are already absolute http(s) URLs survive."""

    if not isinstance(href, str) or not href.strip():
        return None
    try:
        absolute = urllib.parse.urljoin(base_url or "", href.strip())
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
    """Call the installed trafilatura through its supported API; only
    already-decoded text is passed in.

    Any exception degrades to "unavailable" (the caller labels the
    fallback); extraction failures never abort the observation.
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
            # version differences (older releases lack some kwargs): fall
            # back to the minimal supported signature
            content = trafilatura.extract(text, url=base_url,
                                          output_format="markdown")
    except Exception:
        content = None
    return content, metadata


def _trafilatura_ready() -> bool:
    """Call-time availability: the import-time probe AND the live module
    state. Setting ``sys.modules["trafilatura"] = None`` (the house
    dual-environment simulation of a missing install) must flip extraction
    to the labeled stdlib fallback without a re-import."""

    return HAVE_TRAFILATURA and sys.modules.get("trafilatura") is not None


@dataclass
class RichExtraction:
    """Extraction result for one fetched response (attribute-shaped)."""

    text: str = ""
    links: list = field(default_factory=list)
    kind: str = "empty"
    extractor: str | None = None
    metadata: dict = field(default_factory=dict)
    anchors: list = field(default_factory=list)
    mailtos: list = field(default_factory=list)
    copyright_lines: list = field(default_factory=list)
    footer_lines: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "links": self.links,
            "kind": self.kind,
            "extractor": self.extractor,
            "metadata": self.metadata,
            "anchors": self.anchors,
            "mailtos": self.mailtos,
            "copyright_lines": self.copyright_lines,
            "footer_lines": self.footer_lines,
            "notes": self.notes,
        }


def _extract_rich(body: bytes, content_type: str | None,
                  base_url: str | None) -> dict:
    """Body/links/provenance extraction for fetch results.

    Returned fields: text, links, kind, extractor, metadata, anchors,
    mailtos, copyright_lines, footer_lines, notes. JSON/plain text pass
    through untouched; HTML goes trafilatura -> stdlib fallback.
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

    # 1) independent provenance capture BEFORE boilerplate cleanup
    #    (trafilatura would drop these signals)
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
    result["links"] = _absolute_links(parser.anchors, base_url or "")

    # 2) trafilatura (already-decoded text only; never its downloader);
    #    missing/failed/empty -> stdlib fallback
    fallback_text, _, fallback_kind = extract_page(body, content_type,
                                                   base_url or "")
    if not _trafilatura_ready():
        result.update(text=fallback_text, kind="html",
                      extractor="fallback-stdlib")
        result["notes"].append(
            "trafilatura not installed in this environment; used stdlib "
            "HTMLParser extraction (labeled; not trafilatura output)")
        return result
    content, metadata = _trafilatura_extract(text, base_url or "")
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


def extract_text(body: bytes, content_type: str | None,
                 base_url: str | None = None) -> RichExtraction:
    """Public extraction entry: fetched bytes -> RichExtraction.

    ``base_url`` anchors relative-href resolution; without it only anchors
    whose hrefs are already absolute http(s) URLs produce link entries.
    """

    return RichExtraction(**_extract_rich(body, content_type, base_url))


def render_markdown_text(markdown: str) -> str:
    """Markdown source -> plain text: link text is kept, URLs and markup
    syntax are dropped, so the model quotes the prose it actually sees."""

    parts: list[str] = []
    for token in MarkdownIt("commonmark").parse(markdown or ""):
        if token.type == "inline" and token.children:
            for child in token.children:
                if child.type in ("text", "code_inline"):
                    parts.append(child.content)
                elif child.type in ("softbreak", "hardbreak"):
                    parts.append("\n")
                elif child.type == "image":
                    parts.append(child.content or "")
        elif token.type == "fence":
            parts.append("\n" + token.content + "\n")
        elif token.block and token.type.endswith("_close"):
            parts.append("\n")
    return " ".join(" ".join(parts).split())


def decode_readme(payload: dict, cap: int) -> tuple[str, str]:
    """GitHub readme: decode per encoding (base64) or as-is; returns
    (text, mode)."""

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
    text = render_markdown_text(text)
    mode = f"{mode}+markdown-text"
    if len(text) > cap:
        text = text[:cap] + f"\n[…readme truncated at {cap} chars]"
    return text, mode
