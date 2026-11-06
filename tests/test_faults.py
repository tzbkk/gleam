"""A/C fault-injection + half-run linkage + B-class contracts.

Every injected fault must converge to a STRUCTURED Result (or tool
payload) with a stable machine ``errors[0].code`` -- never a traceback
escape. B-class contracts assert what gleam
does NOT add: no custom signal handlers anywhere under ``gleam/`` and
no BrokenPipe noise beyond the OS default (the tests only
prove absence; they never add handling).

Stable error-code inventory produced by the injections below:

- storage write injection (investigate, any write surface)
    -> Result ``status=storage-error``, ``errors[0].code="storage-error"``
- run-directory pre-creation failure (store path is a file)
    -> Result ``status=storage-error``, ``errors[0].code="storage-error"``
- pg8000 driver attribute-chain breakage through the udd entry
    -> Result ``status=failed``, ``errors[0].code="udd-driver-unavailable"``
- pg8000 driver attribute-chain breakage inside investigate ``fetch_udd``
    -> failed TOOL payload ``error.kind="udd-driver-unavailable"`` (the
       run continues; observation outcome ``TRANSPORT_ERROR``)
- pg8000 connect-phase transport failure (every ``Connection()``
  attempt raises) through the udd entry
    -> Result ``status=failed``, ``errors[0].code="udd-connection-error"``
- pg8000 socket ``TimeoutError`` in the connect phase through the udd
  entry
    -> Result ``status=failed``, ``errors[0].code="udd-timeout"``
- both connect-phase injections inside investigate ``fetch_udd``
    -> failed TOOL payloads ``error.kind="udd-connection-error"`` /
      ``"udd-timeout"`` (the run continues; observation outcome
      ``TRANSPORT_ERROR``)
- model ``http.client`` connection refusal (real ModelClient)
    -> Result ``status=failed``/``kind=model-error``,
       ``errors[0].code="model-error"``
- fetch transport-layer connection refusal (public IP literal host)
    -> failed tool payload ``error.kind="connection-error"`` with
       observation outcome ``TRANSPORT_ERROR`` (transport vocabulary)
- illegal URLs (bad IPv6 literal / non-numeric port / out-of-range
  port / in-URL credentials)
    -> failed tool payload ``error.kind="unsafe-destination"`` with
       observation outcome ``ABSTAIN``; one bad endpoint never breaks
       the round
- half-run + check linkage reason codes (storage inference)
    -> ``storage-error-recorded`` (+ ``tmp-residue``) when the
       best-effort ``run.json`` close-out lands (check: kind
       ``store-error``); ``run-json-missing`` + ``tmp-residue`` when
       the close-out also fails (check: kind ``store-incomplete``)

Exit mapping is pinned against ``gleam.core.result.exit_code_for``:
ok -> 0; partial/failed/storage-error -> 1; usage/config
errors -> UsageError path, exit 2, no Result.

Zero network: the model client is doubled via the investigate-entry
fixtures (imported wholesale), the real fetch/model transport failures are
injected at the documented module seams, and the database driver is a
module double (a broken attribute chain, or a ``Connection``
constructor that raises before any network is touched).
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import chdir, redirect_stderr, redirect_stdout
from unittest.mock import patch

from gleam.core.result import UsageError, exit_code_for
from gleam.core.transport.outcomes import ABSTAIN_KINDS, OUTCOMES
from gleam.entries import check as check_entry
from gleam.entries import investigate as inv
from gleam.entries import udd as udd_entry
from tests.test_entry_investigate import (
    AssistantTurn,
    ChatResult,
    ToolCall,
    completed_script,
    model_patch,
    only_run_dir,
    prose_result,
    read_run_json,
    run_kwargs,
    tool_call_turn,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def multi_tool_call_turn(calls):
    """One assistant turn carrying several tool calls (same contract as
    ``tool_call_turn`` but with a tuple of (id, name, args) entries)."""

    tools = tuple(
        ToolCall(id=call_id, name=name, arguments_raw=json.dumps(args))
        for call_id, name, args in calls)
    turn = AssistantTurn(content=None, finish_reason="tool_calls",
                         tool_calls=tools, safety_failure=False)
    return ChatResult(model="q",
                      endpoint="http://127.0.0.1:1/chat/completions",
                      status=200, body="{}", turn=turn, error=None,
                      duration_s=0.0)


class RefusedConnection:
    """Connection double whose request blows up at the transport layer
    (below HTTP), replacing the net module's connection factory."""

    def __init__(self, *args, **kwargs):
        pass

    def request(self, *args, **kwargs):
        raise ConnectionRefusedError("injected transport failure")

    def getresponse(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("request must fail before a response")

    def close(self):
        pass


def tool_payloads(script, round_index):
    """Tool-result payloads of one seen conversation snapshot."""

    return [json.loads(m["content"])
            for m in script.seen_messages[round_index]
            if m.get("role") == "tool"]


# ---------------------------------------------------------------------------
# A-class: storage write injections
# ---------------------------------------------------------------------------

class StorageWriteFaults(unittest.TestCase):
    def test_write_observation_injection_converges_storage_error(self):
        """QA scenario 1: patching the real storage seam
        ``gleam.core.storage.RunStore.write_observation`` with OSError
        mid-run converges to a top-level storage-error Result with the
        stable code ``storage-error``; no exception escapes run()."""

        with tempfile.TemporaryDirectory() as store:
            with completed_script():
                with patch(
                    "gleam.core.storage.RunStore.write_observation",
                    side_effect=OSError("disk full"),
                ):
                    result = inv.run(**run_kwargs(store))
        self.assertEqual(result.status, "storage-error")
        self.assertEqual(result.kind, "storage-error")
        self.assertEqual(result.errors[0].code, "storage-error")
        self.assertEqual(result.errors[0].where, "investigate")
        self.assertEqual(exit_code_for(result.status), 1)
        # run() returning (instead of raising) IS the no-escape proof

    def test_midrun_observation_failure_half_run_and_check_linkage(self):
        """Cross-entry linkage: observation writes fail at
        ``os.replace`` while every other atomic write still lands -> the
        half-written run dir is PRESERVED, the pending round document
        is flushed by the store's pre-close hook BEFORE the
        storage-error close-out (``rounds/0001.json`` exists), and the
        REAL check entry reports the same store as kind
        ``store-error`` / status ``storage-error`` with the run's
        diagnostics carrying the ``storage-error-recorded``
        evidence."""

        real_replace = os.replace

        def fail_observation_replaces(tmp, dst):
            if f"{os.sep}observations{os.sep}" in dst:
                raise OSError("injected disk full")
            return real_replace(tmp, dst)

        with tempfile.TemporaryDirectory() as store:
            with completed_script():
                with patch("gleam.core.storage.runstore.os.replace",
                           side_effect=fail_observation_replaces):
                    result = inv.run(**run_kwargs(store))
            self.assertEqual(result.status, "storage-error")
            self.assertEqual(result.errors[0].code, "storage-error")
            run_dir = only_run_dir(store)
            # half-written directory preserved: the per-source
            # directory layout exists and the leaked observation temp
            # file is itself partial-write evidence
            self.assertTrue(os.path.isdir(os.path.join(run_dir, "rounds")))
            self.assertTrue(any(
                name.startswith(".tmp")
                for name in os.listdir(
                    os.path.join(run_dir, "observations"))))
            # the storage domain's failure path flushed the pending
            # round (rounds/ replaces are NOT injected) before sealing
            # the run -- the round's request and its zero completed
            # tool calls survive in rounds/0001.json
            rounds_listing = os.listdir(os.path.join(run_dir, "rounds"))
            self.assertEqual(rounds_listing, ["0001.json"])
            with open(os.path.join(run_dir, "rounds", "0001.json"),
                      encoding="utf-8") as fh:
                flushed_round = json.load(fh)
            self.assertEqual(flushed_round["kind"], "investigate-round")
            self.assertTrue(flushed_round["request"]["messages"])
            self.assertEqual(flushed_round["tool_calls"], [])
            # best-effort close-out landed (what the frozen
            # implementation writes on a storage failure)
            summary = read_run_json(run_dir)
            self.assertEqual(summary["status"], "storage-error")
            self.assertIn("storage_error", summary)
            # the REAL check entry against the same store
            out = io.StringIO()
            with redirect_stdout(out):
                code = check_entry.main(["--store", store, "--json"])
            self.assertEqual(code, 1)
            self.assertEqual(code, exit_code_for("storage-error"))
            report = json.loads(out.getvalue())
            self.assertEqual(report["kind"], "store-error")
            self.assertEqual(report["status"], "storage-error")
            self.assertEqual(report["data"]["run_count"], 1)
            self.assertEqual(report["data"]["error_count"], 1)
            affected = report["data"]["runs"][0]
            self.assertEqual(affected["status"], "storage-error")
            codes = {item["code"] for item in affected["diagnostics"]}
            self.assertIn("storage-error-recorded", codes)
            self.assertIn("tmp-residue", codes)  # leaked temp of the
            # failed atomic write is itself partial-write evidence

    def test_deeper_failure_closeout_also_fails_check_reports_incomplete(self):
        """Worst case: after three successful atomic writes every
        ``os.replace`` fails, so even the best-effort ``run.json``
        close-out cannot land. investigate still converges to
        storage-error; the half-run is preserved with leaked temp
        residue and NO run.json, which the REAL check entry reports as
        kind ``store-incomplete`` / status ``partial`` with the
        ``run-json-missing`` + ``tmp-residue`` reason codes."""

        with tempfile.TemporaryDirectory() as store:
            with completed_script():
                with patch(
                    "gleam.core.storage.runstore.os.replace",
                    side_effect=[None, None, None]
                    + [OSError("injected disk full")] * 200,
                ):
                    result = inv.run(**run_kwargs(store))
            self.assertEqual(result.status, "storage-error")
            self.assertEqual(result.errors[0].code, "storage-error")
            run_dir = only_run_dir(store)
            self.assertFalse(os.path.exists(
                os.path.join(run_dir, "run.json")))
            # the first three mocked replaces are no-ops (side_effect
            # list returns instead of calling the real os.replace), so
            # every write surface keeps its leaked temp file and NO
            # evidence file ever lands: rounds/ holds its two temps
            self.assertTrue(any(
                name.startswith(".tmp")
                for name in os.listdir(os.path.join(run_dir, "rounds"))))
            self.assertFalse(any(
                name.endswith(".json")
                for name in os.listdir(os.path.join(run_dir, "rounds"))))
            out = io.StringIO()
            with redirect_stdout(out):
                code = check_entry.main(["--store", store, "--json"])
            self.assertEqual(code, 1)
            self.assertEqual(code, exit_code_for("partial"))
            report = json.loads(out.getvalue())
            self.assertEqual(report["kind"], "store-incomplete")
            self.assertEqual(report["status"], "partial")
            self.assertEqual(report["data"]["incomplete_count"], 1)
            affected = report["data"]["runs"][0]
            self.assertIsNone(affected["status"])
            codes = {item["code"] for item in affected["diagnostics"]}
            self.assertEqual(codes,
                             {"run-json-missing", "tmp-residue"})

    def test_pre_creation_failure_end_to_end_exit_via_mapper(self):
        """End-to-end: the store path is a regular
        file, so ``RunStore.create`` fails before any directory exists
        -> main(--json) prints a storage-error Result (code
        ``storage-error``) and exits 1 via the unified mapper (the
        full four-status mapper table is pinned in
        test_result.py)."""

        with tempfile.TemporaryDirectory() as tmp:
            blocker = os.path.join(tmp, "not-a-dir")
            with open(blocker, "w", encoding="utf-8") as fh:
                fh.write("regular file blocks the runs tree")
            out = io.StringIO()
            with redirect_stdout(out):
                code = inv.main([
                    "--package", "wizznic", "--store", blocker,
                    "--model-base", "http://127.0.0.1:1",
                    "--model", "q", "--json"])
            self.assertEqual(code, 1)
            self.assertEqual(code, exit_code_for("storage-error"))
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["status"], "storage-error")
            self.assertEqual(payload["kind"], "storage-error")
            self.assertEqual(payload["errors"][0]["code"],
                             "storage-error")
            self.assertEqual(payload["data"]["run_id"], None)
            # nothing was left behind next to the blocking file
            self.assertEqual(os.listdir(tmp), ["not-a-dir"])


