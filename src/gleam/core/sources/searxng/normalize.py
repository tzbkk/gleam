"""Conservative search-result normalization (frozen rules).

No information is invented: only absolute http(s) URLs without
credentials survive (fragments stripped); duplicates collapse onto the
FIRST occurrence preserving rank order while engine attributions
merge; titles/snippets get HTML tags stripped and entities decoded
(SearXNG highlights with ``<b>`` etc.) -- plain tag stripping and
entity reversal only, never new sentences; empty-after-strip fields
stay as empty strings. Every result keeps its per-engine attribution
and the response's ``unresponsive_engines`` list passes through as-is.
"""

from __future__ import annotations

import json
import urllib.parse
from html.parser import HTMLParser

__all__ = [
    "canonical_url",
    "norm_unresponsive",
    "normalize_results",
    "render_results_text",
    "strip_html",
]


class _TagStripper(HTMLParser):
    """Keep only visible text (entities decoded via
    ``convert_charrefs``)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data):
        self.parts.append(data)


def strip_html(text: str) -> str:
    """Strip HTML tags and decode entities; whitespace collapsed.
    Non-string input becomes the empty string."""

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
    """Normalize a result URL: absolute http(s) only, no credentials,
    sane port, fragment dropped; everything else yields ``None``."""

    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parsed = urllib.parse.urlsplit(url.strip())
        port = parsed.port  # malformed ports raise on attribute access
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None  # credentialed result URLs never become candidates
    if port is not None and not (1 <= port <= 65535):
        return None
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    netloc = f"{host}:{port}" if port else host
    return urllib.parse.urlunsplit(
        (scheme, netloc, parsed.path or "/", parsed.query, ""))


def norm_unresponsive(raw) -> list:
    """Normalize the ``unresponsive_engines`` field as-is: list/tuple
    entries keep their first two stringified cells, scalars become
    single-cell lists."""

    out = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, (list, tuple)) and item:
                out.append([str(x) for x in item[:2]])
            elif item is not None:
                out.append([str(item)])
    return out


def normalize_results(raw_results: list) -> tuple[list, int]:
    """Deduplicate preserving rank order (first occurrence wins, engine
    attributions merge, first non-empty title/snippet backfills),
    normalize URLs, strip HTML. Returns ``(results, deduped_count)``."""

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
            engines = [engines] if isinstance(engines, str) else []
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


def render_results_text(query: str, results: list,
                        unresponsive_engines: list) -> str:
    """Deterministic full-text rendering of one search result set
    (the pagination source for evidence re-reading and quote
    validation). One result per rank: title, URL, snippet, engines;
    the trailing lines carry the unresponsive-engine report and the
    third-party-summary provenance note."""

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
