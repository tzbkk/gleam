"""Unified Result shell, diagnostic contract, and provisional exit mapping.

Implements the frozen contract:

- ``errors`` / ``diagnostics`` / ``warnings`` are arrays of
  structured items with the fixed keys ``code``, ``message``, ``where``,
  ``details``; ``details`` is any JSON-serializable value.
- ``status`` is restricted to ``ok`` / ``partial`` / ``failed`` /
  ``storage-error``; ``kind`` is a kebab-case machine label owned by each
  module (this module validates only the shape, never a vocabulary).
- ``schema_version`` is fixed to the integer ``1``.
- Provisional exit mapping ``ok -> 0`` and every other status
  ``-> 1``; usage/config errors take a separate path (:class:`UsageError`)
  that never yields a business Result and carries exit code 2.
- An ``ok`` Result with non-empty ``warnings`` or ``diagnostics``
  is downgraded to ``partial`` by :func:`finalize`.

Purity rules: stdlib only, no I/O, no output, no process termination.
Entry modules consume the returned exit codes and render diagnostics;
this module only builds and validates data.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any

__all__ = [
    "SCHEMA_VERSION",
    "STATUSES",
    "GOLDEN_FIELD_ORDER",
    "DiagnosticItem",
    "Result",
    "UsageError",
    "exit_code_for",
    "finalize",
]

#: Current JSON schema version shared by every module Result.
SCHEMA_VERSION = 1

#: The only allowed ``status`` values.
STATUSES = ("ok", "partial", "failed", "storage-error")

#: Stable top-level key order of the serialized Result.
GOLDEN_FIELD_ORDER = (
    "schema_version",
    "status",
    "kind",
    "data",
    "errors",
    "diagnostics",
    "warnings",
)

# Kebab-case shape check for ``kind``: lowercase alphanumeric
# segments joined by single hyphens. Vocabularies stay with the modules.
_KIND_SHAPE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _require_nonempty_str(value: Any, field_name: str) -> str:
    """Reject missing, empty, or non-string values for a required field."""
    if not isinstance(value, str) or not value:
        raise ValueError(
            f"{field_name} must be a non-empty string, got {value!r}"
        )
    return value


@dataclass(frozen=True, kw_only=True)
class DiagnosticItem:
    """One structured entry of ``errors`` / ``diagnostics`` / ``warnings``.

    ``code`` and ``where`` are stable machine fields, ``message``
    is a human-facing non-empty string, and ``details`` is any
    JSON-serializable value (default ``{}``) that must not carry
    undeclared control semantics.

    The string fields default to ``""`` on purpose: a missing field must
    surface as the contract-mandated ``ValueError`` from validation, not
    as a ``TypeError`` from the generated ``__init__``.
    """

    code: str = ""
    message: str = ""
    where: str = ""
    details: Any = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_nonempty_str(self.code, "code")
        _require_nonempty_str(self.message, "message")
        _require_nonempty_str(self.where, "where")
        try:
            json.dumps(self.details, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError(
                f"details must be a JSON-serializable value, "
                f"got {self.details!r}"
            ) from None

    def to_payload(self) -> dict[str, Any]:
        """Return the ordered mapping with the fixed item key order."""
        return {
            "code": self.code,
            "message": self.message,
            "where": self.where,
            "details": self.details,
        }


@dataclass(frozen=True, kw_only=True)
class Result:
    """Unified business Result shell shared by all eight entry modules.

    Field order mirrors the golden serialization order. The
    constructor validates every invariant eagerly and raises ``ValueError``
    on violation; nothing is silently coerced. ``kind`` vocabularies are
    owned by the domains/entries, not here.
    """

    schema_version: int = SCHEMA_VERSION
    status: str
    kind: str
    data: Any = field(default_factory=dict)
    errors: list[DiagnosticItem] = field(default_factory=list)
    diagnostics: list[DiagnosticItem] = field(default_factory=list)
    warnings: list[DiagnosticItem] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"schema_version is fixed to {SCHEMA_VERSION}, "
                f"got {self.schema_version!r}"
            )
        if self.status not in STATUSES:
            raise ValueError(
                f"status must be one of {STATUSES}, got {self.status!r}"
            )
        if not isinstance(self.kind, str) or not _KIND_SHAPE.fullmatch(self.kind):
            raise ValueError(
                f"kind must be a non-empty kebab-case string, "
                f"got {self.kind!r}"
            )
        for array_name in ("errors", "diagnostics", "warnings"):
            for entry in getattr(self, array_name):
                if not isinstance(entry, DiagnosticItem):
                    raise ValueError(
                        f"{array_name} entries must be DiagnosticItem "
                        f"instances, got {type(entry).__name__}"
                    )

    def to_payload(self) -> dict[str, Any]:
        """Return the ordered top-level mapping (golden field order)."""
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "kind": self.kind,
            "data": self.data,
            "errors": [entry.to_payload() for entry in self.errors],
            "diagnostics": [entry.to_payload() for entry in self.diagnostics],
            "warnings": [entry.to_payload() for entry in self.warnings],
        }

    def to_json(self) -> str:
        """Serialize with the stable golden key order."""
        return json.dumps(self.to_payload(), allow_nan=False)


class UsageError(Exception):
    """Usage/config error: exit code 2, never a business Result.

    Raised by entry and routing layers for argument parsing, configuration
    syntax, unknown keys, duplicate keys, and type/range/URL validation
    failures. The message is the diagnostic the entry layer writes to
    stderr; core itself performs no output and never terminates
    the process.
    """

    exit_code = 2


def exit_code_for(status: str) -> int:
    """Map a Result status to the provisional exit code.

    ``ok -> 0``; ``partial`` / ``failed`` / ``storage-error`` -> 1.
    Usage/config errors are outside this mapping: they raise
    :class:`UsageError` (``exit_code`` 2) before any Result exists.
    Unknown statuses raise ``ValueError``.
    """
    if status == "ok":
        return 0
    if status in ("partial", "failed", "storage-error"):
        return 1
    raise ValueError(f"unknown status: {status!r}")


def finalize(result: Result) -> Result:
    """Apply the ok-to-partial downgrade to a finished Result.

    A Result that claims ``ok`` while carrying ``warnings`` or
    ``diagnostics`` is returned as ``partial``; every other status is
    returned unchanged (``errors`` already imply a non-ok status).
    Pure: a new instance is built for the downgrade and the input
    is never mutated.
    """
    if result.status == "ok" and (result.warnings or result.diagnostics):
        return replace(result, status="partial")
    return result