# ---------------------------------------------------------------------------
# Pre-creation replay: missing model configuration (usage path, no Result)
# ---------------------------------------------------------------------------

class ModelConfigFaults(unittest.TestCase):
    def test_model_config_missing_usage_exit_no_result(self):
        """Missing --model/--model-base is a usage error: main returns 2
        (the UsageError class exit code), stderr carries the message,
        stdout carries NO Result, and no run directory appears."""

        with tempfile.TemporaryDirectory() as store:
            before = os.listdir(store)
            out, err = io.StringIO(), io.StringIO()
            # Fresh cwd: an operator's repo-root .env must not supply
            # model config to this missing-config usage path.
            with tempfile.TemporaryDirectory() as cwd, chdir(cwd):
                with redirect_stdout(out), redirect_stderr(err):
                    code = inv.main(["--package", "wizznic",
                                     "--store", store])
            self.assertEqual(code, 2)
            self.assertEqual(code, UsageError.exit_code)
            self.assertEqual(out.getvalue(), "")
            self.assertIn("model", err.getvalue())
            self.assertEqual(os.listdir(store), before)


# ---------------------------------------------------------------------------
# A-class: driver injection
# ---------------------------------------------------------------------------

class DriverUnavailableFaults(unittest.TestCase):
    def test_driver_unavailable_udd_entry_failed_result(self):
        """A broken pg8000 driver module (attribute chain) through the
        REAL udd entry converges to a failed Result with the stable
        code ``udd-driver-unavailable``; exit maps to 1 and no
        exception escapes."""

        with patch("gleam.core.sources.udd.pgdriver.pg8000",
                   types.ModuleType("pg8000")):
            result = udd_entry.run(argv=[
                "--package", "wizznic", "--dataset", "sources",
                "--dsn", "postgresql://udd/udd"])
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.kind, "query-result")
        self.assertEqual(result.errors[0].code, "udd-driver-unavailable")
        self.assertTrue(result.errors[0].message)
        self.assertEqual(exit_code_for(result.status), 1)

    def test_driver_unavailable_inside_investigate_is_failed_tool_payload(self):
        """The same driver breakage INSIDE investigate is an honest
        failed tool payload (``error.kind="udd-driver-unavailable"``)
        with the observation graded ``TRANSPORT_ERROR``: the loop
        continues and the run still closes with a structured Result."""

        with tempfile.TemporaryDirectory() as store:
            from tests.test_entry_investigate import ScriptedModel
            script = ScriptedModel([
                tool_call_turn("c-1", "fetch_udd",
                               {"action": "fetch",
                                "datasets": ["sources"]}),
                prose_result("database is unreachable; stopping"),
            ])
            with patch("gleam.core.sources.udd.pgdriver.pg8000",
                       types.ModuleType("pg8000")), \
                    model_patch(script):
                result = inv.run(**run_kwargs(store))
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.kind, "no-final-report")
            self.assertEqual(result.errors[0].code, "no-final-report")
            payloads = tool_payloads(script, 1)
            self.assertEqual(len(payloads), 1)
            self.assertEqual(payloads[0]["error"]["kind"],
                             "udd-driver-unavailable")
            summary = read_run_json(only_run_dir(store))
            self.assertEqual(summary["counts"]["by_outcome"],
                             {"TRANSPORT_ERROR": 1})


