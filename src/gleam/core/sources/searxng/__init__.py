"""Typed SearXNG source adapter -- trusted local search with frozen
semantics.

Public surface (consumed by the investigate search_web tool; stable
constructor shape): ``SearxngClient(base=..., timeout=...)`` with
``.search(query)`` returning a typed :class:`SearxngResult`.

Contract summary:
- JSON API ``GET /search`` against the operator-configured trusted
  base (the config domain owns the value); redirects are NEVER
  followed and every request is deadline- and byte-bounded on the
  package's own transport path, strictly separate from public probe
  URLs;
- result normalization is frozen: only absolute http(s) URLs survive
  (fragments stripped, credentialed URLs dropped), duplicates collapse
  onto the first occurrence preserving rank order with engine
  attributions merged, titles/snippets get HTML tags stripped, every
  result keeps per-engine attribution, and ``unresponsive_engines``
  passes through as-is;
- the three-way honesty contract: a clean empty (``ok``, zero
  results) is distinct from an explicit ambiguity (zero results +
  engine failures: ``failed``, ``ambiguous=True``, code
  ``all-engines-failed-or-empty``) and from every other stable
  failure grade (``timeout`` / ``connection-error`` /
  ``unexpected-redirect`` / ``format-forbidden`` / ``rate-limited`` /
  ``http-5xx`` / ``http-error`` / ``malformed-json``) -- failures are
  never read as "no results";
- the raw search JSON is retained on the result (``raw_body``) for
  observation archival by the managed entry; snippets carry the
  third-party-summary provenance note in ``note`` and in the rendered
  text.

Usage mistakes (empty/over-long query, malformed base) raise
``UsageError`` (exit 2 at the entry). Tests patch
``gleam.core.sources.searxng.client.transport`` (module boundary);
no injection seams exist.
"""

from __future__ import annotations

from .client import (
    DEFAULT_MAX_QUERY_CHARS,
    DEFAULT_SEARCH_BASE,
    DEFAULT_USER_AGENT,
    SEARCH_MAX_BYTES,
    SEARCH_TIMEOUT_S,
    SearxngClient,
    SearxngPolicy,
    SearxngResult,
    WHERE,
)
from .normalize import (
    canonical_url,
    norm_unresponsive,
    normalize_results,
    render_results_text,
    strip_html,
)

__all__ = [
    "DEFAULT_MAX_QUERY_CHARS",
    "DEFAULT_SEARCH_BASE",
    "DEFAULT_USER_AGENT",
    "SEARCH_MAX_BYTES",
    "SEARCH_TIMEOUT_S",
    "SearxngClient",
    "SearxngPolicy",
    "SearxngResult",
    "WHERE",
    "canonical_url",
    "norm_unresponsive",
    "normalize_results",
    "render_results_text",
    "strip_html",
]
