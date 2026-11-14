"""Read-only run classification from directory structure.

Pure directory-structure classification primitives for the check entry:
nothing is ever created, modified, or removed, and a run that cannot be
proven complete is never reported as a valid completed run.

Classification per run directory:

- ``valid``: ``run.json`` exists, parses as a JSON object, carries a
  non-empty ``status`` string, and no temp-file residue remains.
- ``incomplete``: ``run.json`` is missing, unreadable, or lacks the
  completion ``status`` field, or temp-file residue from an interrupted
  atomic write remains in the tree.
- ``error``: ``run.json`` explicitly records the ``storage-error``
  status; this recorded state outranks incomplete evidence
  signs.

Classification is layout-agnostic by construction: it reads only
``run.json`` (its completion ``status``) and walks the tree for
temp-file residue. It never inspects evidence subdirectories, so the
inside-run evidence layout never affects classification.

One shape predicate sits next to the classifier:
:func:`is_run_dir` answers whether a directory is itself one run
(``run.json`` or any evidence directory from ``LAYOUT_DIRS``). The
check entry uses it to tell a store root apart from a single run
directory -- a misuse the pure classifier cannot detect, because a run
tree's evidence subdirectories classify exactly like runs without
``run.json``. The predicate never influences classification.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from gleam.core.storage.runstore import LAYOUT_DIRS

__all__ = [
    "REASON_RUN_JSON_MISSING",
    "REASON_RUN_JSON_UNREADABLE",
    "REASON_RUN_STATUS_MISSING",
    "REASON_STORAGE_ERROR_RECORDED",
    "REASON_TMP_RESIDUE",
    "STATE_ERROR",
    "STATE_INCOMPLETE",
    "STATE_VALID",
    "TMP_PREFIX",
    "RunInfo",
    "infer_run",
    "infer_store",
    "is_run_dir",
]

STATE_VALID = "valid"
STATE_INCOMPLETE = "incomplete"
STATE_ERROR = "error"

#: ``run.json`` is absent.
REASON_RUN_JSON_MISSING = "run-json-missing"
#: ``run.json`` exists but does not parse as a JSON object.
REASON_RUN_JSON_UNREADABLE = "run-json-unreadable"
#: ``run.json`` parses but the completion ``status`` field is absent/empty.
REASON_RUN_STATUS_MISSING = "run-status-missing"
#: Temp-file residue from an interrupted atomic write (partial-write
#: evidence). Atomic writes create temp entries with :data:`TMP_PREFIX`
#: and rename them away; any survivor marks an interrupted write.
REASON_TMP_RESIDUE = "tmp-residue"
#: ``run.json`` records the ``storage-error`` status itself.
REASON_STORAGE_ERROR_RECORDED = "storage-error-recorded"

#: Temp entries created by the atomic write helpers (``.tmp-json-...``,
#: ``.tmp-bytes-...``) and any other ``.tmp`` leftover all share this
#: prefix.
TMP_PREFIX = ".tmp"

_STORAGE_ERROR_STATUS = "storage-error"


@dataclass(frozen=True)
class RunInfo:
    """One run directory's classification plus machine-readable signs.

    ``status`` is the raw ``status`` string recorded in ``run.json``
    (``None`` when the summary is missing or unreadable); ``reasons``
    are stable kebab-case codes explaining a non-valid state.
    """

    run_id: str
    state: str
    status: str | None
    reasons: tuple[str, ...]


def _has_tmp_residue(run_dir: str) -> bool:
    for root, dirs, files in os.walk(run_dir):
        for name in dirs + files:
            if name.startswith(TMP_PREFIX):
                return True
    return False


def infer_run(run_dir: str) -> RunInfo:
    """Classify one run directory purely from its structure."""
    run_id = os.path.basename(os.path.normpath(run_dir))
    reasons: list[str] = []
    status: str | None = None
    run_json = os.path.join(run_dir, "run.json")
    if not os.path.isfile(run_json):
        reasons.append(REASON_RUN_JSON_MISSING)
    else:
        try:
            with open(run_json, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            # json.JSONDecodeError subclasses ValueError; an unreadable
            # summary can never prove completion.
            reasons.append(REASON_RUN_JSON_UNREADABLE)
            doc = None
        if isinstance(doc, dict):
            recorded = doc.get("status")
            if isinstance(recorded, str) and recorded:
                status = recorded
            else:
                reasons.append(REASON_RUN_STATUS_MISSING)
    if _has_tmp_residue(run_dir):
        reasons.append(REASON_TMP_RESIDUE)
    if status == _STORAGE_ERROR_STATUS:
        reasons.append(REASON_STORAGE_ERROR_RECORDED)
        state = STATE_ERROR
    elif reasons:
        state = STATE_INCOMPLETE
    else:
        state = STATE_VALID
    return RunInfo(run_id=run_id, state=state, status=status,
                   reasons=tuple(reasons))


def is_run_dir(path: str) -> bool:
    """True when ``path`` is a directory shaped like one run tree.

    Structural signals only: ``run.json`` at the root, or any evidence
    child directory from :data:`LAYOUT_DIRS`. The layout is created up
    front by the writer, so an interrupted run still carries the
    evidence directories even before any ``run.json`` close-out.
    """
    if not os.path.isdir(path):
        return False
    if os.path.isfile(os.path.join(path, "run.json")):
        return True
    return any(
        os.path.isdir(os.path.join(path, name)) for name in LAYOUT_DIRS
    )


def infer_store(store: str) -> list[RunInfo]:
    """Classify every run directly under ``<store>/`` (read-only, sorted).

    A missing store, an empty store, or a store path that is not a
    directory yields an empty list (nothing to verify, zero
    counts). Stray non-directory entries under the store root are
    ignored (flattened layout: no inner ``runs/`` level).
    """
    if not os.path.isdir(store):
        return []
    infos: list[RunInfo] = []
    for name in sorted(os.listdir(store)):
        path = os.path.join(store, name)
        if os.path.isdir(path):
            infos.append(infer_run(path))
    return infos