def failing_driver_module(exc):
    """pg8000 module double whose ``native.Connection`` constructor
    raises ``exc`` on EVERY attempt (the encrypted one and the
    plaintext retry alike): the connect-phase fault shape. Nothing
    network-shaped ever runs -- the fake constructor raises first."""

    native = types.ModuleType("pg8000.native")

    class Connection:
        def __init__(self, **kwargs):
            raise exc

    native.Connection = Connection
    module = types.ModuleType("pg8000")
    module.native = native
    return module


class DriverConnectFaults(unittest.TestCase):
    """Connect-phase injections through the same pg8000 seam as the
    driver-unavailable cases: a transport-flavored constructor failure
    classifies ``udd-connection-error`` and a socket ``TimeoutError``
    classifies ``udd-timeout`` (the timeout branch outranks the
    connect branch), at BOTH the udd entry and the fetch_udd tool
    level."""

    def test_connection_error_udd_entry_failed_result(self):
        """Every ``Connection()`` attempt (TLS then plaintext retry)
        raises ConnectionRefusedError through the REAL udd entry ->
        failed Result with the stable code ``udd-connection-error``;
        exit maps to 1 and no exception escapes."""

        with patch("gleam.core.sources.udd.pgdriver.pg8000",
                   failing_driver_module(ConnectionRefusedError(
                       "injected connect refusal"))):
            result = udd_entry.run(argv=[
                "--package", "wizznic", "--dataset", "sources",
                "--dsn", "postgresql://udd/udd"])
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.kind, "query-result")
        self.assertEqual(result.errors[0].code, "udd-connection-error")
        self.assertTrue(result.errors[0].message)
        self.assertEqual(exit_code_for(result.status), 1)

    def test_timeout_udd_entry_failed_result(self):
        """The connect phase raises socket ``TimeoutError`` on both
        attempts -> failed Result with the stable code
        ``udd-timeout``; exit maps to 1 and no exception escapes."""

        with patch("gleam.core.sources.udd.pgdriver.pg8000",
                   failing_driver_module(TimeoutError(
                       "injected socket timeout"))):
            result = udd_entry.run(argv=[
                "--package", "wizznic", "--dataset", "sources",
                "--dsn", "postgresql://udd/udd"])
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.kind, "query-result")
        self.assertEqual(result.errors[0].code, "udd-timeout")
        self.assertTrue(result.errors[0].message)
        self.assertEqual(exit_code_for(result.status), 1)

    def test_connection_error_inside_investigate_is_failed_tool_payload(self):
        """Connect-phase refusal INSIDE investigate is an honest
        failed tool payload (``error.kind="udd-connection-error"``)
        with the observation graded ``TRANSPORT_ERROR``: the loop
        continues and the run still closes with a structured
        Result."""

        with tempfile.TemporaryDirectory() as store:
            from tests.test_entry_investigate import ScriptedModel
            script = ScriptedModel([
                tool_call_turn("c-1", "fetch_udd",
                               {"action": "fetch",
                                "datasets": ["sources"]}),
                prose_result("mirror unreachable; stopping"),
            ])
            with patch(
                "gleam.core.sources.udd.pgdriver.pg8000",
                failing_driver_module(ConnectionRefusedError(
                    "injected connect refusal"))), \
                    model_patch(script):
                result = inv.run(**run_kwargs(store))
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.kind, "no-final-report")
            self.assertEqual(result.errors[0].code, "no-final-report")
            payloads = tool_payloads(script, 1)
            self.assertEqual(len(payloads), 1)
            self.assertEqual(payloads[0]["error"]["kind"],
                             "udd-connection-error")
            summary = read_run_json(only_run_dir(store))
            self.assertEqual(summary["counts"]["by_outcome"],
                             {"TRANSPORT_ERROR": 1})

    def test_timeout_inside_investigate_is_failed_tool_payload(self):
        """Connect-phase ``TimeoutError`` INSIDE investigate is an
        honest failed tool payload (``error.kind="udd-timeout"``)
        with the observation graded ``TRANSPORT_ERROR``: the loop
        continues and the run still closes with a structured
        Result."""

        with tempfile.TemporaryDirectory() as store:
            from tests.test_entry_investigate import ScriptedModel
            script = ScriptedModel([
                tool_call_turn("c-1", "fetch_udd",
                               {"action": "fetch",
                                "datasets": ["sources"]}),
                prose_result("the mirror is too slow; stopping"),
            ])
            with patch(
                "gleam.core.sources.udd.pgdriver.pg8000",
                failing_driver_module(TimeoutError(
                    "injected socket timeout"))), \
                    model_patch(script):
                result = inv.run(**run_kwargs(store))
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.kind, "no-final-report")
            self.assertEqual(result.errors[0].code, "no-final-report")
            payloads = tool_payloads(script, 1)
            self.assertEqual(len(payloads), 1)
            self.assertEqual(payloads[0]["error"]["kind"],
                             "udd-timeout")
            summary = read_run_json(only_run_dir(store))
            self.assertEqual(summary["counts"]["by_outcome"],
                             {"TRANSPORT_ERROR": 1})


