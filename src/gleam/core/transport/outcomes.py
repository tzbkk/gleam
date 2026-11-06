"""Observation-outcome classification for fetch results.

The outcome vocabulary is exactly four values:
- ``OK``: the HTTP exchange completed without a transport-level failure
  and the status is not a 4xx client error.
- ``HTTP_ERROR``: 4xx -- the HTTP exchange completed but the target
  reported a client-side error; the response is retained and this is
  never a verdict of death.
- ``TRANSPORT_ERROR``: the fetch failed below HTTP (DNS, connection,
  timeout, 5xx, redirect problems); failure only ever means "unknown".
- ``ABSTAIN``: the fetch was intentionally not carried out: robots
  disallow/unknown, exhausted request budget, unsafe destination, or
  offline mode (the five fixed ABSTAIN kinds).

Evidence writing itself lives in the storage domain, not here.
"""

from __future__ import annotations

from .net import FetchOutcome

OK = "OK"
HTTP_ERROR = "HTTP_ERROR"
TRANSPORT_ERROR = "TRANSPORT_ERROR"
ABSTAIN = "ABSTAIN"

OUTCOMES = frozenset({OK, HTTP_ERROR, TRANSPORT_ERROR, ABSTAIN})

# Error kinds that mean "deliberately not probed" rather than "probed and
# failed"
ABSTAIN_KINDS = frozenset({"robots-disallow", "robots-unknown",
                           "budget-exhausted", "unsafe-destination",
                           "offline"})


def outcome_for(result: FetchOutcome) -> str:
    """Classify one fetch outcome into the four-value vocabulary."""

    if not result.ok:
        kind = (result.error or {}).get("kind", "transport-error")
        if kind in ABSTAIN_KINDS:
            return ABSTAIN
        return TRANSPORT_ERROR
    if result.status is not None and 400 <= result.status < 500:
        return HTTP_ERROR
    return OK
