"""Append-only run storage: exclusive run directories and atomic writes.

Implements the frozen contract:

- Every run exclusively creates
  ``<store>/<UTC stamp>-<package>-<uuid4>/`` (stamp format
  ``%Y%m%dT%H%M%SZ``; package is the run's Debian source package name).
  Creation is attempted once with no collision retry; identifiers
  follow the ``SAFE_ID`` policy and writes use the temp-file +
  ``os.replace`` atomic pattern.
- Observation IDs are the 64-hex sha256 over the canonical
  compact JSON of the record minus the ID itself; identical content maps
  to one ID and is stored once per run.
- Once the run directory exists, a write failure keeps the
  half-written directory and attempts a best-effort ``run.json``
  close-out with the ``storage-error`` status; the failure itself always
  surfaces as :class:`StorageError`.
- No derived cross-run cache and no cross-run exclusion files; the run
  directories are the only authoritative evidence.
- Run directories live directly under ``<store>/`` (no inner ``runs/``
  directory level).

Inside-run layout: every new run directory contains exactly nine
evidence subdirectories (``LAYOUT_DIRS``): ``rounds/``, ``observations/``
and the seven per-source directories ``fetch_url/``, ``search_web/``,
``github/``, ``fetch_bug/``, ``fetch_udd/``, ``repology/``, ``robots/``.
One round document lands at ``rounds/NNNN.json`` (written once per
round), observations at ``observations/<sha256>.json`` (the file name IS
the observation ID carried by the stored record and by ``run.json``),
and per-source documents at ``<source>/<sha256>.json`` where the file
name is the sha256 of the exact canonical compact bytes written.
Existing stores are never migrated, rewritten, or deleted --
``gleam.core.storage.inference`` classifies runs through ``run.json``
alone, and no in-repo reader reads run leaf files.

Failure semantics: a failure
before the run directory exists raises :class:`StorageError` and leaves
nothing behind under the store; a failure afterwards preserves the
directory for read-only classification by :mod:`gleam.core.storage.inference`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, NoReturn

__all__ = [
    "SAFE_ID",
    "LAYOUT_DIRS",
    "SOURCE_DIRS",
    "RunStore",
    "StorageError",
    "observation_id_for",
    "source_dir_for_label",
    "utc_now",
]

#: Identifier policy: identifiers never contain a
#: path separator, a leading dot, or a drive-style colon.
SAFE_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+-]*$")

#: Evidence subdirectories of every run directory, in creation order
#: (one rounds/ series, observations/, and one directory per
#: evidence source).
LAYOUT_DIRS = ("rounds", "observations", "fetch_url", "search_web",
               "github", "fetch_bug", "fetch_udd", "repology", "robots")

#: Per-source document directories (LAYOUT_DIRS minus the rounds/
#: and observations/ series); the valid ``source`` values of
#: :meth:`RunStore.write_source_document`.
SOURCE_DIRS = ("fetch_url", "search_web", "github", "fetch_bug",
               "fetch_udd", "repology", "robots")

#: The ONE label -> source-directory table.
#: Internal evidence labels of the investigate entry map to the
#: per-source directory their documents belong in; ``robots`` is not a
#: tool label (robots bodies are flushed from the fetcher), so it has
#: no row here.
LABEL_SOURCE_DIRS = {
    "agent-fetch": "fetch_url",
    "agent-search": "search_web",
    "agent-github": "github",
    "agent-bts": "fetch_bug",
    "agent-udd": "fetch_udd",
    "agent-repology": "repology",
}

#: Status recorded by the best-effort close-out on a mid-run write
#: failure.
STORAGE_ERROR_STATUS = "storage-error"


def source_dir_for_label(label: str) -> str:
    """Source directory for an internal evidence label.

    The single mapping table lives here; no second table exists
    anywhere else. Unknown labels raise :class:`ValueError` naming the
    valid labels.
    """

    try:
        return LABEL_SOURCE_DIRS[label]
    except (KeyError, TypeError):
        valid = ", ".join(sorted(LABEL_SOURCE_DIRS))
        raise ValueError(
            f"unknown evidence label: {label!r} (valid labels: {valid})"
        ) from None


class StorageError(Exception):
    """Storage integrity failure.

    Raised when the run directory cannot be created (nothing is left
    behind under the store) or when a write fails after creation (the
    half-written directory is preserved with a best-effort ``run.json``
    close-out). Entry modules translate this exception into
    ``status=storage-error`` results.
    """


def utc_now() -> str:
    """Second-resolution UTC timestamp ``%Y-%m-%dT%H:%M:%SZ``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_id(identifier: Any, what: str) -> str:
    if not isinstance(identifier, str) or not SAFE_ID.match(identifier) \
            or identifier in {".", ".."}:
        raise ValueError(f"unsafe {what}: {identifier!r}")
    return identifier