# ---------------------------------------------------------------------------
# A-class: transport-layer injections via http.client-class seams
# ---------------------------------------------------------------------------

class TransportFaults(unittest.TestCase):
    def test_model_http_client_connection_refused_converges(self):
        """Real ModelClient with ``gleam.core.model.http.client``
        patched so the connection is refused at the transport layer:
        the run converges to failed/model-error with the stable code
        ``model-error`` (terminal mapping), the partial
        trajectory is closed as run.json status ``model-error``, and no
        exception escapes."""

        with tempfile.TemporaryDirectory() as store:
            with patch("gleam.core.model.http.client") as http_client:
                http_client.HTTPConnection.side_effect = (
                    ConnectionRefusedError("injected connection refusal"))
                result = inv.run(**run_kwargs(store))
            summary = read_run_json(only_run_dir(store))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.kind, "model-error")
        self.assertEqual(result.errors[0].code, "model-error")
        self.assertEqual(result.data["status_detail"], "model-error")
        self.assertEqual(summary["status"], "model-error")

    def test_fetch_transport_error_maps_transport_error_outcome(self):
        """fetch_url against a public IP literal (no DNS lookup) with
        the net connection factory replaced by a refusing double: the
        host-level connection refusal is graded ``TRANSPORT_ERROR``
        (transport vocabulary), the tool payload carries the stable
        ``connection-error`` kind, and the round continues to a
        structured Result."""

        with tempfile.TemporaryDirectory() as store:
            from tests.test_entry_investigate import ScriptedModel
            script = ScriptedModel([
                tool_call_turn("c-1", "fetch_url",
                               {"url": "http://93.184.216.34/x"}),
                prose_result("host unreachable; stopping"),
            ])
            with patch("gleam.core.transport.net._open_connection",
                       return_value=RefusedConnection()), \
                    model_patch(script):
                result = inv.run(**run_kwargs(store))
            summary = read_run_json(only_run_dir(store))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.kind, "no-final-report")
        payloads = tool_payloads(script, 1)
        self.assertEqual(payloads[0]["error"]["kind"], "connection-error")
        self.assertEqual(summary["counts"]["by_outcome"],
                         {"TRANSPORT_ERROR": 1})


