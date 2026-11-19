"""Offline tests for the investigate entry.

Zero network: the model client is replaced by an in-module
``ScriptedModel`` (no ``gleam.core.model.ScriptedModel``
exists in the package -- the contract is kept, the double is built
here), the
UDD adapter and the transport fetcher are patched at the entry module
boundary, and every run uses a real temporary store so the managed
storage path (RunStore) is exercised for real.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import tempfile
import unittest
from contextlib import chdir, contextmanager, redirect_stderr, redirect_stdout
from unittest.mock import patch

from gleam.core.model import (
    AssistantTurn,
    ChatResult,
    ToolCall,
    parse_assistant,
)
from gleam.core.result import DiagnosticItem, UsageError, exit_code_for
from gleam.core.sources.bts import BtsPolicy, BtsResult
from gleam.core.sources.github import GithubPolicy, GithubResult
from gleam.core.sources.repology import RepologyPolicy
from gleam.core.sources.searxng import SearxngResult
from gleam.core.sources.udd import UddPolicy, UddResult
from gleam.core.storage import LAYOUT_DIRS, SOURCE_DIRS, RunStore
from gleam.core.transport import FetchOutcome, HopResult
from gleam.core.transport.extract import render_markdown_text
from gleam.entries import check as check_entry
from gleam.entries import investigate as inv


# ---------------------------------------------------------------------------
# Scripted model double (ScriptedModel client contract)
# ---------------------------------------------------------------------------

class ScriptedModel:
    """Pops one scripted ChatResult per chat() call.

    A script entry may be a callable ``messages -> ChatResult`` for
    turns that must cite evidence discovered earlier in the same run
    (observation ids are content hashes and cannot be predicted).
    Honors the client contract: the constructor-captured ``audit``
    callback fires once per chat() with the full request record
    (messages AND tools), so the entry's rounds/NNNN.json round
    document merge is exercised even with the client doubled.
    """

    def __init__(self, turns):
        self.turns = list(turns)
        self.seen_messages = []
        self.audit = None

    def chat(self, messages, tools=None):
        self.seen_messages.append(json.loads(json.dumps(list(messages))))
        step = self.turns.pop(0)
        if callable(step):
            step = step(messages)
        if self.audit is not None:
            turn = step.turn
            self.audit({
                "model": "q",
                "endpoint": "http://127.0.0.1:1/chat/completions",
                "request": {"model": "q", "temperature": 0.0,
                            "max_tokens": 8192,
                            "messages": json.loads(json.dumps(
                                list(messages))),
                            "tools": tools if tools is not None else []},
                "prompt_sha256": "0" * 64,
                "response_status": step.status,
                "response_body_raw": step.body,
                "error": step.error,
                "validation": "error" if step.error else "ok",
                "duration_s": 0.0,
                "finish_reason": (turn.finish_reason
                                  if turn is not None else None),
                "reasoning_content_present": False,
                "safety_failure": bool(turn is not None
                                       and turn.safety_failure),
                "safety_error": (turn.safety_error
                                 if turn is not None
                                 and turn.safety_failure else None),
            })
        return step


def _chat_result(turn, *, status=200, error=None):
    return ChatResult(model="q",
                      endpoint="http://127.0.0.1:1/chat/completions",
                      status=status, body="{}", turn=turn, error=error,
                      duration_s=0.0)


def tool_call_turn(call_id, name, args):
    call = ToolCall(id=call_id, name=name,
                    arguments_raw=json.dumps(args))
    turn = AssistantTurn(content=None, finish_reason="tool_calls",
                         tool_calls=(call,), safety_failure=False)
    return _chat_result(turn)


def safety_turn(message, finish_reason=None):
    return _chat_result(parse_assistant(message, finish_reason))


def prose_result(text):
    turn = AssistantTurn(content=text, finish_reason="stop",
                         tool_calls=(), safety_failure=False)
    return _chat_result(turn)


def transport_error_result(kind="connection-error"):
    return ChatResult(model="q",
                      endpoint="http://127.0.0.1:1/chat/completions",
                      status=None, body="", turn=None,
                      error={"kind": kind,
                             "message": "endpoint unreachable in test"},
                      duration_s=0.0)


QUOTE_FROM_UDD_PAYLOAD = "udd-mirror-readonly-rows"


def finish_from_messages(call_id="c-finish"):
    """Build a VALID finish turn citing the udd evidence already in the
    conversation (observation id + homepage URL + a quote that is a
    substring of the tool payload actually exposed to the model)."""

    def build(messages):
        obs_id = None
        url = None
        for message in reversed(messages):
            if message.get("role") != "tool":
                continue
            payload = json.loads(message["content"])
            obs_id = payload.get("observation_id")
            if obs_id:
                match = re.search(r"https://[a-zA-Z0-9./_-]+",
                                  message["content"])
                if match:
                    url = match.group(0)
                break
        if not obs_id or not url:
            raise AssertionError("no citable udd evidence in messages")
        report = {
            "summary": "upstream presence found",
            "candidates": [{
                "url": url,
                "why": "homepage in the UDD sources row",
                "supporting_obs_ids": [obs_id],
                "quote": QUOTE_FROM_UDD_PAYLOAD,
                "uncertainty": "low",
            }],
            "uncertainties": "none for the test",
            "fact_evidence": ["udd:sources#row=1"],
        }
        return tool_call_turn(call_id, "finish_investigation", report)

    return build


def forged_finish_turn(call_id):
    report = {
        "summary": "forged report",
        "candidates": [{
            "url": "https://memory-only.example/forged",
            "why": "from model memory",
            "supporting_obs_ids": ["does-not-exist"],
            "quote": "THIS QUOTE NEVER APPEARED IN ANY TOOL RESULT",
        }],
        "uncertainties": "none",
    }
    return tool_call_turn(call_id, "finish_investigation", report)


# ---------------------------------------------------------------------------
# Adapter doubles
# ---------------------------------------------------------------------------

def make_udd_result(homepage="https://wizznic.example/upstream"):
    return UddResult(
        status="ok", package="wizznic", release=None,
        datasets=("sources",),
        rows=[{"source": "wizznic", "version": "1.1",
               "homepage": homepage}],
        provenance=["udd:sources#row=1"], row_count=1,
        dataset_info={"sources": {"row_count": 1, "limit": 20,
                                  "more_available": False,
                                  "hidden_row_count": 0}},
        dsn_display="postgresql://udd-mirror.debian.net/udd",
        fetched_at="2026-01-01T00:00:00Z",
    )


class FakeFetcher:
    """Deterministic fetcher double (no network, fixed outcome)."""

    def __init__(self, body: bytes, url="http://example.org/x",
                 content_type="text/plain"):
        self.policy = {"max_bytes": 262144, "timeout_s": 15.0,
                       "max_redirects": 3, "max_requests": 12,
                       "min_interval_s": 1.0, "user_agent": "test"}
        self.request_count = 0
        self._robots_bodies = {}
        self._url = url
        self._body = body
        self._content_type = content_type

    def fetch(self, url, headers=None):
        self.request_count += 1
        hop = HopResult(url=self._url, status=200,
                        headers={"content-type": [self._content_type]},
                        body=self._body)
        return FetchOutcome(ok=True, status=200, final_url=self._url,
                            redirect_chain=[], body=self._body,
                            hops=[hop])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_kwargs(store):
    return {"package": "wizznic", "store": store,
            "model_base": "http://127.0.0.1:1", "model": "q"}


def udd_patch():
    return patch("gleam.entries.investigate.UddClient")


def model_patch(script):
    def factory(*args, **kwargs):
        script.audit = kwargs.get("audit")
        return script
    return patch("gleam.entries.investigate.ModelClient",
                 side_effect=factory)


@contextmanager
def completed_script(udd_result=None):
    """QA scenario 1 wiring: fetch_udd turn + dynamic valid finish."""
    with udd_patch() as udd_cls:
        udd_cls.return_value.query.return_value = (
            udd_result or make_udd_result())
        script = ScriptedModel([
            lambda messages: (_assert_not_called_yet(udd_cls),
                              tool_call_turn(
                                  "c-1", "fetch_udd",
                                  {"action": "fetch",
                                   "datasets": ["sources"]}))[1],
            finish_from_messages(),
        ])
        with model_patch(script):
            yield script, udd_cls


def _assert_not_called_yet(udd_cls):
    if udd_cls.call_count != 0:
        raise AssertionError("UDD client constructed before first turn")


def tree_snapshot(root):
    """Path -> (size, sha256) snapshot of a directory tree."""
    snapshot = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(base, name)
            with open(path, "rb") as handle:
                data = handle.read()
            rel = os.path.relpath(path, root)
            import hashlib
            snapshot[rel] = (len(data),
                             hashlib.sha256(data).hexdigest())
    return snapshot


def only_run_dir(store):
    entries = sorted(e for e in os.listdir(store)
                     if os.path.isdir(os.path.join(store, e)))
    assert len(entries) == 1, entries
    return os.path.join(store, entries[0])


def read_run_json(run_dir):
    with open(os.path.join(run_dir, "run.json"), encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class InvestigateCompletedPath(unittest.TestCase):
    def test_completed_full_path(self):
        """QA scenario 1: scripted completed path, zero network, real
        tmp store -> ok/completed + full managed-storage tree."""
        with tempfile.TemporaryDirectory() as store:
            with completed_script() as (script, udd_cls):
                result = inv.run(**run_kwargs(store))
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.kind, "completed")
            self.assertEqual(result.data["status_detail"], "completed")
            self.assertIn("ok", result.data["final_report_validation"])
            udd_cls.return_value.query.assert_called_once()
            run_dir = only_run_dir(store)
            for piece in ("run.json", "rounds", "observations",
                          "fetch_url", "search_web", "github", "fetch_bug",
                          "fetch_udd", "repology", "robots"):
                self.assertTrue(
                    os.path.exists(os.path.join(run_dir, piece)), piece)
            summary = read_run_json(run_dir)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["status_detail"], "completed")
            by_name = summary["counts"]["tool_calls_by_name"]
            self.assertEqual(by_name["fetch_udd"]["calls"], 1)
            self.assertEqual(by_name["fetch_udd"]["errors"], 0)
            self.assertEqual(summary["counts"]["rounds"], 2)
            self.assertEqual(summary["package"], "wizznic")
            # udd row citations registered for audit back-reference
            citations = summary["fact_citation_index"]
            self.assertIsInstance(citations, dict)
            for obs_ids in citations.values():
                self.assertIsInstance(obs_ids, list)
                for obs_id in obs_ids:
                    self.assertIsInstance(obs_id, str)
            # one merged round document per round: the audit
            # record mapped into the uniform shape plus the embedded
            # tool results
            rounds_dir = os.path.join(run_dir, "rounds")
            self.assertEqual(sorted(os.listdir(rounds_dir)),
                             ["0001.json", "0002.json"])
            with open(os.path.join(rounds_dir, "0001.json"),
                      encoding="utf-8") as fh:
                round_doc = json.load(fh)
            self.assertNotIn("call_id", round_doc)
            self.assertEqual(round_doc["kind"], "investigate-round")
            self.assertEqual(round_doc["round"], 1)
            self.assertEqual(round_doc["model"], "q")
            self.assertTrue(round_doc["endpoint"])
            self.assertEqual(round_doc["prompt_sha256"], "0" * 64)
            self.assertEqual(round_doc["finish_reason"], "tool_calls")
            self.assertIn("tools", round_doc["request"])
            self.assertIn("messages", round_doc["request"])
            self.assertTrue(round_doc["request"]["messages"])
            self.assertIn("validation", round_doc["response"])
            self.assertIsNone(round_doc["response"]["error"])
            self.assertEqual(round_doc["response"]["validation"], "ok")
            self.assertIsNone(round_doc["error"])
            self.assertFalse(round_doc["safety_failure"])
            self.assertIsNone(round_doc["safety_error"])
            for tc in round_doc["tool_calls"]:
                self.assertIn("name", tc)
                self.assertIn("result", tc)
            self.assertEqual(
                [tc["name"] for tc in round_doc["tool_calls"]],
                ["fetch_udd"])
            udd_call = round_doc["tool_calls"][0]
            self.assertTrue(udd_call["executed"])
            self.assertEqual(udd_call["result_kind"], "ok")
            # the tool RESULT is embedded in the round document (there
            # is no second per-round file)
            self.assertIn("observation_id", udd_call["result"])
            # run.json rounds[] keeps the summary only; the file path
            # is derivable from the round number
            for entry in summary["rounds"]:
                self.assertIn("round", entry)
                self.assertIn("finish_reason", entry)

    def test_run_json_mode_string_uses_the_pg8000_driver(self):
        with tempfile.TemporaryDirectory() as store:
            with completed_script():
                inv.run(**run_kwargs(store))
            summary = read_run_json(only_run_dir(store))
            self.assertEqual(
                summary["mode"]["udd"],
                "live readonly mirror queries (no startup prefetch; "
                "fixed SQL templates via the pg8000 driver)",
            )
            self.assertNotIn("psql", json.dumps(summary).lower())

    def test_zero_prefetch_udd_client_lazy(self):
        """No UDD client construction or query before the first model
        turn; a prose-only run never constructs it at all."""
        with tempfile.TemporaryDirectory() as store:
            with udd_patch() as udd_cls:
                script = ScriptedModel([prose_result("looks dead to me")])
                with model_patch(script):
                    result = inv.run(**run_kwargs(store))
            udd_cls.assert_not_called()
            self.assertEqual(result.kind, "no-final-report")
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.data["status_detail"],
                             "no-final-report")
            self.assertEqual(result.data["fallback_final_prose"],
                             "looks dead to me")


class InvestigateTerminals(unittest.TestCase):
    def test_budget_exhausted_maps_partial(self):
        with tempfile.TemporaryDirectory() as store:
            with udd_patch() as udd_cls:
                udd_cls.return_value.query.return_value = make_udd_result()
                script = ScriptedModel([
                    tool_call_turn("c-1", "fetch_udd",
                                   {"action": "fetch",
                                    "datasets": ["sources"]}),
                    tool_call_turn("c-2", "fetch_udd",
                                   {"action": "fetch",
                                    "datasets": ["sources"]}),
                ])
                with model_patch(script):
                    result = inv.run(max_rounds=2, **run_kwargs(store))
            self.assertEqual(result.status, "partial")
            self.assertEqual(result.kind, "partial")
            self.assertEqual(result.data["status_detail"],
                             "budget-exhausted")
            summary = read_run_json(only_run_dir(store))
            self.assertEqual(summary["status"], "budget-exhausted")
            self.assertEqual(summary["counts"]["rounds"], 2)

    def test_context_budget_exhausted_maps_failed_model_error(self):
        big_body = b"y" * 9000
        with tempfile.TemporaryDirectory() as store:
            fetcher = FakeFetcher(big_body)
            turns = [tool_call_turn(f"c-{i}", "fetch_url",
                                    {"url": "http://example.org/x"})
                     for i in range(1, 9)]
            script = ScriptedModel(turns)
            with patch("gleam.entries.investigate.Fetcher",
                       return_value=fetcher), model_patch(script):
                result = inv.run(max_context_chars=4000,
                                 **run_kwargs(store))
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.kind, "model-error")
            self.assertEqual(result.data["status_detail"],
                             "context-budget-exhausted")
            summary = read_run_json(only_run_dir(store))
            self.assertEqual(summary["status"],
                             "context-budget-exhausted")
            self.assertLessEqual(summary["counts"]["rounds"], 8)
            # No request was sent for the round that could not be
            # capped: fewer model turns than fetch turns scripted.
            self.assertLess(len(script.seen_messages),
                            len(turns))

    def test_model_transport_error_maps_failed_model_error(self):
        with tempfile.TemporaryDirectory() as store:
            script = ScriptedModel([transport_error_result()])
            with model_patch(script):
                result = inv.run(**run_kwargs(store))
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.kind, "model-error")
            self.assertEqual(result.data["status_detail"], "model-error")
            self.assertEqual(result.errors[0].code, "model-error")

    def test_invalid_report_two_repairs_then_terminal(self):
        with tempfile.TemporaryDirectory() as store:
            with udd_patch() as udd_cls:
                udd_cls.return_value.query.return_value = make_udd_result()
                script = ScriptedModel([
                    tool_call_turn("c-1", "fetch_udd",
                                   {"action": "fetch",
                                    "datasets": ["sources"]}),
                    forged_finish_turn("c-2"),
                    forged_finish_turn("c-3"),
                    forged_finish_turn("c-4"),
                ])
                with model_patch(script):
                    result = inv.run(**run_kwargs(store))
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.kind, "invalid-report")
            self.assertEqual(result.data["status_detail"],
                             "invalid-report")
            self.assertIsInstance(result.data["final_report_validation"],
                                  list)
            summary = read_run_json(only_run_dir(store))
            self.assertEqual(summary["status"], "invalid-report")
            self.assertEqual(summary["counts"]["rounds"], 4)
            finish_rounds = [r for r in summary["rounds"]
                             if any(tc["name"] == "finish_investigation"
                                    for tc in r["tool_calls"])]
            self.assertEqual(len(finish_rounds), 3)
            # the first two rejections were fed back as tool results
            tool_contents = [m["content"] for m in script.seen_messages[-1]
                             if m.get("role") == "tool"]
            self.assertEqual(
                sum('"accepted": false' in content
                    for content in tool_contents), 2)
            self.assertFalse(any('"accepted": true' in content
                                 for content in tool_contents))

    def test_malformed_envelope_zero_tools_loop_continues(self):
        with tempfile.TemporaryDirectory() as store:
            with udd_patch() as udd_cls:
                udd_cls.return_value.query.return_value = make_udd_result()
                bad_message = {"content": "", "tool_calls": [
                    {"id": "dup", "function": {
                        "name": "fetch_url", "arguments": "{}"}},
                    {"id": "dup", "function": {
                        "name": "search_web", "arguments": "{}"}},
                ]}
                script = ScriptedModel([
                    safety_turn(bad_message),
                    tool_call_turn("c-2", "fetch_udd",
                                   {"action": "fetch",
                                    "datasets": ["sources"]}),
                    finish_from_messages(),
                ])
                with model_patch(script):
                    result = inv.run(**run_kwargs(store))
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.kind, "completed")
            summary = read_run_json(only_run_dir(store))
            first = summary["rounds"][0]
            self.assertEqual(first["error"]["kind"],
                             "malformed-tool-calls")
            self.assertEqual(first["tool_calls"], [])
            # the safety-rejected round writes the SAME uniform
            # document as a tool-carrying round -- one file,
            # safety_failure set, zero tool calls, no second file
            rounds_dir = os.path.join(only_run_dir(store), "rounds")
            self.assertEqual(sorted(os.listdir(rounds_dir)),
                             ["0001.json", "0002.json", "0003.json"])
            with open(os.path.join(rounds_dir, "0001.json"),
                      encoding="utf-8") as fh:
                first_doc = json.load(fh)
            self.assertTrue(first_doc["safety_failure"])
            self.assertEqual(first_doc["safety_error"]["kind"],
                             "malformed-tool-calls")
            self.assertEqual(first_doc["error"]["kind"],
                             "malformed-tool-calls")
            self.assertEqual(first_doc["tool_calls"], [])
            self.assertTrue(first_doc["request"]["messages"])
            self.assertEqual(first_doc["response"]["validation"], "ok")
            # the safety rejection was fed back to the model
            second_turn_input = script.seen_messages[1]
            self.assertIn("malformed-tool-calls",
                          json.dumps(second_turn_input))
            # zero tools executed in round 1: exactly one udd query total
            udd_cls.return_value.query.assert_called_once()


class InvestigateStorage(unittest.TestCase):
    def test_storage_write_failure_top_level_storage_error(self):
        """QA scenario 2: a mid-run write failure is a TOP-LEVEL
        storage-error with a stable code -- never a tool failure -- and
        the half-written run directory is preserved."""
        with tempfile.TemporaryDirectory() as store:
            with completed_script():
                with patch("gleam.core.storage.RunStore.write_observation",
                           side_effect=OSError("disk full")):
                    result = inv.run(**run_kwargs(store))
            self.assertEqual(result.status, "storage-error")
            self.assertEqual(result.kind, "storage-error")
            self.assertEqual(result.errors[0].code, "storage-error")
            self.assertEqual(result.errors[0].where, "investigate")
            self.assertEqual(exit_code_for(result.status), 1)
            run_dir = only_run_dir(store)
            # the half-written directory stays for read-only reporting
            self.assertTrue(os.path.isdir(run_dir))
            summary = read_run_json(run_dir)
            self.assertEqual(summary["status"], "storage-error")
            # the pending round document was flushed BEFORE the
            # close-out sealed the run (the store never self-sealed on
            # this direct write_observation failure, so the entry's
            # failure handler landed it)
            with open(os.path.join(run_dir, "rounds", "0001.json"),
                      encoding="utf-8") as fh:
                flushed = json.load(fh)
            self.assertEqual(flushed["kind"], "investigate-round")
            self.assertTrue(flushed["request"]["messages"])
            self.assertEqual(flushed["tool_calls"], [])

    def test_existing_runs_untouched(self):
        """An investigate run never reads or modifies existing
        runs under the same store."""
        with tempfile.TemporaryDirectory() as store:
            prior = RunStore.create(store, "oldpkg")
            prior.write_observation({"kind": "synthetic", "seq": 1,
                                     "note": "prior run evidence"})
            prior.close_run(status="completed")
            before = tree_snapshot(store)
            with completed_script():
                result = inv.run(**run_kwargs(store))
            self.assertEqual(result.status, "ok")
            after = tree_snapshot(store)
            preserved = {path: value for path, value in after.items()
                         if path in before}
            self.assertEqual(preserved, before)
            self.assertNotEqual(result.data["run_id"], prior.run_id)
            new_dir = os.path.join(store, result.data["run_id"])
            self.assertTrue(os.path.isdir(new_dir))

REDIRECT_BODY = b"<html><body>final page body after redirect</body></html>"


class RedirectFetcher:
    """Two-hop fetch double: a 301 redirect hop followed by the final
    200 response, shaped exactly like the real Fetcher's history."""

    def __init__(self):
        self.policy = {"max_bytes": 262144, "timeout_s": 15.0,
                       "max_redirects": 3, "max_requests": 12,
                       "min_interval_s": 1.0, "user_agent": "test"}
        self.request_count = 0
        self._robots_bodies = {}
        self._start = "http://example.org/start"
        self._final = "https://example.org/final"

    def fetch(self, url, headers=None):
        self.request_count += 1
        hops = [
            HopResult(url=self._start, status=301,
                      pinned_ip="93.184.216.34",
                      headers={"location": [self._final],
                               "content-type": ["text/plain"]},
                      body=b""),
            HopResult(url=self._final, status=200,
                      pinned_ip="93.184.216.34",
                      headers={"content-type": ["text/html"]},
                      body=REDIRECT_BODY),
        ]
        return FetchOutcome(
            ok=True, status=200, final_url=self._final,
            redirect_chain=[{"from": self._start, "to": self._final,
                             "status": 301}],
            body=REDIRECT_BODY, hops=hops)


