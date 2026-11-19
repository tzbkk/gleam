"""investigate --verbose narration (plan verbose-flag §7.5 investigate
smoke + §7.9 tool-client narration guard).

Zero network: the model is the ScriptedModel double (extended to also
keep every audit record it emitted, so round-trip expectations derive
from the same records the narration reads), source adapters are the
test_entry_investigate doubles, and the §7.9 github test drives the
REAL GithubClient over the test_verbose_fetcher transport stubs.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from gleam.core.sources.github import GithubClient, GithubPolicy
from gleam.core.sources.bts import BtsPolicy
from gleam.core.sources.repology import RepologyPolicy
from gleam.core.sources.searxng import SearxngResult
from gleam.core.storage import RunStore
from gleam.core.transport import FetchOutcome, HopResult
from gleam.core.verbose import format_count, make_verbose
from gleam.entries import investigate as inv
from tests.test_entry_investigate import (
    BTS_SIX_SOURCE_BODY,
    SIX_SOURCE_SEARCH,
    BugFetchSingleArtifactTests,
    FakeFetcher,
    FakeGithubClient,
    FakeSearchClient,
    RobotsBodyFetcher,
    ScriptedModel,
    finish_citing_udd,
    github_ok_result,
    make_udd_result,
    model_patch,
    only_run_dir,
    prose_result,
    repology_transport_patch,
    tool_call_turn,
    udd_patch,
)
from tests.test_verbose import FIXED_TIME
from tests.test_verbose_fetcher import (
    PUBLIC_IP,
    FakeResponse,
    patch_env,
    route_map,
)

PREFIX = "[investigate 12:34:56] "


def entry_argv(store, verbose=True):
    argv = ["--package", "wizznic", "--store", store,
            "--model", "q", "--model-base", "http://127.0.0.1:1"]
    if verbose:
        argv.append("--verbose")
    return argv


class RecordingModel(ScriptedModel):
    """ScriptedModel that also keeps every audit record it emitted;
    round-trip line expectations derive from these records, never
    from pinned literals."""

    def __init__(self, turns):
        super().__init__(turns)
        self.records = []

    def chat(self, messages, tools=None):
        inner = self.audit

        def audit(record):
            self.records.append(record)
            if inner is not None:
                inner(record)

        self.audit = audit
        try:
            return super().chat(messages, tools)
        finally:
            self.audit = inner


def expected_model_line(record, tool_calls):
    chars_in = len(json.dumps(record["request"], ensure_ascii=False,
                              sort_keys=True))
    finish = record["finish_reason"] or "none"
    return (f"model {format_count(chars_in)} chars in, "
            f"{format_count(len(record['response_body_raw']))} out, "
            f"finish={finish}, "
            f"{format_count(tool_calls)} tool call"
            f"{'' if tool_calls == 1 else 's'}")


class RoutingFetcher:
    """FakeFetcher variant with one FetchOutcome per URL (error
    outcomes included)."""

    def __init__(self, routes):
        self.policy = {"max_bytes": 262144, "timeout_s": 15.0,
                       "max_redirects": 3, "max_requests": 12,
                       "min_interval_s": 1.0, "user_agent": "test"}
        self.request_count = 0
        self._robots_bodies = {}
        self._routes = routes

    def fetch(self, url, headers=None):
        self.request_count += 1
        return self._routes[url]


def six_source_verbose_script():
    return RecordingModel([
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


class VerbosePinnedClock(unittest.TestCase):
    def setUp(self):
        clock = patch("gleam.core.verbose.time.gmtime",
                      return_value=FIXED_TIME)
        clock.start()
        self.addCleanup(clock.stop)
        self.err = io.StringIO()


class InvestigateVerboseSmokeTests(VerbosePinnedClock):
    """§7.5 investigate: run dir + round-trip lines + tool lines
    (incl. a failure) + finish_investigation; quiet default zero
    stderr for a full fake run."""

    def test_six_source_transcript(self):
        script = six_source_verbose_script()
        with tempfile.TemporaryDirectory() as store:
            with patch("gleam.entries.investigate.Fetcher",
                       return_value=RobotsBodyFetcher()), \
                    patch("gleam.entries.investigate.SearxngClient",
                          return_value=FakeSearchClient(
                              SIX_SOURCE_SEARCH)), \
                    patch("gleam.entries.investigate.GithubClient",
                          return_value=FakeGithubClient(
                              github_ok_result())), \
                    patch("gleam.entries.investigate.BtsClient") \
                    as bts_cls, \
                    udd_patch() as udd_cls, \
                    repology_transport_patch(), \
                    model_patch(script):
                bts_cls.return_value.fetch.return_value = \
                    BugFetchSingleArtifactTests._ok_result(
                        BTS_SIX_SOURCE_BODY)
                bts_cls.return_value.request_count = 1
                udd_cls.return_value.query.return_value = \
                    make_udd_result()
                with redirect_stderr(self.err):
                    result = inv.run(entry_argv(store))
            self.assertEqual(result.status, "ok")
            run_id = os.path.basename(only_run_dir(store))
            # round 1 crosses the 1000 boundary: the k-form is real
            first = script.records[0]
            chars_in = len(json.dumps(first["request"],
                                      ensure_ascii=False,
                                      sort_keys=True))
            self.assertGreaterEqual(chars_in, 1000)
            expected = [
                f"run dir {store}/{run_id}",
                f"round 1: {expected_model_line(script.records[0], 1)}",
                "round 1: fetch_url http://example.org/x "
                "-> 200, 0.0 KB",
                f"round 2: {expected_model_line(script.records[1], 1)}",
                "round 2: search_web 'wizznic upstream' -> 1 result",
                f"round 3: {expected_model_line(script.records[2], 1)}",
                "round 3: github repo=BratishkaErik/ncdu -> 200",
                f"round 4: {expected_model_line(script.records[3], 1)}",
                "round 4: fetch_bug bug=1000000 -> 1 message",
                f"round 5: {expected_model_line(script.records[4], 1)}",
                "round 5: fetch_udd package=wizznic datasets=sources "
                "-> 1 row",
                f"round 6: {expected_model_line(script.records[5], 1)}",
                "round 6: repology project=ncdu -> 200",
                f"round 7: {expected_model_line(script.records[6], 1)}",
                "round 7: finish_investigation",
            ]
            self.assertEqual(
                [PREFIX + line for line in expected],
                self.err.getvalue().splitlines())
            self.assertRegex(expected[1], r" \d+\.\dk chars in, ")

    def test_failure_line_and_stop_round(self):
        ok_body = b"Q" * 4100
        fetcher = RoutingFetcher({
            "https://ok.example/x": FetchOutcome(
                ok=True, status=200, final_url="https://ok.example/x",
                body=ok_body, hops=[HopResult(
                    url="https://ok.example/x", status=200,
                    headers={"content-type": ["text/plain"]},
                    body=ok_body)]),
            "https://blocked.example/page": FetchOutcome(
                ok=False, final_url="https://blocked.example/page",
                error={"kind": "robots-unknown",
                       "message": "robots verdict unknown"}),
        })
        search = SearxngResult(
            status="ok", query="ncdu upstream maintenance",
            final_url="http://127.0.0.1:8888/search?q=ncdu",
            http_status=200,
            results=[{"title": f"r {i}", "url": f"https://r{i}.example/x",
                      "snippet": "s", "engines": ["bing"]}
                     for i in range(26)],
            raw_body=b"{}", rendered_text="rendered")
        script = RecordingModel([
            tool_call_turn("f-1", "fetch_url",
                           {"url": "https://ok.example/x"}),
            tool_call_turn("f-2", "search_web",
                           {"query": "ncdu upstream maintenance"}),
            tool_call_turn("f-3", "fetch_url",
                           {"url": "https://blocked.example/page"}),
            prose_result("stopping without a report"),
        ])
        with tempfile.TemporaryDirectory() as store:
            with patch("gleam.entries.investigate.Fetcher",
                       return_value=fetcher), \
                    patch("gleam.entries.investigate.SearxngClient",
                          return_value=FakeSearchClient(search)), \
                    model_patch(script):
                with redirect_stderr(self.err):
                    result = inv.run(entry_argv(store))
            self.assertEqual(result.kind, "no-final-report")
            run_id = os.path.basename(only_run_dir(store))
            expected = [
                f"run dir {store}/{run_id}",
                f"round 1: {expected_model_line(script.records[0], 1)}",
                "round 1: fetch_url https://ok.example/x "
                "-> 200, 4.1 KB",
                f"round 2: {expected_model_line(script.records[1], 1)}",
                "round 2: search_web 'ncdu upstream maintenance' "
                "-> 26 results",
                f"round 3: {expected_model_line(script.records[2], 1)}",
                "round 3: fetch_url https://blocked.example/page "
                "-> error robots-unknown",
                f"round 4: {expected_model_line(script.records[3], 0)}",
            ]
            self.assertEqual(
                [PREFIX + line for line in expected],
                self.err.getvalue().splitlines())
            self.assertIn("finish=stop, 0 tool calls", expected[7])

    def test_quiet_default_zero_stderr(self):
        script = RecordingModel([
            tool_call_turn("q-1", "fetch_udd",
                           {"action": "fetch", "datasets": ["sources"]}),
            finish_citing_udd("q-2"),
        ])
        with tempfile.TemporaryDirectory() as store:
            with udd_patch() as udd_cls, model_patch(script):
                udd_cls.return_value.query.return_value = \
                    make_udd_result()
                with redirect_stderr(self.err):
                    result = inv.run(entry_argv(store, verbose=False))
            self.assertEqual(result.status, "ok")
            self.assertEqual(self.err.getvalue(), "")


def inv_prose_turn(text):
    from gleam.core.model import AssistantTurn, ChatResult
    turn = AssistantTurn(content=text, finish_reason="stop",
                         tool_calls=(), safety_failure=False)
    return ChatResult(model="q",
                      endpoint="http://127.0.0.1:1/chat/completions",
                      status=200, body="{}", turn=turn, error=None,
                      duration_s=0.0)


def verbose_ctx(sink, **overrides):
    """One real InvestigationContext on a temp store with the verbose
    sink attached (gate_ctx pattern; github enabled by default)."""

    manager = tempfile.TemporaryDirectory()
    run_store = RunStore.create(manager.name, "wizznic")
    fields = {
        "store": run_store, "run_id": run_store.run_id,
        "package": "wizznic",
        "limits": {"max_rounds": 4, "max_tool_calls": 8,
                   "max_body_chars": 40000, "max_links": 120,
                   "max_context_chars": 320000,
                   "max_report_repairs": 2},
        "display": {"mailto_max": 5, "copyright_max": 5, "footer_max": 5,
                    "max_url_chars": 2048,
                    "max_search_query_chars": 400, "min_quote_chars": 8,
                    "field_char_cap": 200, "report_cell_max_chars": 2000,
                    "report_max_errors": 20},
        "udd_dsn": "postgresql://udd-mirror.debian.net/udd",
        "udd_policy": None,
        "udd_dataset_scope": (), "default_release": None,
        "udd_limit": None, "udd_max_calls": 2, "udd_max_rows": 50,
        "udd_enabled": False,
        "fetcher": FakeFetcher(b"page body"),
        "request_cap": 10, "search_enabled": False,
        "search_client": None, "github_base": "https://api.github.com",
        "github_policy": GithubPolicy(),
        "bts_enabled": False, "bts_policy": BtsPolicy(),
        "repology_enabled": False, "repology_base": None,
        "repology_policy": RepologyPolicy(),
        "robots_exempt_hosts": frozenset(),
        "verbose": sink,
    }
    fields.update(overrides)
    return manager, inv.InvestigationContext(**fields)


class GithubClientVerboseThreadingTests(unittest.TestCase):
    """The round-1 BLOCKER fix: GithubClient's OWN internal Fetcher
    (:180) receives the caller's sink -- investigate builds a fresh
    client per github tool call, so the sink must ride the
    constructor every time."""

    def test_verbose_reaches_internal_fetcher(self):
        sink = make_verbose("investigate", True)
        with patch("gleam.core.sources.github.client.transport."
                   "Fetcher") as fetcher_cls:
            GithubClient(api_base="https://api.github.com",
                         verbose=sink)
        self.assertIs(fetcher_cls.call_args.kwargs["verbose"], sink)

    def test_default_stays_quiet(self):
        with patch("gleam.core.sources.github.client.transport."
                   "Fetcher") as fetcher_cls:
            GithubClient(api_base="https://api.github.com")
        self.assertIsNone(fetcher_cls.call_args.kwargs["verbose"])


class GithubToolNarrationTests(VerbosePinnedClock):
    """§7.9: with the sink threaded, a github tool call's internals
    narrate under the [investigate …] prefix (guards the finding-1/2
    regression class)."""

    def test_github_get_budget_line_runs_under_investigate(self):
        body = json.dumps({"full_name": "ncdu/ncdu",
                           "html_url": "https://github.com/ncdu/ncdu"}
                          ).encode()
        routes = {"/robots.txt": FakeResponse(
            200, body=b"User-agent: *\nDisallow:\n")}
        patch_env(self, {"api.github.com": PUBLIC_IP},
                  route_map(routes,
                            default=FakeResponse(200, body=body)))
        sink = io.StringIO()
        manager, ctx = verbose_ctx(make_verbose("investigate", True,
                                                stream=sink))
        with manager:
            payload = ctx.tool_github({"action": "repo",
                                       "owner": "ncdu", "repo": "ncdu"})
        self.assertEqual(payload["status"], 200)
        # per-call instance budget = remaining shared budget (10);
        # the silent robots pre-fetch consumed one of them
        lines = sink.getvalue().splitlines()
        self.assertIn(
            f"{PREFIX}robots https://api.github.com/repos/ncdu/ncdu: "
            f"allow", lines)
        self.assertIn(
            f"{PREFIX}GET https://api.github.com/repos/ncdu/ncdu "
            f"(budget 9/10)", lines)
        self.assertIn(
            f"{PREFIX}dns api.github.com -> {PUBLIC_IP} (pinned)",
            lines)
        self.assertIn(
            f"{PREFIX}200 OK, {len(body)} bytes", lines)
        # §4 emission order: robots -> GET -> dns -> response
        order = [lines.index(f"{PREFIX}robots "
                             "https://api.github.com/repos/ncdu/ncdu:"
                             " allow"),
                 lines.index(f"{PREFIX}GET "
                             "https://api.github.com/repos/ncdu/ncdu "
                             "(budget 9/10)"),
                 lines.index(f"{PREFIX}dns api.github.com -> "
                             f"{PUBLIC_IP} (pinned)"),
                 lines.index(f"{PREFIX}200 OK, {len(body)} bytes")]
        self.assertEqual(order, sorted(order))


class ClientSinkThreadingTests(unittest.TestCase):
    """Finding 2: every network-touching component the context
    constructs receives the ONE sink (identical callable), and no
    client builds its own."""

    def test_all_lazy_clients_receive_the_context_sink(self):
        sink = make_verbose("investigate", True, stream=io.StringIO())
        manager, ctx = verbose_ctx(sink)
        with manager, \
                patch("gleam.entries.investigate.UddClient") as u, \
                patch("gleam.entries.investigate.BtsClient") as b, \
                patch("gleam.entries.investigate.RepologyClient") as r, \
                patch("gleam.entries.investigate.GithubClient",
                      return_value=FakeGithubClient(
                          github_ok_result())) as g:
            ctx._udd()
            ctx._bts()
            ctx._repology()
            ctx.tool_github({"action": "repo", "owner": "ncdu",
                             "repo": "ncdu"})
        for mock in (u, b, r, g):
            self.assertIs(mock.call_args.kwargs["verbose"], sink)


if __name__ == "__main__":
    unittest.main()
