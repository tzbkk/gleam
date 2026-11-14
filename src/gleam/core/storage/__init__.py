"""L3 storage domain: append-only run directories and run classification.

Public surface (consumed by the investigate entry for managed writes and
by the check entry for read-only scanning):

- :class:`RunStore` / :meth:`RunStore.create` — exclusive creation of
  ``<store>/<UTC stamp>-<package>-<uuid4>/`` (flattened layout, no
  inner ``runs/`` level) plus append-only atomic evidence
  writes (temp file + ``os.replace``).
- :class:`StorageError` — storage integrity failure. Pre-creation
  failures leave nothing under the store; post-creation failures keep
  the half-written directory with a best-effort ``run.json`` close-out
  in the ``storage-error`` status.
- :func:`observation_id_for` — content-hash observation IDs; identical
  content deduplicates to one ID and one file per run.
- :func:`infer_run` / :func:`infer_store` / :class:`RunInfo` — pure
  directory-structure classification for check; no marker
  files are ever created.
- :func:`is_run_dir` — the store-root shape predicate: a directory is
  itself one run when it carries ``run.json`` or any evidence child
  directory. The check entry uses it to reject a single run's directory
  passed as ``--store`` instead of silently reading its evidence
  subdirectories as runs.

The package performs no output, never terminates the process, and reads
nothing from the process environment. No derived cross-run cache or
exclusion helper exists: the run directories are the only
authoritative evidence.
"""

from gleam.core.storage.inference import (
    REASON_RUN_JSON_MISSING,
    REASON_RUN_JSON_UNREADABLE,
    REASON_RUN_STATUS_MISSING,
    REASON_STORAGE_ERROR_RECORDED,
    REASON_TMP_RESIDUE,
    STATE_ERROR,
    STATE_INCOMPLETE,
    STATE_VALID,
    TMP_PREFIX,
    RunInfo,
    infer_run,
    infer_store,
    is_run_dir,
)
from gleam.core.storage.runstore import (
    SAFE_ID,
    LAYOUT_DIRS,
    SOURCE_DIRS,
    STORAGE_ERROR_STATUS,
    RunStore,
    StorageError,
    observation_id_for,
    source_dir_for_label,
    utc_now,
)

__all__ = [
    "REASON_RUN_JSON_MISSING",
    "REASON_RUN_JSON_UNREADABLE",
    "REASON_RUN_STATUS_MISSING",
    "REASON_STORAGE_ERROR_RECORDED",
    "REASON_TMP_RESIDUE",
    "SAFE_ID",
    "STATE_ERROR",
    "STATE_INCOMPLETE",
    "STATE_VALID",
    "STORAGE_ERROR_STATUS",
    "LAYOUT_DIRS",
    "SOURCE_DIRS",
    "TMP_PREFIX",
    "RunInfo",
    "RunStore",
    "StorageError",
    "infer_run",
    "infer_store",
    "is_run_dir",
    "observation_id_for",
    "source_dir_for_label",
    "utc_now",
]
