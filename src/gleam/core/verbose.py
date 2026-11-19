"""stderr narration for --verbose runs; explicit parameter, no
process-global state."""

import json
import re
import sys
import time
from collections.abc import Callable
from typing import Literal, assert_never

from .result import Result


def _quiet(message: str) -> None:
    return None


def make_verbose(module: str, enabled: bool, stream=None):
    """Return a callable(str) writing ``[module HH:MM:SS] message``
    lines; ``_quiet`` when disabled.

    ``stream`` is resolved at call time (default ``sys.stderr``) so
    tests can redirect after construction.
    """
    if not enabled:
        return _quiet

    def verbose(message: str) -> None:
        stamp = time.strftime("%H:%M:%S", time.gmtime())
        print(f"[{module} {stamp}] {message}",
              file=stream if stream is not None else sys.stderr)

    return verbose


#: One robots.txt verdict label accepted by :func:`robots_line`.
RobotsVerdict = Literal["allow", "disallow", "exempt", "unknown"]

_ROBOTS_STATUS = re.compile(r"robots status (\d+); ")


def robots_line(url: str, verdict: RobotsVerdict,
                detail: str | None) -> str:
    """One shared robots.txt narration line (user-approved wording).

    Serves both the Fetcher safety layer and the bts mbox gate so the
    two never drift. ``url`` is the request URL for allow/disallow/
    unknown lines and the bare host for exempt lines (both call
    sites' approved samples). ``detail`` carries the request path for
    ``disallow`` (urllib.robotparser exposes no matched rule), the
    exempt note label for ``exempt`` (``"operator file"`` or a
    caller-supplied note such as ``"tool-intrinsic, API terms"``),
    and the verdict's detail string for ``unknown``: status-shaped
    details (``robots status 503; ...``) compress to
    ``robots 503, abstain``; every other detail is quoted verbatim.
    """
    match verdict:
        case "allow":
            return f"robots {url}: allow"
        case "disallow":
            return f"robots {url}: disallow (path {detail})"
        case "exempt":
            return f"robots {url}: exempt ({detail})"
        case "unknown":
            status = _ROBOTS_STATUS.match(detail)
            if status:
                return (f"robots {url}: unknown "
                        f"(robots {status.group(1)}, abstain)")
            return f"robots {url}: unknown ({detail})"
        case unreachable:
            assert_never(unreachable)


def format_count(n: int) -> str:
    """Counts below 1000 as plain digits; 1000 and above as truncated
    tenths (``12453 -> "12.4k"``, floored -- never rounded -- to one
    decimal)."""
    if n < 1000:
        return str(n)
    hundreds = n // 100
    return f"{hundreds // 10}.{hundreds % 10}k"


def format_kb(num_bytes: int) -> str:
    """Decimal KB (1000 B) floored to one tenth: ``4096 -> "4.0 KB"``,
    ``4100..4199 -> "4.1 KB"``, below 100 bytes -> ``"0.0 KB"``."""
    hundreds = num_bytes // 100
    return f"{hundreds // 10}.{hundreds % 10} KB"


def dump_diagnostics(result: Result,
                     verbose: Callable[[str], None]) -> None:
    """One narration line per structured item, errors ->
    diagnostics -> warnings, no header line.

    Each line is ``! `` plus single-line sorted JSON so the full
    ``where`` and ``details`` fields -- the ones the human renderer
    drops -- ride the stderr dump.
    """
    for item in (*result.errors, *result.diagnostics, *result.warnings):
        verbose("! " + json.dumps(item.to_payload(),
                                  ensure_ascii=False, sort_keys=True))
