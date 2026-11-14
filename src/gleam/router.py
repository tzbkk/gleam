"""Subcommand router: pure routing, zero business logic.

Maps the first argv token to one of the eight entry modules and
delegates the remaining argv unchanged to that module's ``main``.
Empty argv routes to the help entry (general overview, stdout,
exit 0); an unknown token also routes to the help entry, which
prints the usage diagnostic to stderr and returns 2 (the
dispatcher itself never writes output, loads no ``.env``, parses no
flags, and validates nothing). Wiring into ``python -m gleam``
happens in ``gleam/__main__.py``.

Route lookup resolves ``entry.main`` through the module object at
dispatch time so tests can patch each entry's ``main`` cleanly.
"""

from __future__ import annotations

import sys
from typing import Sequence

from gleam.entries import (
    bts,
    check,
    investigate,
    repology,
    searxng,
    udd,
    version,
)
from gleam.entries import help as help_entry

__all__ = ["dispatch"]

#: Subcommand -> entry module (canonical MODULES order).
_ROUTES = {
    "udd": udd,
    "bts": bts,
    "repology": repology,
    "check": check,
    "investigate": investigate,
    "searxng": searxng,
    "help": help_entry,
    "version": version,
}


def dispatch(argv: Sequence[str] | None = None) -> int:
    """Route one argv (no program name) to an entry ``main``.

    Returns the entry's exit code. Empty argv routes to the help
    entry; an unrecognized first token routes there too, where it
    becomes the stderr usage diagnostic (exit 2). The router adds no
    output, no validation, and no business logic of its own.
    """
    items = list(sys.argv[1:]) if argv is None else list(argv)
    if not items:
        return help_entry.main([])
    entry = _ROUTES.get(items[0])
    if entry is None:
        return help_entry.main([items[0]])
    return entry.main(items[1:])