def _safe_join(base: str, identifier: str, what: str) -> str:
    _check_id(identifier, what)
    path = os.path.join(base, identifier)
    # Second line of defense: the resolved path must stay inside base.
    if os.path.dirname(os.path.abspath(path)) != os.path.abspath(base):
        raise ValueError(f"{what} escapes store dir: {identifier!r}")
    return path


def _write_json_atomic(path: str, payload: Any) -> None:
    """Write JSON via a temp file inside the target directory."""
    tmp_fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path),
                                   prefix=".tmp-json-")
    try:
        # newline="\n": text mode must never translate LF to the
        # platform line ending (Windows text mode defaults to CRLF
        # translation); JSON files stay byte-identical on every OS.
        with os.fdopen(tmp_fd, "w", encoding="utf-8",
                       newline="\n") as fh:
            # allow_nan=False: NaN/Inf are rejected instead of writing
            # invalid JSON to disk.
            json.dump(payload, fh, ensure_ascii=False, indent=2,
                      sort_keys=True, allow_nan=False)
            fh.write("\n")
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def _write_bytes_atomic(path: str, data: bytes) -> None:
    """Write bytes via a temp file inside the target directory."""
    tmp_fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path),
                                   prefix=".tmp-bytes-")
    try:
        with os.fdopen(tmp_fd, "wb") as fh:
            fh.write(data)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def observation_id_for(record: dict) -> str:
    """Content-addressed observation ID.

    The hash input is the full record minus the ``observation_id`` key,
    serialized as compact Python JSON with sorted keys
    (``ensure_ascii=False``, ``allow_nan=False``) and UTF-8 encoded.
    """

    content = {key: value for key, value in record.items()
               if key != "observation_id"}
    blob = json.dumps(content, sort_keys=True, ensure_ascii=False,
                      allow_nan=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class RunStore:
    """One exclusively created run directory directly under ``<store>/``
    (flattened layout; the store root IS the runs root).

    Writes are append-only and atomic; history is never modified. An
    instance serves exactly one run: :meth:`create` makes the directory,
    the write methods append evidence, and :meth:`close_run` finalizes
    ``run.json``.
    """

    def __init__(self, base: str, package: str):
        # The package is a path segment of the run directory name, so it
        # must satisfy the same identifier policy as every other path
        # component (SAFE_ID, ``.``/``..`` rejected).
        _check_id(package, "package")
        self.base = os.path.abspath(base)
        self.package = package
        self.run_id: str | None = None
        self.run_dir: str | None = None
        self.started_at: str | None = None
        self._observation_ids: set[str] = set()
        self._source_documents: set[str] = set()
        self._rounds_written: set[str] = set()
        self._staged_round: tuple[int, Callable[[], dict | None]] | None = None
        self._closed = False
        self._failed = False

    # ---- creation ---------------------------------------------------------

    @classmethod
    def create(cls, base: str, package: str) -> "RunStore":
        """Exclusively create the run directory and return the store.

        A single attempt, no collision retry. Any failure before
        the run directory fully exists raises :class:`StorageError`
        after removing everything this call created, so nothing is left
        anywhere under the store.
        """
        store = cls(base, package)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{stamp}-{store.package}-{uuid.uuid4()}"
        run_dir = os.path.join(store.base, run_id)
        base_existed = os.path.isdir(store.base)
        try:
            os.makedirs(store.base, exist_ok=True)
            os.mkdir(run_dir)  # exclusive creation
            for sub in LAYOUT_DIRS:
                os.mkdir(os.path.join(run_dir, sub))
        except OSError as exc:
            store._cleanup_partial(base_existed, run_dir)
            raise StorageError(
                f"could not create run directory {run_dir}: {exc}"
            ) from exc
        store.run_id = run_id
        store.run_dir = run_dir
        store.started_at = utc_now()
        return store

    def _cleanup_partial(self, base_existed: bool,
                         run_dir: str) -> None:
        """Best-effort removal of everything this create() call made.

        Directories that existed before the attempt are never touched;
        ``os.rmdir`` only succeeds on empty directories, so a store that
        held other runs stays intact.
        """
        if os.path.isdir(run_dir):
            shutil.rmtree(run_dir, ignore_errors=True)
        if not base_existed and os.path.isdir(self.base):
            try:
                os.rmdir(self.base)
            except OSError:
                pass

    # ---- in-run writes ------------------------------------------------------

    def _assert_writable(self) -> str:
        if not self.run_dir or not self.run_id:
            raise RuntimeError("create() has not been called")
        if self._failed:
            raise StorageError(
                "run storage already failed; the half-written run "
                "directory is preserved for read-only classification"
            )
        if self._closed:
            raise StorageError("run is already closed")
        return self.run_dir

    def write_observation(self, record: dict) -> str:
        """Append one observation record, deduplicated by content.

        Returns the content-hash observation ID. Identical content maps
        to the same ID and is written at most once per run; a stale
        ``observation_id`` key on the input record is replaced by the
        computed ID. The ID is the bare sha256 hex digest and is exactly
        the file stem: ``observations/<sha256>.json`` carries
        ``"observation_id": "<sha256>"``.
        """
        run_dir = self._assert_writable()
        obs_id = observation_id_for(record)
        if obs_id in self._observation_ids:
            return obs_id
        stored = {key: value for key, value in record.items()
                  if key != "observation_id"}
        stored["observation_id"] = obs_id
        path = _safe_join(os.path.join(run_dir, "observations"),
                          obs_id, "observation id") + ".json"
        try:
            _write_json_atomic(path, stored)
        except OSError as exc:
            self._storage_failure(exc, f"observation write failed for {obs_id}")
        self._observation_ids.add(obs_id)
        return obs_id

    def write_source_document(self, source: str, document: dict) -> dict:
        """Append one per-source document under ``<source>/<sha256>.json``.

        The canonical bytes are the compact sorted-key JSON encoding of
        ``document`` (UTF-8, no trailing newline); the file name is the
        sha256 of exactly those bytes, and those exact bytes are what
        lands on disk. Identical document -> identical path, and the
        second write is idempotent: it is stored at most once per run
        (content-addressed dedup, same semantics as observations).

        Returns ``{"source": source, "sha256": sha, "path": path}`` with
        the run-relative path.
        """
        if source not in SOURCE_DIRS:
            valid = ", ".join(SOURCE_DIRS)
            raise ValueError(
                f"unknown source directory: {source!r} (valid: {valid})")
        run_dir = self._assert_writable()
        blob = json.dumps(document, ensure_ascii=False, sort_keys=True,
                          allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
        sha = hashlib.sha256(blob).hexdigest()
        rel = f"{source}/{sha}.json"
        if rel not in self._source_documents:
            path = _safe_join(os.path.join(run_dir, source), sha,
                              "source document sha") + ".json"
            try:
                _write_bytes_atomic(path, blob)
            except OSError as exc:
                self._storage_failure(
                    exc, f"source document write failed for {source}/{sha}")
            self._source_documents.add(rel)
        return {"source": source, "sha256": sha, "path": rel}

    def write_round(self, round_no: int, record: dict) -> str:
        """Append the round document ``rounds/<round_no:04d>.json``.

        Rounds are write-once (append-only): each round path is written
        at most once before close; a repeated round number raises
        :class:`ValueError`. Returns the run-relative path.
        """
        if not isinstance(round_no, int) or isinstance(round_no, bool) \
                or round_no < 1:
            raise ValueError(
                f"round number must be a positive integer, got {round_no!r}")
        rel = f"rounds/{round_no:04d}.json"
        if rel in self._rounds_written:
            raise ValueError(f"round {round_no} already written; rounds "
                             "are append-only (write-once)")
        if self._staged_round is not None \
                and self._staged_round[0] == round_no:
            # This call takes ownership of the round: the failure-path
            # staged flush must never write it a second time.
            self._staged_round = None
        run_dir = self._assert_writable()
        path = os.path.join(run_dir, "rounds", f"{round_no:04d}.json")
        try:
            _write_json_atomic(path, record)
        except OSError as exc:
            self._storage_failure(
                exc, f"round write failed for round {round_no}")
        self._rounds_written.add(rel)
        return rel

    def stage_round(self, round_no: int,
                    document: Callable[[], dict | None]) -> None:
        """Stage the pending round document.

        The investigate loop stages the in-flight round right after
        its chat completes. If a later write in the same round fails,
        the failure path (:meth:`_storage_failure`) flushes the staged
        document BEFORE the storage-error close-out seals the run, so
        the pending round lands on disk even though the store is about
        to become unwritable. ``document`` must build exactly the
        record :meth:`write_round` would write, or return ``None``
        when nothing presentable exists (the chat never completed).
        """
        if not isinstance(round_no, int) or isinstance(round_no, bool) \
                or round_no < 1:
            raise ValueError(
                f"round number must be a positive integer, got {round_no!r}")
        self._staged_round = (round_no, document)

    def flush_staged_round(self) -> None:
        """Best-effort write of the staged round (pre-close hook).

        Called from :meth:`_storage_failure` before the storage-error
        close-out and from the entry's failure handler before its own
        close-out attempt. Never raises: a no-op when nothing is
        staged or the round is already written (write-once respected,
        so a re-flush can never crash the failure path), and best
        effort by design -- if the ``rounds/`` write itself fails, the
        failure is swallowed and the close-out proceeds so the
        half-written tree is still preserved and classified.
        """
        staged = self._staged_round
        self._staged_round = None
        if staged is None or not self.run_dir:
            return
        round_no, build = staged
        rel = f"rounds/{round_no:04d}.json"
        if rel in self._rounds_written:
            return
        try:
            document = build()
        except Exception:
            return  # a foreign callable must never mask the failure
        if document is None:
            return
        try:
            _write_json_atomic(
                os.path.join(self.run_dir, "rounds",
                             f"{round_no:04d}.json"), document)
        except OSError:
            return  # best effort: rounds/ writes can fail too
        self._rounds_written.add(rel)

    # ---- close-out ------------------------------------------------------

    def close_run(self, status: str = "completed", **fields: Any) -> None:
        """Finalize the run by atomically writing ``run.json``.

        The summary carries ``run_id``, ``package``, ``started_at``,
        ``finished_at``, ``status``, ``observation_ids``, plus any extra
        ``fields`` (the investigate entry owns the full field set). If
        the write fails, the directory is kept, one minimal
        ``storage-error`` close-out is attempted, and the failure always
        propagates as :class:`StorageError`.
        """
        if not self.run_dir or not self.run_id:
            raise RuntimeError("create() has not been called")
        if self._failed:
            raise StorageError(
                "run storage already failed; cannot close with "
                f"status {status!r}"
            )
        if self._closed:
            raise StorageError("run is already closed")
        if not isinstance(status, str) or not status:
            raise ValueError(
                f"status must be a non-empty string, got {status!r}"
            )
        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "package": self.package,
            "started_at": self.started_at,
            "finished_at": utc_now(),
            "status": status,
            "observation_ids": sorted(self._observation_ids),
        }
        payload.update(fields)
        try:
            _write_json_atomic(os.path.join(self.run_dir, "run.json"), payload)
        except OSError as exc:
            self._storage_failure(exc, "run.json write failed")
        self._closed = True

    def _storage_failure(self, exc: OSError, operation: str) -> NoReturn:
        """Shared failure path for every write surface.

        Keeps the run directory, attempts one minimal ``storage-error``
        close-out, seals the store against further writes, and raises
        :class:`StorageError` chained to the original failure. Nothing
        is ever swallowed: if the close-out also fails, that fact is
        part of the raised message.
        """
        # Pre-close hook: the pending (staged) round lands BEFORE
        # the storage-error close-out seals the run.
        self.flush_staged_round()
        close_error = self._close_with_storage_error(operation, exc)
        self._failed = True
        self._closed = True
        message = f"{operation}: {exc}"
        if close_error is not None:
            message += f"; storage-error close-out also failed: {close_error}"
        raise StorageError(message) from exc

    def _close_with_storage_error(self, operation: str,
                                  error: Exception) -> OSError | None:
        """Attempt the minimal ``storage-error`` close-out.

        Best effort by design: the original failure must surface, so
        this helper never raises and returns its own failure instead.
        The only target is ``run.json``; no marker file is ever created.
        """
        payload = {
            "run_id": self.run_id,
            "package": self.package,
            "started_at": self.started_at,
            "finished_at": utc_now(),
            "status": STORAGE_ERROR_STATUS,
            "storage_error": {"operation": operation, "error": str(error)},
            "observation_ids": sorted(self._observation_ids),
        }
        try:
            _write_json_atomic(os.path.join(self.run_dir, "run.json"), payload)
        except OSError as close_error:
            return close_error
        return None
