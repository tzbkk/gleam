"""Internal typed error for the BTS adapter.

``BtsError`` carries a stable machine ``kind`` from the frozen
vocabulary: which failure maps to which kind decides
the auto-mode fallback eligibility, so the classification is
never re-derived here. This exception never crosses the public adapter
boundary: :meth:`gleam.core.sources.bts.BtsClient.fetch` converts every
failure into a failed :class:`~gleam.core.sources.bts.BtsResult`; only
usage mistakes (invalid bug id / source) raise the public
``UsageError`` before any network activity.
"""

from __future__ import annotations

__all__ = ["BtsError"]


class BtsError(Exception):
    """Internal fetch error with a stable ``kind`` label.

    Kind vocabulary (frozen): budget-exhausted /
    robots-disallow / unsafe-destination / transport-error / timeout /
    http-error / http-4xx / rate-limited / not-found / challenge /
    challenge-unknown / parse-error / redirect / soap-fault. Caller
    mistakes raise the public ``UsageError`` at the adapter boundary.

    ``attempts`` (or ``None``) preserves the per-source provenance of an
    auto-mode fetch: ``[{"source": "soap", "error": {kind, message} |
    None}, ...]``.
    """

    def __init__(self, kind: str, message: str):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message
        self.attempts: list[dict] | None = None