# ---------------------------------------------------------------------------
# A-class: illegal destination URLs (never break the round)
# ---------------------------------------------------------------------------

ILLEGAL_URLS = (
    ("bad-ipv6-literal", "http://[::1/x"),
    ("non-numeric-port", "http://example.org:abc/"),
    ("out-of-range-port", "http://example.org:99999/"),
    ("url-credentials", "http://user:secret@example.org/"),
)


class IllegalUrlFaults(unittest.TestCase):
    def test_illegal_urls_abstain_and_never_break_the_round(self):
        """Feeding four illegal URLs (bad IPv6 literal, non-numeric
        port, out-of-range port, in-URL credentials) through the REAL
        fetcher yields four ``unsafe-destination`` tool failures graded
        ``ABSTAIN`` (unsafe-destination is an abstain kind), the
        model receives every error payload, and the very same round
        sequence continues normally to a structured Result."""

        self.assertIn("unsafe-destination", ABSTAIN_KINDS)
        with tempfile.TemporaryDirectory() as store:
            from tests.test_entry_investigate import ScriptedModel
            calls = [(f"c-{i + 1}", "fetch_url", {"url": url})
                     for i, (_label, url) in enumerate(ILLEGAL_URLS)]
            script = ScriptedModel([
                multi_tool_call_turn(calls),
                prose_result("all endpoints were refused as unsafe"),
            ])
            with model_patch(script):
                result = inv.run(**run_kwargs(store))
            summary = read_run_json(only_run_dir(store))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.kind, "no-final-report")
        self.assertEqual(result.errors[0].code, "no-final-report")
        payloads = tool_payloads(script, 1)
        self.assertEqual(len(payloads), len(ILLEGAL_URLS))
        for payload, (_label, url) in zip(payloads, ILLEGAL_URLS):
            self.assertEqual(payload["error"]["kind"],
                             "unsafe-destination", url)
            self.assertEqual(payload["requested_url"], url)
        self.assertEqual(summary["counts"]["by_outcome"],
                         {"ABSTAIN": len(ILLEGAL_URLS)})
        # every observation outcome stays inside the outcome vocabulary
        self.assertTrue(
            set(summary["counts"]["by_outcome"]).issubset(OUTCOMES))