class RobotsAbstainFetcher:
    """No-response fetch double: a robots disallow abstention (the
    request was never made -- no status, no hops)."""

    def __init__(self):
        self.policy = {"max_bytes": 262144, "timeout_s": 15.0,
                       "max_redirects": 3, "max_requests": 12,
                       "min_interval_s": 1.0, "user_agent": "test"}
        self.request_count = 0
        self._robots_bodies = {}

    def fetch(self, url, headers=None):
        self.request_count += 1
        return FetchOutcome(
            ok=False, final_url=url,
            robots={"result": "disallow",
                    "detail": "robots rules parsed",
                    "url": "http://example.org/robots.txt",
                    "cached": False, "path": "/x"},
            error={"kind": "robots-disallow",
                   "message": "robots disallow"})


ROBOTS_BODY_ALLOW = b"User-agent: *\nAllow: /\n"
ROBOTS_BODY_DISALLOW = b"User-agent: *\nDisallow: /\n"


class RobotsBodyFetcher:
    """Fetch double whose origin HAS a parsed robots body stored in
    ``_robots_bodies`` exactly like net.py stores it, with the
    decision dict riding ``robots`` like the real Fetcher produces."""

    def __init__(self):
        self.policy = {"max_bytes": 262144, "timeout_s": 15.0,
                       "max_redirects": 3, "max_requests": 12,
                       "min_interval_s": 1.0, "user_agent": "test"}
        self.request_count = 0
        self._robots_bodies = {"http://example.org:80": ROBOTS_BODY_ALLOW}

    def _decision(self):
        return {"result": "allowed", "detail": "robots rules parsed",
                "url": "http://example.org:80/robots.txt",
                "cached": False, "path": "/"}

    def fetch(self, url, headers=None):
        self.request_count += 1
        hop = HopResult(url=url, status=200,
                        headers={"content-type": ["text/plain"]},
                        body=b"page body")
        return FetchOutcome(ok=True, status=200, final_url=url,
                            robots=self._decision(), body=b"page body",
                            hops=[hop])


class ChangingRobotsFetcher(RobotsBodyFetcher):
    """No-skip double: the robots body CHANGES before the second
    fetch, so the run stores two distinct robots documents."""

    def __init__(self):
        super().__init__()
        self._fetches = 0

    def fetch(self, url, headers=None):
        self._fetches += 1
        if self._fetches == 2:
            self._robots_bodies["http://example.org:80"] = (
                ROBOTS_BODY_DISALLOW)
        return super().fetch(url, headers=headers)


class RobotsForbiddenFetcher:
    """No-response fetch double with the exact decision net.py
    produces for a robots 403 answer (conservative abstain, no body)."""

    def __init__(self):
        self.policy = {"max_bytes": 262144, "timeout_s": 15.0,
                       "max_redirects": 3, "max_requests": 12,
                       "min_interval_s": 1.0, "user_agent": "test"}
        self.request_count = 0
        self._robots_bodies = {}

    def fetch(self, url, headers=None):
        self.request_count += 1
        return FetchOutcome(
            ok=False, final_url=url,
            robots={"result": "unknown",
                    "detail": "robots status 403; conservative abstain",
                    "url": "http://example.org:80/robots.txt",
                    "cached": False},
            error={"kind": "robots-unknown",
                   "message": "robots status 403; conservative abstain"})


