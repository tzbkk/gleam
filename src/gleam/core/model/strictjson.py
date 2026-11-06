"""Strict JSON helpers for model-domain payloads.

``loads_strict`` rejects NaN/Infinity JSON constants and overflow floats
(e.g. ``1e999``); every model response and every tool-call arguments string
is parsed through it. ``prompt_hash`` gives the stable SHA-256 identity of a
request payload for audit records.
"""

from __future__ import annotations

import hashlib
import json
import math


def _reject_constant(name: str) -> float:
    raise ValueError(f"non-finite JSON constant {name}")


def _parse_float_strict(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):  # overflow like 1e999 is caught here too
        raise ValueError(f"non-finite JSON number {text}")
    return value


def loads_strict(text: str):
    """``json.loads`` variant that rejects NaN/Infinity and overflow floats."""

    return json.loads(text, parse_constant=_reject_constant,
                      parse_float=_parse_float_strict)


def prompt_hash(payload: dict) -> str:
    """Stable SHA-256 of a request payload (sorted keys, non-ASCII kept)."""

    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
