"""Shared human-output helpers for the entry shells

Private to the entries package. Every module renders paragraphs the
same width-indented way and prints the Result problem lists the same
way, so both live here instead of being copied into six renderers.
"""

from __future__ import annotations

import textwrap
from typing import Sequence

from gleam.core.result import Result

__all__ = ["WIDTH", "plural", "wrap", "problem_lines"]

WIDTH = 78


def wrap(text: str, indent: str = "  ",
         subsequent: str | None = None) -> str:
    """One wrapped paragraph; ``indent`` on the first line and
    ``subsequent`` (default: ``indent``) on the rest."""
    return textwrap.fill(
        " ".join(text.split()),
        WIDTH,
        initial_indent=indent,
        subsequent_indent=indent if subsequent is None else subsequent,
        break_long_words=False,
        break_on_hyphens=False,
    )


def plural(count: int, noun: str) -> str:
    """``1 row`` / ``3 rows``."""
    return f"{count} {noun}" + ("" if count == 1 else "s")


def problem_lines(result: Result, seen: Sequence[str] = ()) -> list[str]:
    """``! code: message`` lines for errors, diagnostics, and warnings.

    ``seen`` holds messages already printed elsewhere (a failed step's
    detail, for instance), so no problem is ever printed twice. The
    ``where`` path stays in ``--json``: a human needs the code and the
    message, not the module that raised it.
    """
    lines: list[str] = []
    printed = set(seen)
    for item in (*result.errors, *result.diagnostics, *result.warnings):
        if item.message in printed:
            continue
        lines.append(wrap(f"! {item.code}: {item.message}",
                          subsequent="    "))
    return lines
