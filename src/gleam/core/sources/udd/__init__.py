"""Typed UDD source adapter -- datasets-only query surface.

Public surface (consumed by the udd entry shell and the investigate
fetch_udd tool):

- :func:`list_datasets` -- pure 18-dataset catalog, no database, no
  budget consumption;
- :class:`UddClient` -- ``UddClient(dsn=...).query(datasets, package,
  ...)`` returning a typed :class:`UddResult` with columnar rows, the
  parallel ``udd:<dataset>#row=N`` provenance array, and the honest
  three-way semantics (empty = ok, failure = failed with a
  stable ``udd-*`` code, truncation = ``truncated``/``more_available``
  flags for the entry to map to ``partial``).

Queries run entirely in-process over the pg8000 wire-protocol driver
(:mod:`gleam.core.sources.udd.pgdriver`): one connection per query,
session-scoped read-only transaction and statement timeout, no child
process, no shell, no environment passthrough. No test-only
injection seams exist in this package: offline tests patch
``gleam.core.sources.udd.pgdriver.pg8000`` at that module boundary.
"""

from __future__ import annotations

from .catalog import DATASETS, DATASETS_TEMPLATE_VERSION, DATASET_NAMES
from .client import UddClient, UddPolicy, UddResult
from .catalog import list_datasets

__all__ = [
    "DATASETS",
    "DATASETS_TEMPLATE_VERSION",
    "DATASET_NAMES",
    "UddClient",
    "UddPolicy",
    "UddResult",
    "list_datasets",
]
