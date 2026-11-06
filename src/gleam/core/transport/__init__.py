"""Transport domain: bounded safe fetching, content extraction, and
observation-outcome classification for untrusted public URLs.

Public surface (consumed by the sources/model domains and the
investigate entry):
- fetching: ``Fetcher`` (``.fetch(url, headers=None) -> FetchOutcome``),
  ``FetchOutcome``/``HopResult``, ``DEFAULT_POLICY``,
  ``REDIRECT_STATUSES``
- safety: ``check_host(host) -> HostCheck``, ``validate surface``
  (``FetchError``, ``origin_key``, ``ALLOWED_SCHEMES``)
- extraction: ``extract_text(body, content_type, base_url=None) ->
  RichExtraction`` (``.text``/``.extractor``/... plus ``.to_dict()``),
  ``extract_page``, ``decode_readme``
- outcomes: ``outcome_for(result)`` with constants ``OK``/``HTTP_ERROR``/
  ``TRANSPORT_ERROR``/``ABSTAIN``, vocabularies ``OUTCOMES`` and
  ``ABSTAIN_KINDS``

Trusted endpoints (operator-configured model base / SearXNG base /
GitHub API base) are strictly separated from the public-probe URLs this
package serves; they never route through this fetcher and this package
accepts no trusted-endpoint input.
"""

from __future__ import annotations

from .extract import (HAVE_TRAFILATURA, TRAFILATURA_VERSION, RichExtraction,
                      decode_readme, extract_page, extract_text)
from .net import (DEFAULT_POLICY, REDIRECT_STATUSES, FetchOutcome, Fetcher,
                  HopResult)
from .outcomes import (ABSTAIN, ABSTAIN_KINDS, HTTP_ERROR, OK, OUTCOMES,
                       TRANSPORT_ERROR, outcome_for)
from .safety import (ALLOWED_SCHEMES, FetchError, HostCheck, check_host,
                     origin_key)

__all__ = [
    "ABSTAIN", "ABSTAIN_KINDS", "ALLOWED_SCHEMES", "DEFAULT_POLICY",
    "FetchError", "FetchOutcome", "Fetcher", "HTTP_ERROR", "HAVE_TRAFILATURA",
    "HopResult", "HostCheck", "OK", "OUTCOMES", "REDIRECT_STATUSES",
    "RichExtraction", "TRAFILATURA_VERSION", "TRANSPORT_ERROR",
    "check_host", "decode_readme", "extract_page", "extract_text",
    "origin_key", "outcome_for",
]