class SourceDocumentTests(unittest.TestCase):
    """Normalized per-source documents: every response-carrying
    observation writes ONE document under its source directory;
    no-response observations carry ``source_document: null`` and write
    no file."""

    def test_redirect_fetch_writes_normalized_document(self):
        """Happy path: a fetch with TWO hops (one redirect) stores one
        fetch_url/<sha>.json envelope with the structured hop headers
        and a payload hash; the observation points at it and keeps no
        body/headers raw refs."""
        with tempfile.TemporaryDirectory() as store:
            fetcher = RedirectFetcher()
            script = ScriptedModel([
                tool_call_turn("c-1", "fetch_url",
                               {"url": "http://example.org/start"}),
                prose_result("done looking"),
            ])
            with patch("gleam.entries.investigate.Fetcher",
                       return_value=fetcher), model_patch(script):
                result = inv.run(**run_kwargs(store))
            self.assertEqual(result.kind, "no-final-report")
            run_dir = only_run_dir(store)
            docs = os.listdir(os.path.join(run_dir, "fetch_url"))
            self.assertEqual(len(docs), 1)
            sha = docs[0][:-len(".json")]
            with open(os.path.join(run_dir, "fetch_url", docs[0]),
                      encoding="utf-8") as fh:
                doc = json.load(fh)
            for key in ("requested_url", "url", "status",
                        "content_sha256"):
                self.assertIn(key, doc)
            self.assertEqual(doc["source"], "fetch_url")
            self.assertEqual(doc["requested_url"],
                             "http://example.org/start")
            self.assertEqual(doc["url"], "https://example.org/final")
            self.assertEqual(doc["status"], 200)
            self.assertEqual(len(doc["hops"]), 2)
            self.assertEqual(doc["hops"][0]["url"],
                             "http://example.org/start")
            self.assertEqual(doc["hops"][0]["status"], 301)
            # structured {name: [values]} headers, never a text dump
            self.assertEqual(doc["hops"][0]["headers"],
                             {"location": ["https://example.org/final"],
                              "content-type": ["text/plain"]})
            self.assertEqual(doc["hops"][1]["url"],
                             "https://example.org/final")
            self.assertEqual(doc["hops"][1]["headers"],
                             {"content-type": ["text/html"]})
            self.assertEqual(doc["hops"][1]["pinned_ip"],
                             "93.184.216.34")
            # content_sha256 hashes the ORIGINAL served bytes
            self.assertEqual(doc["content_sha256"],
                             hashlib.sha256(REDIRECT_BODY).hexdigest())
            self.assertEqual(doc["content_bytes"], len(REDIRECT_BODY))
            self.assertFalse(doc["truncated"])
            self.assertEqual(doc["body_encoding"], "utf-8")
            self.assertEqual(doc["body"], REDIRECT_BODY.decode("utf-8"))
            self.assertEqual(doc["extra"], {})
            observations = os.listdir(
                os.path.join(run_dir, "observations"))
            self.assertEqual(len(observations), 1)
            with open(os.path.join(run_dir, "observations",
                                   observations[0]),
                      encoding="utf-8") as fh:
                rec = json.load(fh)
            self.assertEqual(rec["source_document"],
                             {"source": "fetch_url", "sha256": sha,
                              "path": f"fetch_url/{sha}.json"})
            self.assertNotIn("raw_refs", rec)
            self.assertEqual(rec["outcome"], "OK")
            self.assertEqual(rec["status_code"], 200)
            self.assertEqual(rec["final_url"], "https://example.org/final")

    def test_robots_abstain_writes_no_source_document(self):
        """Failure path: a robots-disallow abstention (no response
        obtained) still writes the observation -- outcome ABSTAIN,
        source_document null -- and creates no file in fetch_url/."""
        with tempfile.TemporaryDirectory() as store:
            fetcher = RobotsAbstainFetcher()
            script = ScriptedModel([
                tool_call_turn("c-1", "fetch_url",
                               {"url": "http://example.org/x"}),
                prose_result("cannot probe"),
            ])
            with patch("gleam.entries.investigate.Fetcher",
                       return_value=fetcher), model_patch(script):
                result = inv.run(**run_kwargs(store))
            self.assertEqual(result.kind, "no-final-report")
            run_dir = only_run_dir(store)
            self.assertEqual(
                os.listdir(os.path.join(run_dir, "fetch_url")), [])
            observations = os.listdir(
                os.path.join(run_dir, "observations"))
            self.assertEqual(len(observations), 1)
            with open(os.path.join(run_dir, "observations",
                                   observations[0]),
                      encoding="utf-8") as fh:
                rec = json.load(fh)
            self.assertEqual(rec["outcome"], "ABSTAIN")
            self.assertIsNone(rec["source_document"])
            self.assertNotIn("raw_refs", rec)

    def test_non_utf8_body_stored_base64(self):
        """Bytes that do not decode as UTF-8 are stored base64
        with body_encoding "base64" while content_sha256 keeps hashing
        the ORIGINAL bytes."""
        raw = b"\x89PNG\r\n\x1a\n\x00\xff\xfe binary payload"
        with tempfile.TemporaryDirectory() as store:
            fetcher = FakeFetcher(raw)
            script = ScriptedModel([
                tool_call_turn("c-1", "fetch_url",
                               {"url": "http://example.org/x"}),
                prose_result("done"),
            ])
            with patch("gleam.entries.investigate.Fetcher",
                       return_value=fetcher), model_patch(script):
                inv.run(**run_kwargs(store))
            run_dir = only_run_dir(store)
            docs = os.listdir(os.path.join(run_dir, "fetch_url"))
            self.assertEqual(len(docs), 1)
            with open(os.path.join(run_dir, "fetch_url", docs[0]),
                      encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertEqual(doc["body_encoding"], "base64")
            self.assertEqual(doc["body"],
                             base64.b64encode(raw).decode("ascii"))
            self.assertEqual(doc["content_sha256"],
                             hashlib.sha256(raw).hexdigest())
            self.assertEqual(doc["content_bytes"], len(raw))


class RobotsProvenanceTests(unittest.TestCase):
    """Robots provenance: every
    parsed robots body lands as its own content-addressed
    ``robots/<sha>.json`` document; the observation's robots entry
    merges the transport decision with ``content_sha256``/``path`` of
    the body it used, null for every outcome without a body; the
    record carries no ``raw_refs`` key."""

    def test_robots_body_stored_and_referenced(self):
        """Happy path: a fetch on a robots-governed origin stores one
        ``robots/<sha>.json`` whose ``content_sha256`` is the body
        hash; the observation entry carries the same hash and path."""
        with tempfile.TemporaryDirectory() as store:
            fetcher = RobotsBodyFetcher()
            script = ScriptedModel([
                tool_call_turn("c-1", "fetch_url",
                               {"url": "http://example.org/x"}),
                prose_result("done"),
            ])
            with patch("gleam.entries.investigate.Fetcher",
                       return_value=fetcher), model_patch(script):
                result = inv.run(**run_kwargs(store))
            self.assertEqual(result.kind, "no-final-report")
            run_dir = only_run_dir(store)
            body_sha = hashlib.sha256(ROBOTS_BODY_ALLOW).hexdigest()
            docs = sorted(os.listdir(os.path.join(run_dir, "robots")))
            self.assertEqual(len(docs), 1)
            with open(os.path.join(run_dir, "robots", docs[0]),
                      encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertEqual(doc["source"], "robots")
            self.assertEqual(doc["origin"], "http://example.org:80")
            self.assertEqual(doc["url"], "http://example.org:80/robots.txt")
            self.assertEqual(doc["content_sha256"], body_sha)
            self.assertEqual(doc["content_bytes"], len(ROBOTS_BODY_ALLOW))
            self.assertEqual(doc["body"], ROBOTS_BODY_ALLOW.decode("utf-8"))
            self.assertEqual(doc["extra"], {})
            observations = os.listdir(
                os.path.join(run_dir, "observations"))
            self.assertEqual(len(observations), 1)
            with open(os.path.join(run_dir, "observations",
                                   observations[0]),
                      encoding="utf-8") as fh:
                rec = json.load(fh)
            self.assertEqual(
                rec["robots"],
                {"origin": "http://example.org:80", "result": "allowed",
                 "detail": "robots rules parsed",
                 "url": "http://example.org:80/robots.txt", "cached": False,
                 "content_sha256": body_sha,
                 "path": f"robots/{docs[0]}"})
            self.assertNotIn("raw_refs", rec)

    def test_changed_robots_body_writes_second_document(self):
        """No-skip rule: a robots body
        that changes mid-run writes a SECOND ``robots/<sha>.json`` and
        the second observation records the NEW hash and path."""
        with tempfile.TemporaryDirectory() as store:
            fetcher = ChangingRobotsFetcher()
            script = ScriptedModel([
                tool_call_turn("c-1", "fetch_url",
                               {"url": "http://example.org/x"}),
                tool_call_turn("c-2", "fetch_url",
                               {"url": "http://example.org/y"}),
                prose_result("done"),
            ])
            with patch("gleam.entries.investigate.Fetcher",
                       return_value=fetcher), model_patch(script):
                result = inv.run(**run_kwargs(store))
            self.assertEqual(result.kind, "no-final-report")
            run_dir = only_run_dir(store)
            docs = sorted(os.listdir(os.path.join(run_dir, "robots")))
            self.assertEqual(len(docs), 2)
            allow_sha = hashlib.sha256(ROBOTS_BODY_ALLOW).hexdigest()
            disallow_sha = hashlib.sha256(ROBOTS_BODY_DISALLOW).hexdigest()
            observations = os.listdir(
                os.path.join(run_dir, "observations"))
            self.assertEqual(len(observations), 2)
            records = {}
            for name in observations:
                with open(os.path.join(run_dir, "observations", name),
                          encoding="utf-8") as fh:
                    loaded = json.load(fh)
                records[loaded["final_url"]] = loaded
            self.assertEqual(sorted(records),
                             ["http://example.org/x",
                              "http://example.org/y"])
            first = records["http://example.org/x"]["robots"]
            second = records["http://example.org/y"]["robots"]
            self.assertEqual(first["content_sha256"], allow_sha)
            self.assertEqual(second["content_sha256"], disallow_sha)
            self.assertNotEqual(first["path"], second["path"])
            self.assertEqual(
                sorted(entry["path"] for entry in (first, second)),
                sorted(f"robots/{doc}" for doc in docs))

    def test_robots_403_abstain_records_null_provenance(self):
        """Failure path: a robots 403 conservative abstain carries the
        transport decision (result "unknown" as net.py produces it)
        with ``content_sha256``/``path`` null and no robots file."""
        with tempfile.TemporaryDirectory() as store:
            fetcher = RobotsForbiddenFetcher()
            script = ScriptedModel([
                tool_call_turn("c-1", "fetch_url",
                               {"url": "http://example.org/x"}),
                prose_result("cannot probe"),
            ])
            with patch("gleam.entries.investigate.Fetcher",
                       return_value=fetcher), model_patch(script):
                result = inv.run(**run_kwargs(store))
            self.assertEqual(result.kind, "no-final-report")
            run_dir = only_run_dir(store)
            self.assertEqual(
                os.listdir(os.path.join(run_dir, "robots")), [])
            observations = os.listdir(
                os.path.join(run_dir, "observations"))
            self.assertEqual(len(observations), 1)
            with open(os.path.join(run_dir, "observations",
                                    observations[0]),
                      encoding="utf-8") as fh:
                rec = json.load(fh)
            # no robots body existed: the provenance entry carries
            # nulls and can never mimic a stored document
            self.assertIsNone(rec["robots"]["content_sha256"])
            self.assertIsNone(rec["robots"]["path"])
            self.assertNotIn("raw_refs", rec)


class InvestigateToolsAndAntiForgery(unittest.TestCase):
    def test_adapter_usage_error_is_failed_tool_payload(self):
        """UDD adapter UsageError converts to a failed TOOL result (the
        loop continues); it is never an entry exit."""
        with tempfile.TemporaryDirectory() as store:
            with udd_patch() as udd_cls:
                udd_cls.return_value.query.side_effect = UsageError(
                    "requested effective rows 130 exceed the per-query "
                    "total row cap 120")
                script = ScriptedModel([
                    tool_call_turn("c-1", "fetch_udd",
                                   {"action": "fetch",
                                    "datasets": ["sources"]}),
                    prose_result("giving up"),
                ])
                with model_patch(script):
                    result = inv.run(**run_kwargs(store))
            self.assertEqual(result.kind, "no-final-report")
            feedback = json.loads(
                script.seen_messages[1][-1]["content"])
            self.assertEqual(feedback["error"]["kind"],
                             "argument-invalid")
            self.assertIn("row cap", feedback["error"]["message"])

    def test_dataset_scope_enforced(self):
        """With an operator-configured dataset scope the model
        cannot select outside it."""
        with tempfile.TemporaryDirectory() as store:
            script = ScriptedModel([
                tool_call_turn("c-1", "fetch_udd",
                               {"action": "fetch",
                                "datasets": ["bugs"]}),
                prose_result("fine"),
            ])
            with model_patch(script):
                inv.run(datasets=("sources",), **run_kwargs(store))
            feedback = json.loads(
                script.seen_messages[1][-1]["content"])
            self.assertEqual(feedback["error"]["kind"],
                             "argument-out-of-scope")

    def test_read_evidence_extends_exposure(self):
        """A quote from a read_evidence window (never in the first
        payload) validates: exposure tracking registers windows."""
        body = b"".join(b"line-%04d of the fetched page\n" % i
                        for i in range(700))  # ~25k chars
        with tempfile.TemporaryDirectory() as store:
            fetcher = FakeFetcher(body)

            def finish_after_read(messages):
                window = None
                for message in reversed(messages):
                    if message.get("role") != "tool":
                        continue
                    payload = json.loads(message["content"])
                    if payload.get("evidence_kind") == "page-fetch":
                        window = payload
                        break
                if window is None:
                    return tool_call_turn(
                        "c-read", "read_evidence",
                        {"observation_id": _first_obs_id(messages),
                         "offset": 15000})
                text = window["body_text"]
                quote = " ".join(text.split()[12:22])
                report = {
                    "summary": "deep quote validated",
                    "candidates": [{
                        "url": "http://example.org/x",
                        "why": "page text",
                        "supporting_obs_ids":
                            [window["observation_id"]],
                        "quote": quote,
                    }],
                    "uncertainties": "none",
                }
                return tool_call_turn("c-fin", "finish_investigation",
                                      report)

            script = ScriptedModel([
                tool_call_turn("c-1", "fetch_url",
                               {"url": "http://example.org/x"}),
                finish_after_read,
                finish_after_read,
            ])
            with patch("gleam.entries.investigate.Fetcher",
                       return_value=fetcher), model_patch(script):
                result = inv.run(**run_kwargs(store))
            self.assertEqual(result.status, "ok",
                             result.data["detail"])
            self.assertEqual(result.kind, "completed")


def _first_obs_id(messages):
    for message in messages:
        if message.get("role") != "tool":
            continue
        payload = json.loads(message["content"])
        if payload.get("observation_id"):
            return payload["observation_id"]
    raise AssertionError("no observation id in messages")


def _assert_no_rows_key(node, path="record"):
    """Guard: the observation record must not carry rows anywhere."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "rows":
                raise AssertionError(
                    f"unexpected 'rows' key at {path}.{key}")
            _assert_no_rows_key(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _assert_no_rows_key(item, f"{path}[{index}]")


class UddRowsExternalization(unittest.TestCase):
    """Rows live in fetch_udd/<sha>.json documents; the
    observation links the document and summarizes counts only."""

    @staticmethod
    def _two_dataset_result():
        return UddResult(
            status="ok", package="wizznic", release=None,
            datasets=("sources", "upstream"),
            rows=[
                {"source": "wizznic", "version": "1.1", "release": "sid",
                 "maintainer": "", "bin": "", "vcs_type": "",
                 "vcs_url": "", "vcs_browser": "", "section": "games",
                 "priority": "optional",
                 "homepage": "https://wizznic.example/upstream"},
                {"source": "wizznic", "version": "1.1", "release": "sid",
                 "status": "up to date", "upstream_version": "1.1",
                 "upstream_url": "https://wizznic.example/upstream",
                 "watch_file": "", "errors": "",
                 "last_check": "2026-01-01"},
                {"source": "wizznic", "version": "1.0", "release": "sid",
                 "status": "newer upstream", "upstream_version": "1.2",
                 "upstream_url": "", "watch_file": "", "errors": "",
                 "last_check": "2025-06-01"},
            ],
            provenance=["udd:sources#row=1", "udd:upstream#row=1",
                        "udd:upstream#row=2"],
            row_count=3,
            dataset_info={
                "sources": {"row_count": 1, "limit": 20,
                            "more_available": False,
                            "hidden_row_count": 0},
                "upstream": {"row_count": 2, "limit": 20,
                             "more_available": True,
                             "hidden_row_count": 1}},
            dsn_display="postgresql://udd-mirror.debian.net/udd",
            fetched_at="2026-01-01T00:00:00Z",
        )

    def test_rows_in_document_counts_in_observation(self):
        """Happy path: two datasets, one truncated -> one fetch_udd
        document with the rows; the observation carries counts,
        truncation flags and the document link, never rows; the
        model-facing tool payload keeps the rows."""
        with tempfile.TemporaryDirectory() as store:
            with udd_patch() as udd_cls:
                udd_cls.return_value.query.return_value = \
                    self._two_dataset_result()
                script = ScriptedModel([
                    tool_call_turn("c-1", "fetch_udd",
                                   {"action": "fetch",
                                    "datasets": ["sources", "upstream"]}),
                    finish_from_messages(),
                ])
                with model_patch(script):
                    result = inv.run(**run_kwargs(store))
            self.assertEqual(result.status, "ok", result.data["detail"])
            run_dir = only_run_dir(store)
            udd_files = sorted(os.listdir(
                os.path.join(run_dir, "fetch_udd")))
            self.assertEqual(len(udd_files), 1)
            with open(os.path.join(run_dir, "fetch_udd", udd_files[0]),
                      encoding="utf-8") as fh:
                doc = json.load(fh)
            # document shape (a row-bearing build, no HTTP envelope)
            self.assertEqual(doc["source"], "fetch_udd")
            self.assertEqual(doc["status"], "ok")
            self.assertEqual(doc["hops"], [])
            self.assertEqual(doc["body"], "")
            self.assertEqual(
                doc["requested_url"],
                "postgresql://udd-mirror.debian.net/udd")
            self.assertEqual(doc["url"], doc["requested_url"])
            self.assertTrue(doc["truncated"])  # upstream is truncated
            self.assertEqual(sorted(doc["extra"]), ["datasets", "query"])
            self.assertEqual(doc["extra"]["query"], {
                "package": "wizznic",
                "datasets": ["sources", "upstream"],
                "release": None, "limit": 20})
            datasets = doc["extra"]["datasets"]
            self.assertEqual(sorted(datasets),
                             ["sources", "upstream"])
            self.assertEqual(datasets["sources"]["columns"][0],
                             "source")
            self.assertEqual(len(datasets["sources"]["rows"]), 1)
            self.assertEqual(len(datasets["upstream"]["rows"]), 2)
            self.assertEqual(datasets["sources"]["provenance"],
                             ["udd:sources#row=1"])
            self.assertFalse(datasets["sources"]["truncated"])
            self.assertTrue(datasets["upstream"]["truncated"])
            # the observation links the document and counts only
            observations = os.listdir(
                os.path.join(run_dir, "observations"))
            self.assertEqual(len(observations), 1)
            with open(os.path.join(run_dir, "observations",
                                   observations[0]),
                      encoding="utf-8") as fh:
                rec = json.load(fh)
            self.assertEqual(
                rec["source_document"],
                {"source": "fetch_udd",
                 "sha256": udd_files[0][:-len(".json")],
                 "path": f"fetch_udd/{udd_files[0]}"})
            # the file name still equals the hash of its exact bytes
            with open(os.path.join(run_dir, "fetch_udd", udd_files[0]),
                      "rb") as fh:
                stored_bytes = fh.read()
            self.assertEqual(rec["source_document"]["sha256"],
                             hashlib.sha256(stored_bytes).hexdigest())
            self.assertEqual(rec["extra"]["row_counts"], {
                "sources": {"row_count": 1, "truncated": False},
                "upstream": {"row_count": 2, "truncated": True}})
            _assert_no_rows_key(rec)
            # the model-facing payload keeps the rows exactly as before
            with open(os.path.join(run_dir, "rounds", "0001.json"),
                      encoding="utf-8") as fh:
                round_doc = json.load(fh)
            tool_result = round_doc["tool_calls"][0]["result"]
            self.assertEqual(
                len(tool_result["datasets_data"]["sources"]["rows"]), 1)
            self.assertEqual(
                len(tool_result["datasets_data"]["upstream"]["rows"]), 2)

    def test_failed_result_no_document_no_rows(self):
        """Failure path: a failed UddResult observes with
        source_document: null, writes no fetch_udd file, embeds no
        rows."""
        failed = UddResult(
            status="failed", package="wizznic", release=None,
            datasets=(), rows=[], provenance=[], row_count=0,
            errors=[DiagnosticItem(
                code="udd-query-error", where="gleam.core.sources.udd",
                message="driver failed")],
            dsn_display="postgresql://udd-mirror.debian.net/udd",
            fetched_at="2026-01-01T00:00:00Z",
        )
        with tempfile.TemporaryDirectory() as store:
            with udd_patch() as udd_cls:
                udd_cls.return_value.query.return_value = failed
                script = ScriptedModel([
                    tool_call_turn("c-1", "fetch_udd",
                                   {"action": "fetch",
                                    "datasets": ["sources"]}),
                    prose_result("cannot verify anything"),
                ])
                with model_patch(script):
                    inv.run(**run_kwargs(store))
            run_dir = only_run_dir(store)
            self.assertEqual(
                os.listdir(os.path.join(run_dir, "fetch_udd")), [])
            observations = os.listdir(
                os.path.join(run_dir, "observations"))
            self.assertEqual(len(observations), 1)
            with open(os.path.join(run_dir, "observations",
                                   observations[0]),
                      encoding="utf-8") as fh:
                rec = json.load(fh)
            self.assertIsNone(rec["source_document"])
            self.assertNotIn("extra", rec)
            _assert_no_rows_key(rec)


class BugFetchSingleArtifactTests(unittest.TestCase):
    """ONE fetch_bug/<sha>.json artifact per BTS fetch -- the
    observation's own normalized envelope; the payload's raw_ref
    points at it."""

    BUG_PAGE = "https://bugs.debian.org/1000000"

    @staticmethod
    def _ok_result(body: bytes) -> BtsResult:
        return BtsResult(
            status="ok", bug_id=1000000, source="soap",
            messages=[{
                "index": 0, "msg_num": 1, "message_id": None,
                "date": "Mon, 1 Jan 2026 00:00:00 UTC",
                "date_iso": "2026-01-01T00:00:00Z",
                "from": "reporter@example.org",
                "subject": "crash on amd64",
                "subject_decoded": "crash on amd64",
                "body_text": "crash on amd64\nsee "
                             "https://example.org/trace",
                "body_chars": 37, "body_truncated": False,
                "html_only": False, "charsets": ["us-ascii"],
                "attachments": [], "parse_defects": [],
                "urls": ["https://example.org/trace"],
                "source_url": BugFetchSingleArtifactTests.BUG_PAGE,
                "raw": {"offset": 0, "bytes": len(body),
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "boundary": "soap-header-body"},
                "provenance_note": "parsed from preserved bytes",
            }],
            message_count=1,
            provenance=[BugFetchSingleArtifactTests.BUG_PAGE],
            source_selection={"requested": "auto", "chosen": "soap"},
            attempts=[{"source": "soap", "outcome": "ok"}],
            url=BugFetchSingleArtifactTests.BUG_PAGE,
            bug_page_url=BugFetchSingleArtifactTests.BUG_PAGE,
            http_status=200,
            fetched_at="2026-01-01T00:00:00Z",
            content_bytes=len(body),
            content_sha256=hashlib.sha256(body).hexdigest(),
            body_bytes=body,
        )

    @staticmethod
    def _failed_result() -> BtsResult:
        return BtsResult(
            status="failed", bug_id=1000000, source="auto",
            errors=[DiagnosticItem(
                code="bts-transport", where="gleam.core.sources.bts",
                message="SOAP endpoint unreachable")],
            url=BugFetchSingleArtifactTests.BUG_PAGE,
            bug_page_url=BugFetchSingleArtifactTests.BUG_PAGE,
            fetched_at="2026-01-01T00:00:00Z",
        )

    def test_one_artifact_raw_ref_points_at_the_document(self):
        """Happy path: a scripted BTS implementation call writes exactly
        one fetch_bug/<sha>.json file (the observation's own envelope)
        and no second artifact anywhere; the payload's raw_ref
        string-equals the observation's source_document.path; its
        rounds carry no tools_file key."""
        body = (b"<?xml version='1.0'?><soap:Envelope>bug 1000000 "
                b"discussion</soap:Envelope>")
        with tempfile.TemporaryDirectory() as store:
            with patch("gleam.entries.investigate.BtsClient") as bts_cls:
                client = bts_cls.return_value
                client.fetch.return_value = self._ok_result(body)
                client.request_count = 1
                script = ScriptedModel([
                    tool_call_turn("c-1", "fetch_bug",
                                   {"bug_id": 1000000}),
                    prose_result("done looking"),
                ])
                with model_patch(script):
                    result = inv.run(**run_kwargs(store))
            self.assertEqual(result.kind, "no-final-report")
            run_dir = only_run_dir(store)
            docs = os.listdir(os.path.join(run_dir, "fetch_bug"))
            self.assertEqual(len(docs), 1)
            sha = docs[0][:-len(".json")]
            self.assertEqual(len(sha), 64)
            # no stray second artifact: the layout dir set is exactly
            # the nine per-source/round/observation dirs, and the only
            # BTS-capable dir holds the single sha-named document.
            self.assertEqual(
                sorted(d for d in os.listdir(run_dir)
                       if os.path.isdir(os.path.join(run_dir, d))),
                sorted(LAYOUT_DIRS))
            with open(os.path.join(run_dir, "fetch_bug", docs[0]),
                      encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertEqual(doc["source"], "fetch_bug")
            self.assertEqual(doc["status"], 200)
            self.assertEqual(doc["url"], self.BUG_PAGE)
            self.assertEqual(doc["extra"],
                             {"bug_id": 1000000, "source": "auto"})
            self.assertEqual(doc["content_sha256"],
                             hashlib.sha256(body).hexdigest())
            self.assertEqual(doc["content_bytes"], len(body))
            self.assertEqual(doc["body_encoding"], "utf-8")
            self.assertEqual(doc["body"], body.decode("utf-8"))
            observations = os.listdir(
                os.path.join(run_dir, "observations"))
            self.assertEqual(len(observations), 1)
            with open(os.path.join(run_dir, "observations",
                                   observations[0]),
                      encoding="utf-8") as fh:
                rec = json.load(fh)
            self.assertEqual(
                rec["source_document"],
                {"source": "fetch_bug", "sha256": sha,
                 "path": f"fetch_bug/{sha}.json"})
            self.assertNotIn("raw_refs", rec)
            with open(os.path.join(run_dir, "rounds", "0001.json"),
                      encoding="utf-8") as fh:
                round_doc = json.load(fh)
            payload = round_doc["tool_calls"][0]["result"]
            self.assertIsInstance(payload["raw_ref"], str)
            self.assertEqual(payload["raw_ref"],
                             rec["source_document"]["path"])
            run_json = read_run_json(run_dir)
            self.assertNotIn("layout", run_json)
            for entry in run_json["rounds"]:
                self.assertNotIn("tools_file", entry)

    def test_failed_result_no_artifact_no_observation(self):
        """Failure path: a failed BTS result returns the honest error
        payload and writes nothing -- no fetch_bug artifact and no
        observation (a BTS failure never observes; unlike UDD there is
        no source_document: null record to pin because the failure
        branch returns before _observe)."""
        with tempfile.TemporaryDirectory() as store:
            with patch("gleam.entries.investigate.BtsClient") as bts_cls:
                client = bts_cls.return_value
                client.fetch.return_value = self._failed_result()
                client.request_count = 2
                script = ScriptedModel([
                    tool_call_turn("c-1", "fetch_bug",
                                   {"bug_id": 1000000}),
                    prose_result("cannot verify"),
                ])
                with model_patch(script):
                    inv.run(**run_kwargs(store))
            run_dir = only_run_dir(store)
            self.assertEqual(
                os.listdir(os.path.join(run_dir, "fetch_bug")), [])
            self.assertEqual(
                os.listdir(os.path.join(run_dir, "observations")), [])
            with open(os.path.join(run_dir, "rounds", "0001.json"),
                      encoding="utf-8") as fh:
                round_doc = json.load(fh)
            payload = round_doc["tool_calls"][0]["result"]
            self.assertEqual(payload["bug_id"], 1000000)
            self.assertEqual(payload["error"]["kind"], "bts-transport")
            self.assertEqual(payload["http_requests_so_far"], 2)
            run_json = read_run_json(run_dir)
            self.assertNotIn("layout", run_json)


class ToolSurfaceTests(unittest.TestCase):
    """The investigation tool surface (github, fetch_bug): names
    outside the surface are rejected as unknown tools by the protocol
    validation, while capability-disabled messages keep naming the
    approved configuration flags."""

    def test_unknown_tool_names_rejected(self):
        with tempfile.TemporaryDirectory() as store:
            script = ScriptedModel([
                tool_call_turn("c-1", "github_api",
                               {"action": "repo", "owner": "o",
                                "repo": "r"}),
                tool_call_turn("c-2", "bug_fetch", {"bug_id": 1000000}),
                prose_result("stopping"),
            ])
            with model_patch(script):
                result = inv.run(**run_kwargs(store))
            self.assertEqual(result.kind, "no-final-report")
            # the final request replays every tool result in order
            feedback = [
                json.loads(m["content"])
                for m in script.seen_messages[-1]
                if m.get("role") == "tool"]
            self.assertEqual([e["error"]["kind"] for e in feedback],
                             ["unknown-tool", "unknown-tool"])
            self.assertIn("'github_api'",
                          feedback[0]["error"]["message"])
            self.assertIn("'bug_fetch'",
                          feedback[1]["error"]["message"])
            for payload in feedback:
                # the available list shows only the approved spellings
                self.assertIn("'github'", payload["error"]["message"])
                self.assertIn("'fetch_bug'", payload["error"]["message"])
            # unknown names never dispatch: no counts, no observations
            summary = read_run_json(only_run_dir(store))
            self.assertEqual(summary["counts"]["tool_calls_by_name"], {})
            self.assertEqual(summary["counts"]["observations"], 0)

    def test_disabled_messages_name_the_config_flags(self):
        with gate_ctx() as ctx:
            ctx.github_base = None
            payload = ctx.tool_github({"action": "repo", "owner": "o",
                                       "repo": "r"})
        self.assertEqual(payload["error"]["kind"], "capability-disabled")
        self.assertIn("--no-github", payload["error"]["message"])
        with gate_ctx() as ctx:
            payload = ctx.tool_fetch_bug({"bug_id": 1000000})
        self.assertEqual(payload["error"]["kind"], "capability-disabled")
        self.assertIn("--no-bts", payload["error"]["message"])


class InvestigateEntryShell(unittest.TestCase):
    def test_main_json_and_exit_codes(self):
        with tempfile.TemporaryDirectory() as store:
            with completed_script():
                out = io.StringIO()
                with redirect_stdout(out):
                    code = inv.main([
                        "--package", "wizznic", "--store", store,
                        "--model-base", "http://127.0.0.1:1",
                        "--model", "q", "--json"])
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["kind"], "completed")
            self.assertEqual(
                list(payload.keys()),
                ["schema_version", "status", "kind", "data", "errors",
                 "diagnostics", "warnings"])

    def test_main_usage_error_exit_two_no_result(self):
        out, err = io.StringIO(), io.StringIO()
        # Fresh cwd: an operator's repo-root .env must not supply model
        # config to this missing-config usage path.
        with tempfile.TemporaryDirectory() as cwd, chdir(cwd):
            with redirect_stdout(out), redirect_stderr(err):
                code = inv.main(["--package", "wizznic"])
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("model", err.getvalue())

    def test_run_never_prints(self):
        with tempfile.TemporaryDirectory() as store:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                with completed_script():
                    result = inv.run(**run_kwargs(store))
            self.assertEqual(result.status, "ok")
            self.assertEqual(out.getvalue(), "")
            self.assertEqual(err.getvalue(), "")


# ---------------------------------------------------------------------------
# Finish-time uncovered-GitHub-repository gate
# ---------------------------------------------------------------------------


def search_result_with_github(*urls):
    return SearxngResult(
        status="ok", query="ncdu fork",
        final_url="http://127.0.0.1:8888/search?q=ncdu+fork",
        http_status=200,
        results=[{"title": "Fork of ncdu", "url": url,
                  "snippet": "Fork of ncdu with extras", "engines": ["bing"]}
                 for url in urls],
        raw_body=b"{}", rendered_text="rendered")


class FakeSearchClient:
    def __init__(self, result):
        self.base = "http://127.0.0.1:8888"
        self._result = result

    def search(self, query):
        return self._result


class FakeGithubClient:
    def __init__(self, result):
        self.request_count = 1
        self._result = result

    def query(self, action, owner, repo, page=None, per_page=None):
        return self._result


def github_ok_result(view=None, evidence_urls=None):
    return GithubResult(
        status="ok", action="repo", owner="BratishkaErik", repo="ncdu",
        api_url="https://api.github.com/repos/BratishkaErik/ncdu",
        final_url="https://api.github.com/repos/BratishkaErik/ncdu",
        http_status=200,
        view=view or {"full_name": "BratishkaErik/ncdu",
                      "html_url": "https://github.com/BratishkaErik/ncdu"},
        evidence_urls=evidence_urls
        or ["https://github.com/BratishkaErik/ncdu"],
        fetched_at="2026-01-01T00:00:00Z")


@contextmanager
def gate_ctx(search_client=None, fetcher=None):
    """One real InvestigationContext on a temp store (no model, no
    network): the coverage ledger is exercised through the real tool
    methods with source-adapter doubles."""

    with tempfile.TemporaryDirectory() as store:
        run_store = RunStore.create(store, "wizznic")
        yield inv.InvestigationContext(
            store=run_store, run_id=run_store.run_id, package="wizznic",
            limits={"max_rounds": 4, "max_tool_calls": 8,
                    "max_body_chars": 40000, "max_links": 120,
                    "max_context_chars": 320000,
                    "max_report_repairs": 2},
            display={"mailto_max": 5, "copyright_max": 5, "footer_max": 5,
                     "max_url_chars": 2048,
                     "max_search_query_chars": 400, "min_quote_chars": 8,
                     "field_char_cap": 200, "report_cell_max_chars": 2000,
                     "report_max_errors": 20},
            udd_dsn="postgresql://udd-mirror.debian.net/udd",
            udd_policy=UddPolicy(),
            udd_dataset_scope=(), default_release=None, udd_limit=None,
            udd_max_calls=2, udd_max_rows=50, udd_enabled=True,
            fetcher=fetcher or FakeFetcher(b"page body"),
            request_cap=10, search_enabled=True,
            search_client=search_client, github_base="https://api.github.com",
            github_policy=GithubPolicy(), bts_enabled=False,
            bts_policy=BtsPolicy(),
            repology_enabled=False, repology_base=None,
            repology_policy=RepologyPolicy(),
            robots_exempt_hosts=frozenset())


@contextmanager
def repology_ctx(request_cap=10, operator_exempt=frozenset(),
                 main_fetcher=None):
    """One real InvestigationContext with the repology tool ENABLED on
    a temp store (no model, no network): the tool runs the REAL
    RepologyClient against a transport-boundary double."""

    with tempfile.TemporaryDirectory() as store:
        run_store = RunStore.create(store, "wizznic")
        yield inv.InvestigationContext(
            store=run_store, run_id=run_store.run_id, package="wizznic",
            limits={"max_rounds": 4, "max_tool_calls": 8,
                    "max_body_chars": 40000, "max_links": 120,
                    "max_context_chars": 320000,
                    "max_report_repairs": 2},
            display={"mailto_max": 5, "copyright_max": 5, "footer_max": 5,
                     "max_url_chars": 2048,
                     "max_search_query_chars": 400, "min_quote_chars": 8,
                     "field_char_cap": 200, "report_cell_max_chars": 2000,
                     "report_max_errors": 20},
            udd_dsn="postgresql://udd-mirror.debian.net/udd",
            udd_policy=UddPolicy(),
            udd_dataset_scope=(), default_release=None, udd_limit=None,
            udd_max_calls=2, udd_max_rows=50, udd_enabled=False,
            fetcher=main_fetcher or FakeFetcher(b"page body"),
            request_cap=request_cap, search_enabled=False,
            search_client=None, github_base=None,
            github_policy=GithubPolicy(), bts_enabled=False,
            bts_policy=BtsPolicy(),
            repology_enabled=True,
            repology_base="https://repology.org",
            repology_policy=RepologyPolicy(
                max_requests=6, min_interval_s=0.0, timeout_s=1.0),
            robots_exempt_hosts=frozenset(operator_exempt))


def report_citing_search(payload, url="https://github.com/BratishkaErik/ncdu",
                         quote=None):
    return {
        "summary": "upstream presence found",
        "candidates": [{
            "url": url,
            "why": "repository seen in search results",
            "supporting_obs_ids": [payload["observation_id"]],
            "quote": quote or url,
        }],
        "uncertainties": "none for the test",
    }


class GithubRepoUrlParser(unittest.TestCase):
    def test_url_shapes(self):
        cases = [
            ("https://github.com/BratishkaErik/ncdu",
             ("bratishkaerik/ncdu", "github.com/BratishkaErik/ncdu")),
            ("https://github.com/yorhel/ncdu/releases/tag/v2.7",
             ("yorhel/ncdu", "github.com/yorhel/ncdu")),
            ("https://github.com/yorhel/ncdu/tree/main/src",
             ("yorhel/ncdu", "github.com/yorhel/ncdu")),
            ("https://github.com/yorhel/ncdu.git",
             ("yorhel/ncdu", "github.com/yorhel/ncdu")),
            ("https://github.com/yorhel/ncdu?tab=readme",
             ("yorhel/ncdu", "github.com/yorhel/ncdu")),
            ("https://github.com/yorhel/ncdu#section",
             ("yorhel/ncdu", "github.com/yorhel/ncdu")),
            ("https://GITHUB.com/Yorhel/NCDU/",
             ("yorhel/ncdu", "github.com/Yorhel/NCDU")),
            ("https://github.com/", None),
            ("https://github.com/yorhel", None),
            ("http://github.com/yorhel/ncdu", None),
            ("https://gitlab.com/yorhel/ncdu", None),
            ("https://api.github.com/repos/yorhel/ncdu", None),
            ("github.com/yorhel/ncdu", None),
            (None, None),
        ]
        for url, expected in cases:
            with self.subTest(url=url):
                self.assertEqual(inv.github_repo_of_url(url), expected)

    def test_uncovered_keeps_discovery_order(self):
        discovered = {"b/z": "github.com/B/Z", "a/y": "github.com/a/y",
                      "c/x": "github.com/c/x"}
        self.assertEqual(
            inv.uncovered_github_repos(discovered, {"a/y", "c/x"}),
            ["github.com/B/Z"])


class UncoveredRepoGate(unittest.TestCase):
    def test_search_discovery_rejects_with_per_repo_message(self):
        search = FakeSearchClient(search_result_with_github(
            "https://github.com/BratishkaErik/ncdu",
            "https://github.com/other/project"))
        with gate_ctx(search_client=search) as ctx:
            payload = ctx.tool_search_web({"query": "ncdu fork"})
            self.assertEqual(
                ctx.discovered_github_repos,
                {"bratishkaerik/ncdu": "github.com/BratishkaErik/ncdu",
                 "other/project": "github.com/other/project"})
            problems = inv.validate_report(
                report_citing_search(payload), ctx)
        # one validation error per uncovered repo, discovery order
        self.assertEqual(len(problems), 2)
        self.assertIn("uncovered GitHub repository", problems[0])
        self.assertIn("github.com/BratishkaErik/ncdu", problems[0])
        self.assertIn("uncovered GitHub repository", problems[1])
        self.assertIn("github.com/other/project", problems[1])

    def test_github_any_action_covers_case_insensitively(self):
        search = FakeSearchClient(search_result_with_github(
            "https://github.com/BratishkaErik/ncdu"))
        with gate_ctx(search_client=search) as ctx:
            payload = ctx.tool_search_web({"query": "ncdu fork"})
            problems = inv.validate_report(
                report_citing_search(payload), ctx)
            self.assertEqual(len(problems), 1)
            self.assertIn("uncovered GitHub repository", problems[0])
            with patch("gleam.entries.investigate.GithubClient",
                       return_value=FakeGithubClient(github_ok_result())):
                covered = ctx.tool_github(
                    {"action": "readme", "owner": "bratishkaerik",
                     "repo": "NCDU"})
            self.assertFalse(covered.get("error"))
            self.assertEqual(ctx.covered_github_repos,
                             {"bratishkaerik/ncdu"})
            self.assertEqual(inv.validate_report(
                report_citing_search(payload), ctx), [])

    def test_fetch_url_of_repo_page_covers(self):
        search = FakeSearchClient(search_result_with_github(
            "https://github.com/BratishkaErik/ncdu"))
        fetcher = FakeFetcher(b"repo page body",
                              url="https://github.com/BratishkaErik/ncdu")
        with gate_ctx(search_client=search, fetcher=fetcher) as ctx:
            payload = ctx.tool_search_web({"query": "ncdu fork"})
            ctx.tool_fetch_url({"url": "https://github.com/BratishkaErik/"
                                       "ncdu"})
            self.assertEqual(ctx.covered_github_repos,
                             {"bratishkaerik/ncdu"})
            self.assertEqual(inv.validate_report(
                report_citing_search(payload), ctx), [])

    def test_fetch_page_links_discover_repos(self):
        page = (b'<html><body><p>A fork of the project lives '
                b'elsewhere.</p><a href="https://github.com/foo/bar">'
                b"fork</a></body></html>")
        fetcher = FakeFetcher(page, url="https://example.org/x",
                              content_type="text/html")
        with gate_ctx(fetcher=fetcher) as ctx:
            payload = ctx.tool_fetch_url({"url": "https://example.org/x"})
            self.assertEqual(ctx.discovered_github_repos,
                             {"foo/bar": "github.com/foo/bar"})
            problems = inv.validate_report(report_citing_search(
                payload, url="https://example.org/x",
                quote="fork of the project"), ctx)
        self.assertEqual(len(problems), 1)
        self.assertIn("uncovered GitHub repository observed in tool "
                      "results: github.com/foo/bar", problems[0])

    def test_forks_payload_urls_are_not_discovery(self):
        search = FakeSearchClient(search_result_with_github(
            "https://github.com/BratishkaErik/ncdu"))
        forks_view = {"forks": [
            {"html_url": "https://github.com/other/fork1",
             "pushed_at": "2026-11-01T00:00:00Z"},
            {"html_url": "https://github.com/other/fork2",
             "pushed_at": "2026-10-01T00:00:00Z"},
        ]}
        forks_result = github_ok_result(
            view=forks_view,
            evidence_urls=["https://github.com/BratishkaErik/ncdu",
                           "https://github.com/other/fork1",
                           "https://github.com/other/fork2"])
        with gate_ctx(search_client=search) as ctx:
            payload = ctx.tool_search_web({"query": "ncdu fork"})
            with patch("gleam.entries.investigate.GithubClient",
                       return_value=FakeGithubClient(forks_result)):
                ctx.tool_github(
                    {"action": "forks", "owner": "BratishkaErik",
                     "repo": "ncdu"})
            # the forks list introduced NO new gate duties: only the
            # queried repo is covered, nothing else is discovered
            self.assertEqual(ctx.covered_github_repos,
                             {"bratishkaerik/ncdu"})
            self.assertEqual(ctx.discovered_github_repos,
                             {"bratishkaerik/ncdu":
                              "github.com/BratishkaErik/ncdu"})
            self.assertEqual(inv.validate_report(
                report_citing_search(payload), ctx), [])


class DismissedLinksGate(unittest.TestCase):
    """A discovered GitHub link the model judges wrong is
    handled by a stated reason instead of a forced fetch."""

    TOPIC = "https://github.com/topics/yoran-heling"

    def _report(self, payload, dismissed):
        report = report_citing_search(payload, url=self.TOPIC,
                                      quote=self.TOPIC)
        report["dismissed_links"] = dismissed
        return report

    def test_dismissal_with_reason_exempts_the_link(self):
        search = FakeSearchClient(search_result_with_github(self.TOPIC))
        with gate_ctx(search_client=search) as ctx:
            payload = ctx.tool_search_web({"query": "ncdu fork"})
            self.assertEqual(ctx.discovered_github_repos,
                             {"topics/yoran-heling":
                              "github.com/topics/yoran-heling"})
            self.assertEqual(inv.validate_report(self._report(
                payload, [{"url": self.TOPIC,
                           "reason": "GitHub topic page, not a "
                                     "repository"}]), ctx), [])

    def test_dismissal_without_reason_rejected(self):
        search = FakeSearchClient(search_result_with_github(self.TOPIC))
        with gate_ctx(search_client=search) as ctx:
            payload = ctx.tool_search_web({"query": "ncdu fork"})
            problems = inv.validate_report(self._report(
                payload, [{"url": self.TOPIC, "reason": " "}]), ctx)
        self.assertTrue(any("dismissed_links[0].reason must be a "
                            "non-empty string" in p for p in problems))
        self.assertTrue(any(p.startswith("uncovered GitHub repository")
                            for p in problems))

    def test_dismissal_of_unobserved_link_rejected(self):
        search = FakeSearchClient(search_result_with_github(self.TOPIC))
        with gate_ctx(search_client=search) as ctx:
            payload = ctx.tool_search_web({"query": "ncdu fork"})
            problems = inv.validate_report(self._report(
                payload, [{"url": "https://github.com/ghost/repo",
                           "reason": "not a real project"}]), ctx)
        self.assertTrue(any("was not observed in tool results of this "
                            "run" in p for p in problems))

    def test_dismissal_of_covered_link_requires_removal(self):
        search = FakeSearchClient(search_result_with_github(self.TOPIC))
        with gate_ctx(search_client=search) as ctx:
            payload = ctx.tool_search_web({"query": "ncdu fork"})
            ctx.covered_github_repos.add("topics/yoran-heling")
            problems = inv.validate_report(self._report(
                payload, [{"url": self.TOPIC,
                           "reason": "topic page"}]), ctx)
        self.assertEqual(len(problems), 1)
        self.assertIn("already covered by this run", problems[0])
        self.assertIn("remove this dismissal entry", problems[0])

    def test_report_state_and_draft_preview_expose_live_ledger(self):
        search = FakeSearchClient(search_result_with_github(self.TOPIC))
        with gate_ctx(search_client=search) as ctx:
            payload = ctx.tool_search_web({"query": "ncdu fork"})
            state = ctx.tool_inspect_report_state({})
            self.assertEqual(state["uncovered_repositories"],
                             ["github.com/topics/yoran-heling"])
            self.assertEqual(state["github_links"][0], {
                "repository": "topics/yoran-heling",
                "url": "github.com/topics/yoran-heling",
                "covered": False})
            draft = self._report(payload, [{
                "url": self.TOPIC, "reason": "topic page"}])
            preview = ctx.tool_validate_report_draft({"draft": draft})
        self.assertTrue(preview["accepted"])
        self.assertEqual(preview["problems"], [])
        self.assertIn("ledger", preview)

    def test_dismissal_url_must_be_repository_url(self):
        search = FakeSearchClient(search_result_with_github(self.TOPIC))
        with gate_ctx(search_client=search) as ctx:
            payload = ctx.tool_search_web({"query": "ncdu fork"})
            problems = inv.validate_report(self._report(
                payload, [{"url": "https://github.com/topics",
                           "reason": "index page"}]), ctx)
        self.assertTrue(any("is not a GitHub repository URL" in p
                            for p in problems))

    def test_dismissal_unknown_key_rejected(self):
        search = FakeSearchClient(search_result_with_github(self.TOPIC))
        with gate_ctx(search_client=search) as ctx:
            payload = ctx.tool_search_web({"query": "ncdu fork"})
            problems = inv.validate_report(self._report(
                payload, [{"url": self.TOPIC, "reason": "topic page",
                           "confidence": "high"}]), ctx)
        self.assertTrue(any("dismissed_links[0] has unknown keys"
                            in p for p in problems))

    def test_dismissal_not_an_array_rejected(self):
        search = FakeSearchClient(search_result_with_github(self.TOPIC))
        with gate_ctx(search_client=search) as ctx:
            payload = ctx.tool_search_web({"query": "ncdu fork"})
            problems = inv.validate_report(self._report(
                payload, self.TOPIC), ctx)
        self.assertTrue(any("dismissed_links must be an array" in p
                            for p in problems))


class QuoteVerbatimValidation(unittest.TestCase):
    PAGE = ("<html><body><p>Ncdu is a disk usage analyzer with a "
            "text-mode user interface. It is designed to find space hogs "
            "on a remote server where you don\u2019t have an entire "
            "graphical setup available, but it is a useful tool even on "
            "regular desktop systems.</p></body></html>").encode("utf-8")

    def _problems(self, quote):
        fetcher = FakeFetcher(self.PAGE, url="https://example.org/x",
                              content_type="text/html")
        with gate_ctx(fetcher=fetcher) as ctx:
            payload = ctx.tool_fetch_url({"url": "https://example.org/x"})
            self.assertTrue(payload.get("observation_id"))
            return inv.validate_report(report_citing_search(
                payload, url="https://example.org/x", quote=quote), ctx)

    def test_curly_apostrophe_in_page_accepted(self):
        self.assertEqual(self._problems(
            "find space hogs on a remote server where you don't have an "
            "entire graphical setup available"), [])

    def test_paraphrase_rejected(self):
        problems = self._problems(
            "find space hogs on a server where you do not have any "
            "graphical setup")
        self.assertTrue(any("verbatim quotation" in p for p in problems))

    def test_quote_cannot_omit_words_or_stitch_passages(self):
        source = ("the quick brown fox jumps over the lazy dog. "
                  "Ncdu is a disk usage analyzer.")
        page = (f"<html><body><p>{source}</p></body></html>"
                .encode("utf-8"))
        fetcher = FakeFetcher(page, url="https://example.org/x",
                              content_type="text/html")
        with gate_ctx(fetcher=fetcher) as ctx:
            payload = ctx.tool_fetch_url({"url": "https://example.org/x"})
            self.assertTrue(payload.get("observation_id"))

            def problems(quote):
                return inv.validate_report(report_citing_search(
                    payload, url="https://example.org/x",
                    quote=quote), ctx)

            self.assertTrue(any("verbatim quotation" in p
                                for p in problems(
                                    "the quick brown fox over the lazy "
                                    "dog")))
            self.assertTrue(any("verbatim quotation" in p
                                for p in problems(
                                    "quick brown fox lazy dog. Ncdu is "
                                    "a disk")))
            self.assertEqual(problems(
                "the quick brown fox jumps over the lazy dog"), [])

    def test_punctuation_only_suffix_uses_word_floor(self):
        self.assertEqual(self._problems("disk usage !!!"), [])

    def test_markdown_readme_quote_accepted(self):
        readme = ("## Project status\n\n> [!IMPORTANT]\n"
                  "> **This is a Maintenance Fork!**\n>\n"
                  "> Following the [passing of Yoran Heling (aka Yorhel)]"
                  "(https://vndb.org/t24787), the original author of Ncdu, "
                  "this repository serves as a maintenance fork to keep "
                  "his project alive.\n")
        view = {"readme_text": render_markdown_text(readme),
                "readme_mode": "decoded-from-base64+markdown-text"}
        with gate_ctx() as ctx:
            with patch("gleam.entries.investigate.GithubClient",
                       return_value=FakeGithubClient(
                           github_ok_result(view=view))):
                payload = ctx.tool_github(
                    {"action": "readme", "owner": "BratishkaErik",
                     "repo": "ncdu"})
            problems = inv.validate_report({
                "summary": "fork", "uncertainties": "none",
                "candidates": [{
                    "url": "https://github.com/BratishkaErik/ncdu",
                    "why": "maintenance fork",
                    "supporting_obs_ids": [payload["observation_id"]],
                    "quote": ("This is a Maintenance Fork! Following the "
                              "passing of Yoran Heling (aka Yorhel), the "
                              "original author of Ncdu, this repository "
                              "serves as a maintenance fork to keep his "
                              "project alive.")}]}, ctx)
            self.assertEqual(problems, [])

    def test_short_two_word_quote_uses_exact_containment(self):
        self.assertEqual(self._problems("disk usage"), [])


class UncoveredRepoGateRepairLoop(unittest.TestCase):
    def test_rejection_reaches_repair_loop_then_fixed_report_passes(self):
        """End to end: the gate rejection is fed back as a repair tool
        result; after a github call covers the repo the resubmitted
        report is accepted (and the rejected one never becomes
        final_report)."""

        def finish_citing_search(call_id):
            def build(messages):
                for message in reversed(messages):
                    if message.get("role") != "tool":
                        continue
                    payload = json.loads(message["content"])
                    if payload.get("engine") == inv.SEARCH_ENGINE:
                        return tool_call_turn(
                            call_id, "finish_investigation",
                            report_citing_search(payload))
                raise AssertionError("no search tool result in messages")
            return build

        with tempfile.TemporaryDirectory() as store:
            search = FakeSearchClient(search_result_with_github(
                "https://github.com/BratishkaErik/ncdu"))
            script = ScriptedModel([
                tool_call_turn("c-1", "search_web", {"query": "ncdu fork"}),
                finish_citing_search("c-2"),
                tool_call_turn("c-3", "github",
                               {"action": "repo",
                                "owner": "BratishkaErik", "repo": "ncdu"}),
                finish_citing_search("c-4"),
            ])
            with patch("gleam.entries.investigate.SearxngClient",
                       return_value=search), \
                    patch("gleam.entries.investigate.GithubClient",
                          return_value=FakeGithubClient(
                              github_ok_result())), \
                    model_patch(script):
                result = inv.run(**run_kwargs(store))
            self.assertEqual(result.status, "ok", result.data["detail"])
            self.assertEqual(result.kind, "completed")
            # the rejection reached the model as repair feedback
            feedback = None
            for message in script.seen_messages[2]:
                if message.get("role") == "tool":
                    payload = json.loads(message["content"])
                    if payload.get("accepted") is False:
                        feedback = payload
            self.assertIsNotNone(feedback)
            self.assertEqual(len(feedback["problems"]), 1)
            self.assertIn("uncovered GitHub repository",
                          feedback["problems"][0])
            self.assertIn("fix these problems", feedback["message"])
            self.assertEqual(feedback["ledger"]["uncovered_repositories"],
                             ["github.com/BratishkaErik/ncdu"])
            # the accepted report is the resubmitted one; the rejected
            # submission never leaked into run.json
            summary = read_run_json(only_run_dir(store))
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(
                summary["final_report"]["candidates"][0]["url"],
                "https://github.com/BratishkaErik/ncdu")

    def test_dismissal_after_rejection_is_accepted(self):
        """The run9 shape: a topic URL the model cannot engage is
        declared with a reason after the gate rejects the first
        submission; the resubmitted report is accepted and the reason
        lands in run.json."""

        topic = "https://github.com/topics/yoran-heling"

        def finish(call_id, dismissed):
            def build(messages):
                for message in reversed(messages):
                    if message.get("role") != "tool":
                        continue
                    payload = json.loads(message["content"])
                    if payload.get("engine") == inv.SEARCH_ENGINE:
                        report = report_citing_search(
                            payload, url=topic, quote=topic)
                        if dismissed:
                            report["dismissed_links"] = [{
                                "url": topic,
                                "reason": "GitHub topic page listing "
                                          "repositories, not a "
                                          "repository"}]
                        return tool_call_turn(call_id,
                                              "finish_investigation",
                                              report)
                raise AssertionError("no search tool result in messages")
            return build

        with tempfile.TemporaryDirectory() as store:
            search = FakeSearchClient(search_result_with_github(topic))
            script = ScriptedModel([
                tool_call_turn("d-1", "search_web", {"query": "ncdu fork"}),
                finish("d-2", False),
                finish("d-3", True),
            ])
            with patch("gleam.entries.investigate.SearxngClient",
                       return_value=search), model_patch(script):
                result = inv.run(**run_kwargs(store))
            self.assertEqual(result.status, "ok", result.data["detail"])
            summary = read_run_json(only_run_dir(store))
            self.assertEqual(
                summary["final_report"]["dismissed_links"],
                [{"url": topic,
                  "reason": "GitHub topic page listing repositories, "
                            "not a repository"}])

    def test_covered_dismissal_is_removed_after_feedback(self):
        topic = "https://github.com/topics/yoran-heling"

        def finish(call_id, dismissed):
            def build(messages):
                for message in reversed(messages):
                    if message.get("role") != "tool":
                        continue
                    payload = json.loads(message["content"])
                    if payload.get("engine") == inv.SEARCH_ENGINE:
                        report = report_citing_search(
                            payload, url=topic, quote=topic)
                        if dismissed:
                            report["dismissed_links"] = [{
                                "url": topic,
                                "reason": "topic page"}]
                        return tool_call_turn(call_id,
                                              "finish_investigation",
                                              report)
                raise AssertionError("no search tool result in messages")
            return build

        with tempfile.TemporaryDirectory() as store:
            search = FakeSearchClient(search_result_with_github(topic))
            script = ScriptedModel([
                tool_call_turn("e-1", "search_web", {"query": "ncdu"}),
                tool_call_turn("e-2", "github", {
                    "action": "repo", "owner": "topics",
                    "repo": "yoran-heling"}),
                finish("e-3", True),
                finish("e-4", False),
            ])
            with patch("gleam.entries.investigate.SearxngClient",
                       return_value=search), \
                    patch("gleam.entries.investigate.GithubClient",
                          return_value=FakeGithubClient(
                              github_ok_result())), \
                    model_patch(script):
                result = inv.run(**run_kwargs(store))
            self.assertEqual(result.status, "ok", result.data["detail"])
            feedback = json.loads(script.seen_messages[3][-1]["content"])
            self.assertIn("already covered by this run",
                          feedback["problems"][0])
            summary = read_run_json(only_run_dir(store))
            self.assertNotIn("dismissed_links", summary["final_report"])


# ---------------------------------------------------------------------------
# The repology managed tool
# ---------------------------------------------------------------------------

REPOLOGY_PROJECT_BODY = json.dumps([
    {"repo": "aur", "version": "1.21", "status": "newest",
     "visiblename": "ncdu"}]).encode("utf-8")
REPOLOGY_SEARCH_BODY = json.dumps({
    "ncdu": [{"repo": "aur", "version": "1.21", "status": "newest",
              "visiblename": "ncdu"}]}).encode("utf-8")


def repology_transport_patch():
    """Transport-boundary double: the REAL RepologyClient runs against
    per-URL FetchOutcomes shaped exactly like the exempt-path results
    the real Fetcher produces (robots: exempt), hermetic, no network."""

    def fake_fetch(url, headers=None):
        body = (REPOLOGY_SEARCH_BODY if "/projects/" in url
                else REPOLOGY_PROJECT_BODY)
        return FetchOutcome(
            ok=True, status=200, body=body, final_url=url,
            robots={"result": "exempt", "exempt": True,
                    "detail": "host is operator-exempt from robots.txt "
                              "(robots-exempt.txt); robots "
                              "pre-fetch skipped", "url": None,
                    "cached": False},
            hops=[HopResult(url=url, status=200, headers={})])
    return patch("gleam.core.sources.repology.transport",
                 **{"Fetcher.return_value.fetch.side_effect": fake_fetch,
                    "Fetcher.return_value.policy": {
                        "max_bytes": 262144, "timeout_s": 1.0,
                        "max_requests": 6, "min_interval_s": 0.0,
                        "robots_exempt": ["repology.org"],
                        "user_agent": "test"},
                    "Fetcher.return_value.request_count": 0})


class RepologyToolTests(unittest.TestCase):
    def test_disabled_tool_reports_capability_disabled(self):
        with gate_ctx() as ctx:
            ctx.repology_enabled = False
            payload = ctx.tool_repology({"action": "project",
                                         "slug": "ncdu"})
        self.assertEqual(payload["error"]["kind"], "capability-disabled")
        self.assertIn("--no-repology", payload["error"]["message"])

    def test_project_ok_observe_and_evidence(self):
        with repology_ctx() as ctx, repology_transport_patch():
            payload = ctx.tool_repology({"action": "project",
                                         "slug": "ncdu"})
        self.assertNotIn("error", payload)
        self.assertEqual(payload["source_kind"],
                         "repology-api-structured-data")
        self.assertEqual(payload["digest"]["slug"], "ncdu")
        self.assertEqual(payload["digest"]["total_entries"], 1)
        obs_id = payload["observation_id"]
        self.assertIn(obs_id, ctx.evidence)
        self.assertEqual(ctx.evidence[obs_id]["kind"], "repology-api")
        # one observation record written through managed storage
        self.assertEqual(len(ctx.records), 1)
        self.assertEqual(ctx.records[0]["endpoint"]["label"],
                         "agent-repology")

    def test_argument_validation_before_any_request(self):
        cases = [
            {"action": "bogus"},
            {"action": "project"},
            {"action": "project", "slug": ""},
            {"action": "project", "slug": "x" * 201},
            {"action": "search"},
            {"action": "search", "query": "x" * 201},
        ]
        with repology_ctx() as ctx, repology_transport_patch():
            for args in cases:
                with self.subTest(args=args):
                    payload = ctx.tool_repology(args)
                    self.assertEqual(payload["error"]["kind"],
                                     "argument-invalid")
            self.assertEqual(ctx.total_requests(), 0)

    def test_shared_budget_reserved_per_request(self):
        with repology_ctx(request_cap=2) as ctx, repology_transport_patch():
            first = ctx.tool_repology({"action": "project",
                                       "slug": "ncdu"})
            second = ctx.tool_repology({"action": "search",
                                        "query": "ncdu"})
            third = ctx.tool_repology({"action": "project",
                                       "slug": "ncdu"})
        self.assertNotIn("error", first)
        self.assertNotIn("error", second)
        self.assertEqual(third["error"]["kind"], "budget-exhausted")
        self.assertIn("shared request cap", third["error"]["message"])
        # two reservations against the shared pool, none on the main
        # fetcher (its request count stays 0)
        self.assertEqual(ctx.total_requests(), 2)

    def test_tool_intrinsic_exemption_unions_operator_set(self):
        from gleam.core.transport import Fetcher
        main = Fetcher(policy={"min_interval_s": 0.0},
                       exempt_hosts=frozenset({"operator.example"}))
        with repology_ctx(
                operator_exempt={"operator.example"},
                main_fetcher=main) as ctx:
            with repology_transport_patch() as mocked:
                payload = ctx.tool_repology({"action": "project",
                                             "slug": "ncdu"})
                kwargs = mocked.Fetcher.call_args.kwargs
                self.assertEqual(kwargs["exempt_hosts"], frozenset({
                    "operator.example", "repology.org"}))
        self.assertNotIn("error", payload)
        # the investigation's MAIN fetcher keeps exactly the
        # operator-file set: the intrinsic exemption never leaks into
        # manual fetch_url.
        self.assertEqual(main.exempt_hosts, frozenset({"operator.example"}))
        self.assertEqual(main.policy["robots_exempt"],
                         ["operator.example"])

    def test_every_repology_fetch_is_robots_exempt(self):
        with repology_ctx() as ctx, repology_transport_patch():
            payload = ctx.tool_repology({"action": "project",
                                         "slug": "ncdu"})
        record = ctx.records[0]
        self.assertEqual(record["robots"]["result"], "exempt")
        # exempt hosts have no robots body, so the provenance
        # entry carries nulls and can never mimic a stored document
        self.assertIsNone(record["robots"]["content_sha256"])
        self.assertIsNone(record["robots"]["path"])
        self.assertIn("repology.org",
                      record["policy"]["robots_exempt"])

    def _read_only_repology_document(self, run_dir):
        names = os.listdir(os.path.join(run_dir, "repology"))
        self.assertEqual(len(names), 1)
        self.assertRegex(names[0], r"^[0-9a-f]{64}\.json$")
        with open(os.path.join(run_dir, "repology", names[0]),
                  encoding="utf-8") as handle:
            return names[0], json.load(handle)

    def test_project_document_extra_and_source_link(self):
        with repology_ctx() as ctx, repology_transport_patch():
            payload = ctx.tool_repology({"action": "project",
                                         "slug": "ncdu"})
            name, document = self._read_only_repology_document(
                ctx.store.run_dir)
        self.assertNotIn("error", payload)
        self.assertEqual(document["source"], "repology")
        self.assertEqual(document["status"], 200)
        self.assertEqual(document["extra"], {
            "action": "project", "slug": "ncdu",
            "result_summary": {"entries": 1, "versions": ["1.21"]}})
        record = ctx.records[0]
        link = record["source_document"]
        self.assertEqual(link, {"source": "repology",
                                "sha256": name[:-len(".json")],
                                "path": f"repology/{name}"})
        # the record discloses the client fetcher's own effective
        # robots exemption set
        self.assertIn("repology.org",
                      record["policy"]["robots_exempt"])

    def test_search_document_extra_resolved_slugs(self):
        with repology_ctx() as ctx, repology_transport_patch():
            payload = ctx.tool_repology({"action": "search",
                                         "query": "ncdu"})
            _name, document = self._read_only_repology_document(
                ctx.store.run_dir)
        self.assertNotIn("error", payload)
        self.assertEqual(document["extra"], {
            "action": "search", "query": "ncdu",
            "result_summary": {"slugs": ["ncdu"], "more": 0}})

    def test_transport_error_yields_null_source_document(self):
        with repology_ctx() as ctx, repology_transport_patch() as mocked:
            mocked.Fetcher.return_value.fetch.side_effect = (
                lambda url, headers=None: FetchOutcome(
                    ok=False, status=None, final_url=url, body=b"",
                    error={"kind": "connection-error",
                           "message": "refused in test"}))
            payload = ctx.tool_repology({"action": "project",
                                         "slug": "ncdu"})
            self.assertEqual(
                os.listdir(os.path.join(ctx.store.run_dir, "repology")),
                [])
        self.assertEqual(payload["error"]["kind"],
                         "fetch-connection-error")
        record = ctx.records[0]
        self.assertIsNone(record["source_document"])


# ---------------------------------------------------------------------------
# Full layout end to end: one scripted run through every evidence
# source, then the complete store layout asserted on disk -- plus the
# dedup, source-write-failure, and degraded-run classification
# acceptance scenarios.
# ---------------------------------------------------------------------------

#: A search result whose URLs are NOT GitHub repositories: the
#: uncovered-repository gate stays out of this run's way (the github
#: call below still covers the repo it queries).
SIX_SOURCE_SEARCH = SearxngResult(
    status="ok", query="wizznic upstream",
    final_url="http://127.0.0.1:8888/search?q=wizznic+upstream",
    http_status=200,
    results=[{"title": "Wizznic upstream home",
              "url": "https://wizznic.example/upstream",
              "snippet": "upstream project home", "engines": ["bing"]}],
    raw_body=b'{"results": []}', rendered_text="rendered")

BTS_SIX_SOURCE_BODY = (b"<?xml version='1.0'?><soap:Envelope>bug 1000000 "
                       b"discussion</soap:Envelope>")

SOURCE_DOC_NAME = re.compile(r"^[0-9a-f]{64}\.json$")

#: The tool names the six-source script dispatches (the approved
#: surface: ``github``/``fetch_bug``).
SIX_SOURCE_TOOL_NAMES = {"fetch_url", "search_web", "github", "fetch_bug",
                         "fetch_udd", "repology", "finish_investigation"}


def finish_citing_udd(call_id="s-fin"):
    """Build a VALID finish turn citing the UDD evidence of this run
    (the golden accepted-report pattern: observation id + homepage URL
    from the sources row + a quote substring of the exposed payload)."""

    def build(messages):
        for message in reversed(messages):
            if message.get("role") != "tool":
                continue
            payload = json.loads(message["content"])
            if payload.get("source_kind") != "udd-mirror-readonly-rows":
                continue
            report = {
                "summary": "upstream presence found",
                "candidates": [{
                    "url": "https://wizznic.example/upstream",
                    "why": "homepage in the UDD sources row",
                    "supporting_obs_ids": [payload["observation_id"]],
                    "quote": QUOTE_FROM_UDD_PAYLOAD,
                    "uncertainty": "low",
                }],
                "uncertainties": "none for the test",
                "fact_evidence": ["udd:sources#row=1"],
            }
            return tool_call_turn(call_id, "finish_investigation", report)
        raise AssertionError("no udd tool result in messages")

    return build


def six_source_script():
    """One scripted conversation calling every evidence tool with
    minimal valid arguments and then a valid finish (zero network:
    every tool goes through the patched adapter doubles)."""

    return ScriptedModel([
        tool_call_turn("s-1", "fetch_url",
                       {"url": "http://example.org/x"}),
        tool_call_turn("s-2", "search_web",
                       {"query": "wizznic upstream"}),
        tool_call_turn("s-3", "github",
                       {"action": "repo", "owner": "BratishkaErik",
                        "repo": "ncdu"}),
        tool_call_turn("s-4", "fetch_bug", {"bug_id": 1000000}),
        tool_call_turn("s-5", "fetch_udd",
                       {"action": "fetch", "datasets": ["sources"]}),
        tool_call_turn("s-6", "repology",
                       {"action": "project", "slug": "ncdu"}),
        finish_citing_udd("s-7"),
    ])


@contextmanager
def six_source_run(script):
    """Every external double of the six-source run, wired and ready
    (entry-boundary adapters + the repology transport seam)."""

    with patch("gleam.entries.investigate.Fetcher",
               return_value=RobotsBodyFetcher()), \
            patch("gleam.entries.investigate.SearxngClient",
                  return_value=FakeSearchClient(SIX_SOURCE_SEARCH)), \
            patch("gleam.entries.investigate.GithubClient",
                  return_value=FakeGithubClient(github_ok_result())), \
            patch("gleam.entries.investigate.BtsClient") as bts_cls, \
            udd_patch() as udd_cls, \
            repology_transport_patch(), \
            model_patch(script):
        bts_cls.return_value.fetch.return_value = \
            BugFetchSingleArtifactTests._ok_result(BTS_SIX_SOURCE_BODY)
        bts_cls.return_value.request_count = 1
        udd_cls.return_value.query.return_value = make_udd_result()
        yield


def run_files(run_dir):
    """Every file of the run tree as a run-relative posix path."""

    found = []
    for base, _dirs, files in os.walk(run_dir):
        for name in sorted(files):
            found.append(os.path.relpath(os.path.join(base, name),
                                         run_dir).replace(os.sep, "/"))
    return sorted(found)


class StoreLayoutEndToEnd(unittest.TestCase):
    """The acceptance backbone: one deterministic scripted run
    through fetch_url, search_web, github, fetch_bug, fetch_udd,
    repology and finish_investigation, then the complete store
    layout asserted on the produced run directory."""

    def _run_six_source(self, store):
        script = six_source_script()
        with six_source_run(script):
            result = inv.run(**run_kwargs(store))
        return script, result

    def _observation_records(self, run_dir):
        records = {}
        for name in os.listdir(os.path.join(run_dir, "observations")):
            with open(os.path.join(run_dir, "observations", name),
                      encoding="utf-8") as fh:
                record = json.load(fh)
            records[record["final_url"]] = record
        return records

    def test_six_source_run_full_layout_contract(self):
        """The complete layout contract on one real run directory:
        nine dirs + run.json only, merged round documents
        with embedded results, content-addressed evidence files, robots
        provenance, UDD rows externalized, one BTS artifact, no stray
        residue anywhere, and the run classifies valid through the
        real check entry."""
        with tempfile.TemporaryDirectory() as store:
            script, result = self._run_six_source(store)
            self.assertEqual(result.status, "ok", result.data["detail"])
            self.assertEqual(result.kind, "completed")
            run_dir = only_run_dir(store)

            # (1) top level: exactly the nine evidence dirs + run.json
            self.assertEqual(sorted(os.listdir(run_dir)),
                             sorted(list(LAYOUT_DIRS) + ["run.json"]))

            # (2) run.json carries the completion status; rounds[]
            # summaries carry exactly the four keys (no tools_file, no
            # round_file: the path derives from the round number)
            summary = read_run_json(run_dir)
            self.assertEqual(summary["status"], "completed")
            self.assertNotIn("layout", summary)
            self.assertEqual(len(summary["rounds"]), 7)
            for entry in summary["rounds"]:
                self.assertEqual(sorted(entry),
                                 ["finish_reason", "round", "tool_calls"])

            # (3) round documents: request with messages + embedded
            # tool results; only the approved tool names are dispatched
            rounds_dir = os.path.join(run_dir, "rounds")
            self.assertEqual(sorted(os.listdir(rounds_dir)),
                             [f"{n:04d}.json" for n in range(1, 8)])
            dispatched = set()
            for name in os.listdir(rounds_dir):
                with open(os.path.join(rounds_dir, name),
                          encoding="utf-8") as fh:
                    doc = json.load(fh)
                self.assertEqual(doc["kind"], "investigate-round")
                self.assertTrue(doc["request"]["messages"])
                for tool_call in doc["tool_calls"]:
                    self.assertIsInstance(tool_call["result"], dict)
                    dispatched.add(tool_call["name"])
            self.assertEqual(dispatched, SIX_SOURCE_TOOL_NAMES)
            self.assertNotIn("github_api", dispatched)
            self.assertNotIn("bug_fetch", dispatched)

            # (4) content addressing at the file level: every
            # evidence file in the seven source dirs is <64-hex>.json
            # whose name IS the sha256 of its exact bytes
            files = run_files(run_dir)
            source_files = [path for path in files
                            if path.split("/")[0] in SOURCE_DIRS]
            for path in source_files:
                name = path.split("/")[-1]
                self.assertRegex(name, SOURCE_DOC_NAME.pattern, path)
                with open(os.path.join(run_dir, path), "rb") as fh:
                    data = fh.read()
                self.assertEqual(
                    hashlib.sha256(data).hexdigest(),
                    name[:-len(".json")], path)

            # (5) observations: <hex>.json files whose stored records
            # carry that same bare <hex> observation_id as their id,
            # matching run.json's id list
            observation_files = [path for path in files
                                 if path.startswith("observations/")]
            self.assertEqual(len(observation_files), 6)
            stored_ids = []
            for path in observation_files:
                stem = path.split("/")[-1][:-len(".json")]
                with open(os.path.join(run_dir, path),
                          encoding="utf-8") as fh:
                    record = json.load(fh)
                self.assertEqual(record["observation_id"], stem)
                stored_ids.append(stem)
            self.assertEqual(sorted(stored_ids),
                             sorted(summary["observation_ids"]))

            records = self._observation_records(run_dir)

            # every observation links its per-source document and the
            # directory matches the source-label table
            expected_sources = {
                "http://example.org/x": "fetch_url",
                "http://127.0.0.1:8888/search?q=wizznic+upstream":
                    "search_web",
                "https://api.github.com/repos/BratishkaErik/ncdu":
                    "github",
                BugFetchSingleArtifactTests.BUG_PAGE: "fetch_bug",
                "postgresql://udd-mirror.debian.net/udd": "fetch_udd",
                "https://repology.org/api/v1/project/ncdu": "repology",
            }
            self.assertEqual(sorted(records), sorted(expected_sources))
            for final_url, source in expected_sources.items():
                link = records[final_url]["source_document"]
                self.assertEqual(link["source"], source)
                self.assertEqual(link["path"],
                                 f"{source}/{link['sha256']}.json")

            # (6) robots provenance: the fetch observation's merged
            # robots entry carries the BODY hash and the envelope path;
            # the referenced document exists with exactly that body
            fetch_record = records["http://example.org/x"]
            robots_entry = fetch_record["robots"]
            self.assertEqual(robots_entry["origin"],
                             "http://example.org:80")
            self.assertEqual(robots_entry["content_sha256"],
                             hashlib.sha256(ROBOTS_BODY_ALLOW).hexdigest())
            self.assertTrue(robots_entry["path"])
            robots_path = os.path.join(run_dir, robots_entry["path"])
            self.assertTrue(os.path.isfile(robots_path))
            with open(robots_path, encoding="utf-8") as fh:
                robots_doc = json.load(fh)
            self.assertEqual(robots_doc["source"], "robots")
            self.assertEqual(robots_doc["content_sha256"],
                             robots_entry["content_sha256"])
            self.assertEqual(robots_doc["body"],
                             ROBOTS_BODY_ALLOW.decode("utf-8"))
            self.assertEqual(
                [path for path in files if path.startswith("robots/")],
                [robots_entry["path"]])

            # (7) UDD rows live ONLY inside the fetch_udd document;
            # the observation counts with row_count (singular) and
            # carries no rows key anywhere (recursive walk)
            self.assertEqual(
                [path for path in files
                 if path.startswith("fetch_udd/")], [
                    records["postgresql://udd-mirror.debian.net/udd"]
                    ["source_document"]["path"]])
            with open(os.path.join(
                    run_dir,
                    records["postgresql://udd-mirror.debian.net/udd"]
                    ["source_document"]["path"]),
                    encoding="utf-8") as fh:
                udd_doc = json.load(fh)
            self.assertEqual(
                len(udd_doc["extra"]["datasets"]["sources"]["rows"]), 1)
            udd_record = records["postgresql://udd-mirror.debian.net/udd"]
            self.assertEqual(udd_record["extra"]["row_counts"],
                             {"sources": {"row_count": 1,
                                          "truncated": False}})
            for record in records.values():
                _assert_no_rows_key(record)
            for path in source_files + observation_files:
                if path.startswith("fetch_udd/"):
                    continue
                with open(os.path.join(run_dir, path),
                          encoding="utf-8") as fh:
                    _assert_no_rows_key(json.load(fh), path)

            # (8) one BTS artifact; the model payload's raw_ref equals
            # the observation's source_document.path
            self.assertEqual(
                [path for path in files if path.startswith("fetch_bug/")],
                [records[BugFetchSingleArtifactTests.BUG_PAGE]
                 ["source_document"]["path"]])
            with open(os.path.join(rounds_dir, "0004.json"),
                      encoding="utf-8") as fh:
                bts_call = json.load(fh)["tool_calls"][0]
            self.assertEqual(bts_call["name"], "fetch_bug")
            self.assertEqual(
                bts_call["result"]["raw_ref"],
                records[BugFetchSingleArtifactTests.BUG_PAGE]
                ["source_document"]["path"])

            # (9) no stray residue anywhere in the tree
            for path in files:
                name = path.split("/")[-1]
                self.assertFalse(name.endswith(".body"), path)
                self.assertFalse(name.endswith(".headers"), path)
                self.assertNotIn(".bts-raw", path)
                self.assertFalse(name.startswith("robots-"), path)
            self.assertFalse(
                any(path.startswith("raw/") or path.startswith("models/")
                    for path in files))

            # (10) github/ and repology/ documents parse and carry the
            # source of their directory
            for directory in ("github", "repology"):
                for path in [p for p in files
                             if p.startswith(f"{directory}/")]:
                    with open(os.path.join(run_dir, path),
                              encoding="utf-8") as fh:
                        doc = json.load(fh)
                    self.assertEqual(doc["source"], directory, path)
            repology_doc_path = records[
                "https://repology.org/api/v1/project/ncdu"][
                "source_document"]["path"]
            with open(os.path.join(run_dir, repology_doc_path),
                      encoding="utf-8") as fh:
                repology_doc = json.load(fh)
            self.assertEqual(repology_doc["extra"], {
                "action": "project", "slug": "ncdu",
                "result_summary": {"entries": 1,
                                   "versions": ["1.21"]}})

            # (11) the run classifies valid through the REAL check
            # entry over this temp store
            out = io.StringIO()
            with redirect_stdout(out):
                code = check_entry.main(["--store", store, "--json"])
            self.assertEqual(code, 0)
            report = json.loads(out.getvalue())
            self.assertEqual(report["kind"], "store-valid")
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["data"]["run_count"], 1)

    def test_two_identical_fetches_one_envelope_one_observation(self):
        """Dedup through real tools: two identical fetch_url
        calls in ONE run write ONE content-addressed envelope (the
        second write dedups to the same path) and -- with the clock
        frozen -- the two identical observation records dedup to one
        obs id stored once."""
        with tempfile.TemporaryDirectory() as store:
            fetcher = FakeFetcher(b"stable body text " * 20)
            script = ScriptedModel([
                tool_call_turn("c-1", "fetch_url",
                               {"url": "http://example.org/x"}),
                tool_call_turn("c-2", "fetch_url",
                               {"url": "http://example.org/x"}),
                prose_result("done looking"),
            ])
            with patch("gleam.entries.investigate.Fetcher",
                       return_value=fetcher), \
                    patch("gleam.entries.investigate.utc_now",
                          return_value="2026-01-01T00:00:00Z"), \
                    model_patch(script):
                result = inv.run(**run_kwargs(store))
            self.assertEqual(result.kind, "no-final-report")
            run_dir = only_run_dir(store)
            # ONE envelope: the identical document maps to one path
            docs = os.listdir(os.path.join(run_dir, "fetch_url"))
            self.assertEqual(len(docs), 1)
            self.assertRegex(docs[0], SOURCE_DOC_NAME.pattern)
            # the file name still equals the hash of its exact bytes
            with open(os.path.join(run_dir, "fetch_url", docs[0]),
                      "rb") as fh:
                data = fh.read()
            self.assertEqual(hashlib.sha256(data).hexdigest(),
                             docs[0][:-len(".json")])
            # ONE observation: identical records -> identical obs id
            tool_messages = [json.loads(m["content"])
                             for m in script.seen_messages[-1]
                             if m.get("role") == "tool"]
            ids = [payload["observation_id"] for payload in tool_messages
                   if "observation_id" in payload]
            self.assertEqual(len(ids), 2)
            self.assertEqual(ids[0], ids[1])
            self.assertEqual(
                os.listdir(os.path.join(run_dir, "observations")),
                [ids[0] + ".json"])
            # no robots body existed: the robots dir stays empty
            self.assertEqual(
                os.listdir(os.path.join(run_dir, "robots")), [])

    def test_source_document_write_failure_storage_error_round_flushed(self):
        """An os.replace failure DURING a source-
        document write (the fetch_url envelope surface) still closes
        as storage-error, preserves the half-written tree (the leaked
        temp file in fetch_url/ is partial-write evidence), and flushes
        the pending round document before the close-out seals the
        run."""
        real_replace = os.replace

        def fail_fetch_url_replaces(tmp, dst):
            if f"{os.sep}fetch_url{os.sep}" in dst:
                raise OSError("injected disk full")
            return real_replace(tmp, dst)

        with tempfile.TemporaryDirectory() as store:
            fetcher = FakeFetcher(b"page body that never lands")
            script = ScriptedModel([
                tool_call_turn("c-1", "fetch_url",
                               {"url": "http://example.org/x"}),
                prose_result("never reached"),
            ])
            with patch("gleam.entries.investigate.Fetcher",
                       return_value=fetcher), \
                    patch("gleam.core.storage.runstore.os.replace",
                          side_effect=fail_fetch_url_replaces), \
                    model_patch(script):
                result = inv.run(**run_kwargs(store))
            self.assertEqual(result.status, "storage-error")
            self.assertEqual(result.kind, "storage-error")
            self.assertEqual(result.errors[0].code, "storage-error")
            run_dir = only_run_dir(store)
            # the half-written tree is preserved: nine dirs + the
            # best-effort storage-error close-out
            self.assertEqual(sorted(os.listdir(run_dir)),
                             sorted(list(LAYOUT_DIRS) + ["run.json"]))
            summary = read_run_json(run_dir)
            self.assertEqual(summary["status"], "storage-error")
            self.assertIn("source document write failed",
                          summary["storage_error"]["operation"])
            # the pending round was flushed BEFORE the close-out:
            # request messages survive, zero tool calls completed
            with open(os.path.join(run_dir, "rounds", "0001.json"),
                      encoding="utf-8") as fh:
                flushed = json.load(fh)
            self.assertEqual(flushed["kind"], "investigate-round")
            self.assertTrue(flushed["request"]["messages"])
            self.assertEqual(flushed["tool_calls"], [])
            # the failed atomic write leaked its temp file inside the
            # source dir and no envelope ever landed there
            fetch_url_entries = os.listdir(
                os.path.join(run_dir, "fetch_url"))
            self.assertTrue(any(name.startswith(".tmp")
                                for name in fetch_url_entries))
            self.assertFalse(any(name.endswith(".json")
                                 for name in fetch_url_entries))
            self.assertEqual(
                os.listdir(os.path.join(run_dir, "observations")), [])
            # the real check entry reports the preserved half-run as
            # the storage-error it records
            out = io.StringIO()
            with redirect_stdout(out):
                code = check_entry.main(["--store", store, "--json"])
            self.assertEqual(code, 1)
            report = json.loads(out.getvalue())
            self.assertEqual(report["kind"], "store-error")
            codes = {item["code"]
                     for item in report["data"]["runs"][0]["diagnostics"]}
            self.assertIn("storage-error-recorded", codes)
            self.assertIn("tmp-residue", codes)

    def test_robots_abstain_and_udd_failure_tree_still_valid(self):
        """Degraded-run scenario: a run whose fetch is robots-
        abstained AND whose UDD call fails still writes both
        observations with null source documents, keeps the directory
        tree structurally exact, and the real check entry reports the
        run valid."""
        failed_udd = UddResult(
            status="failed", package="wizznic", release=None,
            datasets=(), rows=[], provenance=[], row_count=0,
            errors=[DiagnosticItem(
                code="udd-query-error", where="gleam.core.sources.udd",
                message="driver failed")],
            dsn_display="postgresql://udd-mirror.debian.net/udd",
            fetched_at="2026-01-01T00:00:00Z",
        )
        with tempfile.TemporaryDirectory() as store:
            fetcher = RobotsAbstainFetcher()
            script = ScriptedModel([
                tool_call_turn("c-1", "fetch_url",
                               {"url": "http://example.org/x"}),
                tool_call_turn("c-2", "fetch_udd",
                               {"action": "fetch",
                                "datasets": ["sources"]}),
                prose_result("both sources unusable"),
            ])
            with patch("gleam.entries.investigate.Fetcher",
                       return_value=fetcher), udd_patch() as udd_cls, \
                    model_patch(script):
                udd_cls.return_value.query.return_value = failed_udd
                result = inv.run(**run_kwargs(store))
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.kind, "no-final-report")
            run_dir = only_run_dir(store)
            # structurally exact tree: nine dirs + run.json, no temp
            # residue anywhere
            self.assertEqual(sorted(os.listdir(run_dir)),
                             sorted(list(LAYOUT_DIRS) + ["run.json"]))
            for path in run_files(run_dir):
                self.assertFalse(
                    path.split("/")[-1].startswith(".tmp"), path)
            # both observations exist with null source documents; the
            # degraded evidence dirs hold nothing
            records = self._observation_records(run_dir)
            self.assertEqual(sorted(records), [
                "http://example.org/x",
                "postgresql://udd-mirror.debian.net/udd"])
            for record in records.values():
                self.assertIsNone(record["source_document"])
            self.assertEqual(records["http://example.org/x"]["outcome"],
                             "ABSTAIN")
            self.assertIsNone(
                records["http://example.org/x"]["robots"]
                ["content_sha256"])
            self.assertEqual(
                os.listdir(os.path.join(run_dir, "fetch_url")), [])
            self.assertEqual(
                os.listdir(os.path.join(run_dir, "fetch_udd")), [])
            # the real check entry reports the run valid
            out = io.StringIO()
            with redirect_stdout(out):
                code = check_entry.main(["--store", store, "--json"])
            self.assertEqual(code, 0)
            report = json.loads(out.getvalue())
            self.assertEqual(report["kind"], "store-valid")
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["data"]["run_count"], 1)
            self.assertEqual(report["data"]["runs"][0]["status"],
                             "no-final-report")


if __name__ == "__main__":
    unittest.main()