# ---------------------------------------------------------------------------
# B-class contracts: add nothing, assert absence
# ---------------------------------------------------------------------------

class BClassContracts(unittest.TestCase):
    def test_no_signal_handlers_in_gleam_source(self):
        """No custom signal handling exists anywhere in the
        gleam package source (the literal ``signal.`` token must not
        appear in any ``.py`` file under ``gleam/``; only OS defaults
        apply). Implemented as a source walk mirroring the plan's grep
        contract."""

        gleam_root = os.path.join(REPO_ROOT, "src", "gleam")
        violations = []
        seen = 0
        for base, dirs, files in os.walk(gleam_root):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in sorted(files):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(base, name)
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                seen += 1
                if "signal." in text:
                    violations.append(
                        os.path.relpath(path, gleam_root))
        # sanity: the walk really covered the package sources
        self.assertGreater(seen, 30)
        self.assertEqual(violations, [])

    def test_broken_pipe_documented_behavior_no_traceback(self):
        """Documented behavior: ``python -m gleam.entries.help``
        piped to a one-line reader (the ``head -1`` shape) produces NO
        Python traceback on stderr -- zero bytes or only the OS-default
        short message. Nothing is caught or handled to make this pass;
        the test only proves the absence of traceback noise."""

        proc = subprocess.Popen(
            [sys.executable, "-m", "gleam.entries.help"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=REPO_ROOT)
        try:
            first_line = proc.stdout.readline()
            proc.stdout.close()  # the reader exits after one line
            stderr_bytes = proc.stderr.read()
            returncode = proc.wait()
        finally:
            for stream in (proc.stdout, proc.stderr):
                try:
                    if not stream.closed:
                        stream.close()
                except OSError:  # pragma: no cover
                    pass
            if proc.poll() is None:  # pragma: no cover
                proc.kill()
        self.assertTrue(first_line)  # output actually started
        self.assertNotIn(b"Traceback", stderr_bytes)
        # documented short-noise allowance: stderr is either empty or
        # only the interpreter's "Exception ignored" line pair
        for line in stderr_bytes.splitlines():
            if line:
                self.assertIn(b"BrokenPipeError", line)
        self.assertIn(returncode, (0, 1, 120))


if __name__ == "__main__":
    unittest.main()
