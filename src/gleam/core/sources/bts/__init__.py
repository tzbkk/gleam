"""Typed BTS source adapter -- Debian bug discussion fetching with
frozen semantics.

Public surface (consumed by the bts entry shell and the investigate
fetch_bug tool):

- :class:`BtsClient` -- ``BtsClient(policy=...).fetch(bug_id,
  source="auto")`` returning a typed :class:`BtsResult`;
- :class:`BtsPolicy` -- numeric bounds (live values come from
  the config domain at call time);
- :func:`validate_bug_id` -- the shared bug-id rules (raises
  ``UsageError``; the entry maps exit 2);
- :data:`BTS_SOURCES` -- ``("auto", "soap", "mbox")``.

Semantics: SOAP ``get_bug_log`` is preferred (the documented machine
interface); the mbox fallback fires ONLY on an eligibility error
AND an explicit robots permit for the mbox endpoint; the PoW challenge
flow is bounded with unrecognized shapes reported as
``challenge-unknown``; every message carries message-id / date / from /
subject / body plus a sha256 digest over its raw bytes, URLs are
extracted from bodies, and attachments are METADATA ONLY (never
extracted or executed). Hosts and paths are fixed constants with no
configuration surface of any kind, and robots is never
bypassed. Failures return failed results with stable machine codes and
per-source ``attempts`` provenance; only usage mistakes raise
``UsageError``.

Tests patch ``gleam.core.sources.bts.client._open_connection`` /
``._public_address`` (module boundaries); no injection seams
exist.
"""

from __future__ import annotations

from .client import BtsClient, BtsResult, WHERE
from .policy import (
    AUTO_FALLBACK_ELIGIBLE,
    BTS_ALLOWED_HOSTS,
    BTS_SOURCES,
    BUG_ID_MAX,
    BUG_ID_RE,
    BtsPolicy,
    DEFAULT_BTS_WEB_BASE,
    bug_page_url,
    build_mbox_url,
    build_soap_get_bug_log,
    build_soap_url,
    validate_bug_id,
)

__all__ = [
    "AUTO_FALLBACK_ELIGIBLE",
    "BTS_ALLOWED_HOSTS",
    "BTS_SOURCES",
    "BUG_ID_MAX",
    "BUG_ID_RE",
    "BtsClient",
    "BtsPolicy",
    "BtsResult",
    "DEFAULT_BTS_WEB_BASE",
    "WHERE",
    "bug_page_url",
    "build_mbox_url",
    "build_soap_get_bug_log",
    "build_soap_url",
    "validate_bug_id",
]
