"""investigate 模式测试：合成多轮工具循环、协议、校验与预算。

全部使用脚本化模型响应（patch gleam.models._http_json）与 StubFetcher；
不触碰真实网络 / raw/ / 本地模型端点。
"""

import copy
import json
import os
import tempfile
import unittest
from unittest import mock

from gleam import udd as udd_mod
from gleam.agent import TOOL_NAMES
from gleam.cli import EXIT_COMPLETED_WITH_ERRORS, EXIT_OK, EXIT_USAGE, \
    build_parser, cmd_investigate
from gleam.net import FetchOutcome
from gleam.search import SearchResponse, render_results_text

from .fixtures import QA_MAINTAINER, FakeUddRunner, patch_udd_client

PKGONE_HOME = "https://example.org/pkgone/"
PKGONE_BODY = (b"<html><head><style>body{}</style></head><body>"
               b"<h1>pkgone home page</h1>"
               b"<p>development moved to our git server</p>"
               b"<a href='https://git.example.org/pkgone.git'>source</a>"
               b"</body></html>")
QUOTE = "development moved to our git server"


def tool_call_message(call_id, name, arguments):
    return {"id": call_id, "type": "function",
            "function": {"name": name,
                         "arguments": arguments
                         if isinstance(arguments, str)
                         else json.dumps(arguments)}}


def completion(tool_calls=None, content="", finish_reason="tool_calls",
               reasoning="internal chain"):
    message = {"content": content, "reasoning_content": reasoning}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return json.dumps({"choices": [{"message": message,
                                    "finish_reason": finish_reason}],
                       "usage": {"total_tokens": 10}})


def finish_call(call_id, report):
    return tool_call_message(call_id, "finish_investigation", report)


VALID_REPORT = {
    "summary": "found upstream git server via homepage link",
    "candidates": [{
        "url": "https://git.example.org/pkgone.git",
        "why": "linked from the package homepage as source",
        "supporting_obs_ids": ["obs-0001"],
        "quote": QUOTE,
        "uncertainty": "identity not confirmed; needs human review",
    }],
    "uncertainties": "single page evidence only",
}

# 仅当脚本里真的先调用了 fetch_udd(datasets=["sources"])（fake 回放一行
# sources 行、provenance udd:sources#row=1 进入 fact_citations）时才有效
VALID_REPORT_WITH_FACTS = dict(
    VALID_REPORT, fact_evidence=["udd:sources#row=1"])

EMPTY_REPORT = {
    "summary": "no fetch evidence available in this run",
    "candidates": [],
    "uncertainties": "nothing was fetched; unknown",
}


class StubFetcher:
    def __init__(self, body=PKGONE_BODY, fail_urls=None, error=None):
        self.policy = {"max_bytes": 1024, "timeout_s": 5.0,
                       "max_redirects": 3, "max_requests": 40,
                       "min_interval_s": 0, "user_agent": "test",
                       "robots_max_bytes": 1024}
        self._robots_bodies = {}
        self.request_count = 0
        self.body = body
        self.fail_urls = fail_urls or []
        self.error = error
        self.fetched = []

    def fetch(self, url, headers=None):
        self.fetched.append(url)
        self.headers_seen = headers
        self.request_count += 1
        if self.error is not None:
            return FetchOutcome(ok=False, error=self.error)
        return FetchOutcome(ok=True, status=200, final_url=url,
                            redirect_chain=[], body=self.body)


class StubSearch:
    """SearXNG 客户端替身：注入预设 SearchResponse，绝不触碰网络。"""

    def __init__(self, response=None, error=None):
        self.base_url = "http://127.0.0.1:8888"
        self.response = response
        self.error = error
        self.queries = []

    def search(self, query):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.response


def search_ok(results_spec, query="pkgone upstream"):
    results = [{"title": title, "url": url, "snippet": snippet,
                "engines": engines}
               for title, url, snippet, engines in results_spec]
    rendered = render_results_text(query, results, [])
    raw = json.dumps({"results": results, "unresponsive_engines": []}).encode()
    return SearchResponse(ok=True, status=200,
                          final_url="http://127.0.0.1:8888/search?q=x"
                                    "&format=json",
                          raw_body=raw, query=query, results=results,
                          unresponsive_engines=[], rendered_text=rendered)


class ScriptedModel:
    """按脚本逐轮返回模型响应；记录每轮请求载荷供断言。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, base, path, method, payload, timeout):
        self.requests.append(copy.deepcopy(payload))
        if not self.responses:
            raise AssertionError("model invoked more times than scripted")
        item = self.responses.pop(0)
        status, body = item if isinstance(item, tuple) else (200, item)
        return status, body


class InvestigateCliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = os.path.join(cls.tmp.name, "store")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_cmd(self, argv, responses, fetcher=None, search_client=None,
                runner=None, package="pkgone"):
        args = build_parser().parse_args(
            ["investigate",
             "--package", package, "--store", self.store] + argv)
        model = ScriptedModel(responses)
        runs_root = os.path.join(self.store, "runs")
        before = set(os.listdir(runs_root)) if os.path.isdir(runs_root) else set()
        with patch_udd_client(runner or FakeUddRunner()):
            with mock.patch("gleam.cli.Fetcher",
                            return_value=fetcher or StubFetcher()):
                with mock.patch("gleam.search.SearxngSearch",
                                return_value=search_client or StubSearch()):
                    with mock.patch("gleam.models._http_json",
                                    side_effect=model):
                        code = cmd_investigate(args)
        new_dirs = set(os.listdir(runs_root)) - before \
            if os.path.isdir(runs_root) else set()
        self.assertLessEqual(len(new_dirs), 1)
        run_dir = os.path.join(runs_root, sorted(new_dirs)[0]) \
            if new_dirs else None
        return code, run_dir, model

    def read_summary(self, run_dir):
        with open(os.path.join(run_dir, "run.json"), encoding="utf-8") as fh:
            return json.load(fh)

    # -- 正常多轮循环 ---------------------------------------------------

    def test_full_multi_turn_loop_with_tool_call_id_history(self):
        report = json.loads(json.dumps(VALID_REPORT_WITH_FACTS))
        report["candidates"][0]["supporting_obs_ids"] = ["obs-0002"]
        responses = [
            completion([tool_call_message("c1", "fetch_udd", {
                "action": "fetch", "datasets": ["sources"]})]),
            completion([tool_call_message("c2", "fetch_url",
                                          {"url": PKGONE_HOME})]),
            completion([finish_call("c3", report)]),
        ]
        code, run_dir, model = self.run_cmd([], responses)
        self.assertEqual(code, EXIT_OK)
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["final_report"]["candidates"][0]["url"],
                         "https://git.example.org/pkgone.git")

        # 每轮请求都携带 tools；绝不携带 response_format
        for request in model.requests:
            self.assertIn("tools", request)
            names = {t["function"]["name"] for t in request["tools"]}
            self.assertIn("finish_investigation", names)
            self.assertNotIn("response_format", request)

        # 第 3 轮历史：assistant 回放（无 reasoning_content）+ 匹配
        # tool_call_id 的 tool 结果
        third = model.requests[2]["messages"]
        roles = [m["role"] for m in third]
        self.assertEqual(roles, ["system", "user", "assistant", "tool",
                                 "assistant", "tool"])
        assistant_echoes = [m for m in third if m["role"] == "assistant"]
        self.assertEqual(assistant_echoes[0]["tool_calls"][0]["id"], "c1")
        self.assertEqual(assistant_echoes[0]["tool_calls"][0]["function"]
                         ["name"], "fetch_udd")
        self.assertNotIn("reasoning_content", assistant_echoes[0])
        tool_msgs = [m for m in third if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tool_msgs],
                         ["c1", "c2"])
        udd_result = json.loads(tool_msgs[0]["content"])
        self.assertEqual(udd_result["action"], "fetch")
        self.assertEqual(udd_result["package"], "pkgone")
        self.assertEqual(udd_result["datasets_data"]["sources"]
                         ["provenance"], ["udd:sources#row=1"])
        fetch_result = json.loads(tool_msgs[1]["content"])
        self.assertEqual(fetch_result["observation_id"], "obs-0002")
        self.assertIn("https://git.example.org/pkgone.git",
                      fetch_result["links"])

        # 每轮的原始请求/响应都按唯一轮次 ID 落盘
        for i in range(3):
            path = os.path.join(run_dir, "models", f"inv-r{i + 1:03d}.json")
            self.assertTrue(os.path.isfile(path), path)

    def test_empty_candidates_report_is_valid_completion(self):
        report = {"summary": "nothing found", "candidates": [],
                  "uncertainties": "homepage reachable but no links seen"}
        responses = [completion([finish_call("c1", report)])]
        code, run_dir, _ = self.run_cmd([], responses)
        self.assertEqual(code, EXIT_OK)
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["final_report"]["candidates"], [])

    # -- 畸形与未知工具调用 ----------------------------------------------

    def test_unknown_tool_returns_error_result_and_never_executes(self):
        responses = [
            completion([tool_call_message("c1", "delete_everything",
                                          {"path": "/"})]),
            completion([finish_call("c2", EMPTY_REPORT)]),
        ]
        code, run_dir, model = self.run_cmd([], responses)
        self.assertEqual(code, EXIT_OK)
        tool_result = json.loads(
            [m for m in model.requests[1]["messages"]
             if m["role"] == "tool"][0]["content"])
        self.assertEqual(tool_result["error"]["kind"], "unknown-tool")
        obs_dir = os.path.join(run_dir, "observations")
        self.assertEqual(os.listdir(obs_dir), [])

    def test_malformed_json_arguments_rejected_not_executed(self):
        responses = [
            completion([tool_call_message(
                "c1", "fetch_url", '{"url": "https://example.org/pkg')]),
            completion([finish_call("c2", EMPTY_REPORT)]),
        ]
        code, run_dir, model = self.run_cmd([], responses)
        self.assertEqual(code, EXIT_OK)
        tool_result = json.loads(
            [m for m in model.requests[1]["messages"]
             if m["role"] == "tool"][0]["content"])
        self.assertEqual(tool_result["error"]["kind"], "malformed-arguments")
        self.assertEqual(os.listdir(os.path.join(run_dir, "observations")),
                         [])

    def test_nonfinite_json_arguments_rejected(self):
        responses = [
            completion([tool_call_message(
                "c1", "search_web", '{"query": NaN}')]),
            completion([finish_call("c2", EMPTY_REPORT)]),
        ]
        code, run_dir, model = self.run_cmd([], responses)
        self.assertEqual(code, EXIT_OK)
        tool_result = json.loads(
            [m for m in model.requests[1]["messages"]
             if m["role"] == "tool"][0]["content"])
        self.assertEqual(tool_result["error"]["kind"], "malformed-arguments")
        self.assertIn("non-finite", tool_result["error"]["message"])

    def test_truncated_finish_reason_executes_nothing(self):
        responses = [
            completion([tool_call_message("c1", "fetch_url",
                                          {"url": PKGONE_HOME})],
                       finish_reason="length"),
        ]
        code, run_dir, _ = self.run_cmd(["--max-rounds", "1"], responses)
        self.assertEqual(code, EXIT_COMPLETED_WITH_ERRORS)
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "model-error")
        self.assertIn("truncated", summary["status_detail"])
        self.assertEqual(os.listdir(os.path.join(run_dir, "observations")),
                         [], "truncated tool calls must not be executed")
        self.assertEqual(summary["final_report"], None)

    def test_prose_fallback_preserved_but_not_success(self):
        responses = [
            completion(content="I believe the upstream is git.example.org "
                              "from my memory.", finish_reason="stop"),
        ]
        code, run_dir, _ = self.run_cmd([], responses)
        self.assertEqual(code, EXIT_COMPLETED_WITH_ERRORS)
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "no-final-report")
        self.assertIn("git.example.org", summary["fallback_final_prose"])
        self.assertIsNone(summary["final_report"])

    # -- 预算 -----------------------------------------------------------

    def test_rounds_budget_exhaustion_preserves_trajectory(self):
        responses = [
            completion([tool_call_message("c1", "fetch_udd", {
                "action": "fetch", "datasets": ["sources"]})]),
            completion([tool_call_message("c2", "fetch_udd", {
                "action": "fetch", "datasets": ["sources"]})]),
        ]
        code, run_dir, _ = self.run_cmd(["--max-rounds", "2"], responses)
        self.assertEqual(code, EXIT_COMPLETED_WITH_ERRORS)
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "budget-exhausted")
        self.assertEqual(len(summary["rounds"]), 2)
        for i in (1, 2):
            self.assertTrue(os.path.isfile(
                os.path.join(run_dir, "models", f"inv-r{i:03d}.json")))

    def test_tool_call_budget_returns_error_result_allows_finish(self):
        keep_calling = [
            completion([tool_call_message(f"c{i}", "fetch_udd", {
                "action": "fetch", "datasets": ["sources"]})])
            for i in range(1, 5)
        ]
        responses = keep_calling[:3] + [
            completion([finish_call("c9", EMPTY_REPORT)])]
        code, run_dir, model = self.run_cmd(
            ["--max-tool-calls", "2", "--max-rounds", "4"], responses)
        self.assertEqual(code, EXIT_OK)
        fourth_round_tools = [m for m in model.requests[3]["messages"]
                              if m["role"] == "tool"]
        last = json.loads(fourth_round_tools[-1]["content"])
        self.assertEqual(last["error"]["kind"], "budget-exhausted")

    # -- 终报校验（伪造引用 / 记忆 URL / 未知观测） ----------------------

    def test_forged_obs_id_rejected_then_repaired(self):
        bad = json.loads(json.dumps(VALID_REPORT))
        bad["candidates"][0]["supporting_obs_ids"] = ["obs-4242"]
        responses = [
            completion([tool_call_message("c1", "fetch_url",
                                          {"url": PKGONE_HOME})]),
            completion([finish_call("c2", bad)]),
            completion([finish_call("c3", VALID_REPORT)]),
        ]
        code, run_dir, model = self.run_cmd([], responses)
        self.assertEqual(code, EXIT_OK)
        rejection = json.loads(
            [m for m in model.requests[2]["messages"]
             if m["role"] == "tool"][-1]["content"])
        self.assertFalse(rejection["accepted"])
        self.assertTrue(any("obs-4242" in p
                            for p in rejection["problems"]))
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "completed")

    def test_forged_quote_rejected(self):
        bad = json.loads(json.dumps(VALID_REPORT))
        bad["candidates"][0]["quote"] = "this text never appeared anywhere"
        responses = [
            completion([tool_call_message("c1", "fetch_url",
                                          {"url": PKGONE_HOME})]),
            completion([finish_call("c2", bad)]),
        ]
        code, run_dir, _ = self.run_cmd(["--max-report-repairs", "0"],
                                        responses)
        self.assertEqual(code, EXIT_COMPLETED_WITH_ERRORS)
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "invalid-report")
        self.assertTrue(any("quote" in p for p in
                            summary["final_report_validation"]))

    def test_report_repair_grants_exactly_max_attempts(self):
        bad = json.loads(json.dumps(VALID_REPORT))
        bad["candidates"][0]["quote"] = "this text never appeared anywhere"
        responses = [
            completion([tool_call_message("c1", "fetch_url",
                                          {"url": PKGONE_HOME})]),
            completion([finish_call("c2", bad)]),
            completion([finish_call("c3", bad)]),
            completion([finish_call("c4", bad)]),
        ]
        # max-report-repairs=2：两次被拒后各给修复机会；第三次拒绝即终止
        code, run_dir, model = self.run_cmd(["--max-report-repairs", "2"],
                                            responses)
        self.assertEqual(code, EXIT_COMPLETED_WITH_ERRORS)
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "invalid-report")
        self.assertEqual(len(model.requests), 4,
                         "exactly 3 finish attempts (1 + 2 repairs)")

    def test_memory_only_url_rejected(self):
        bad = json.loads(json.dumps(VALID_REPORT))
        bad["candidates"][0]["url"] = "https://from-memory.example/x"
        responses = [
            completion([tool_call_message("c1", "fetch_url",
                                          {"url": PKGONE_HOME})]),
            completion([finish_call("c2", bad)]),
        ]
        code, run_dir, _ = self.run_cmd(["--max-report-repairs", "0"],
                                        responses)
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "invalid-report")
        self.assertTrue(any("not among the URLs observed" in p for p in
                            summary["final_report_validation"]))

    def test_unknown_fact_citation_rejected(self):
        bad = json.loads(json.dumps(VALID_REPORT))
        bad["fact_evidence"] = ["udd:sources#row=999"]
        responses = [
            completion([tool_call_message("c1", "fetch_url",
                                          {"url": PKGONE_HOME})]),
            completion([finish_call("c2", bad)]),
        ]
        code, run_dir, _ = self.run_cmd(["--max-report-repairs", "0"],
                                        responses)
        summary = self.read_summary(run_dir)
        self.assertEqual(code, EXIT_COMPLETED_WITH_ERRORS)
        self.assertEqual(summary["status"], "invalid-report")
        self.assertTrue(any("udd:sources#row=999" in p for p in
                            summary["final_report_validation"]))

    def test_legacy_snapshot_citation_rejected(self):
        # 快照时代的 table#row=N 引用不再是有效输入（快照非运行时输入）
        bad = json.loads(json.dumps(VALID_REPORT))
        bad["fact_evidence"] = ["sources.csv#row=1"]
        responses = [
            completion([tool_call_message("c1", "fetch_url",
                                          {"url": PKGONE_HOME})]),
            completion([finish_call("c2", bad)]),
        ]
        code, run_dir, _ = self.run_cmd(["--max-report-repairs", "0"],
                                        responses)
        summary = self.read_summary(run_dir)
        self.assertEqual(code, EXIT_COMPLETED_WITH_ERRORS)
        self.assertEqual(summary["status"], "invalid-report")
        self.assertTrue(any("sources.csv#row=1" in p for p in
                            summary["final_report_validation"]))

    def test_legacy_snapshot_evidence_key_rejected(self):
        # 旧的 snapshot_evidence 键本身也不再是合法字段
        bad = json.loads(json.dumps(VALID_REPORT))
        bad["snapshot_evidence"] = ["udd:sources#row=1"]
        responses = [
            completion([tool_call_message("c1", "fetch_url",
                                          {"url": PKGONE_HOME})]),
            completion([finish_call("c2", bad)]),
        ]
        code, run_dir, _ = self.run_cmd(["--max-report-repairs", "0"],
                                        responses)
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "invalid-report")
        self.assertTrue(any("unknown keys" in p for p in
                            summary["final_report_validation"]))

    # -- 多 tool_calls / 上下文 / 证据 ----------------------------------

    def test_parallel_tool_calls_get_matching_results_serially(self):
        report = json.loads(json.dumps(VALID_REPORT))
        report["candidates"][0]["supporting_obs_ids"] = ["obs-0002"]
        responses = [
            completion([
                tool_call_message("c1", "fetch_udd", {
                    "action": "fetch", "datasets": ["sources"]}),
                tool_call_message("c2", "fetch_url", {"url": PKGONE_HOME}),
            ]),
            completion([finish_call("c3", report)]),
        ]
        code, _, model = self.run_cmd([], responses)
        self.assertEqual(code, EXIT_OK)
        tool_msgs = [m for m in model.requests[1]["messages"]
                     if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tool_msgs],
                         ["c1", "c2"])

    def test_context_truncation_disclosed(self):
        big = ("<html><body>"
               + f"<p>{QUOTE}</p>"
               + ("lorem ipsum dolor sit amet " * 900)
               + "<a href='https://git.example.org/pkgone.git'>source</a>"
               + "</body></html>").encode()
        responses = [
            completion([tool_call_message("c1", "fetch_url",
                                          {"url": PKGONE_HOME})]),
            completion([tool_call_message("c2", "fetch_url",
                                          {"url": PKGONE_HOME + "2"})]),
            completion([finish_call("c3", VALID_REPORT)]),
        ]
        code, run_dir, model = self.run_cmd(
            ["--max-context-chars", "6000"], responses,
            fetcher=StubFetcher(body=big))
        self.assertEqual(code, EXIT_OK)
        summary = self.read_summary(run_dir)
        self.assertGreater(len(summary["context_truncations"]), 0)
        shortened = [m for m in model.requests[2]["messages"]
                     if m["role"] == "tool"
                     and "context-budget truncation" in m["content"]]
        self.assertTrue(shortened, "truncation must be disclosed in-band")

    def test_read_evidence_returns_stored_body(self):
        responses = [
            completion([tool_call_message("c1", "fetch_url",
                                          {"url": PKGONE_HOME})]),
            completion([tool_call_message("c2", "read_evidence",
                                          {"observation_id": "obs-0001"})]),
            completion([finish_call("c3", VALID_REPORT)]),
        ]
        code, _, model = self.run_cmd([], responses)
        self.assertEqual(code, EXIT_OK)
        result = json.loads(
            [m for m in model.requests[2]["messages"]
             if m["role"] == "tool"][-1]["content"])
        self.assertIn(QUOTE, result["body_text"])

    def test_fetch_network_error_is_evidence_not_loop_failure(self):
        responses = [
            completion([tool_call_message("c1", "fetch_url",
                                          {"url": PKGONE_HOME})]),
            completion([finish_call("c2", {
                "summary": "homepage unreachable; nothing verifiable",
                "candidates": [],
                "uncertainties": "transport error means unknown",
            })]),
        ]
        code, run_dir, _ = self.run_cmd(
            [], responses,
            fetcher=StubFetcher(
                error={"kind": "timeout", "message": "read deadline"}))
        self.assertEqual(code, EXIT_OK)
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["counts"]["by_outcome"].get(
            "TRANSPORT_ERROR"), 1)
        self.assertTrue(summary["errors"])

    def test_search_web_disabled_reports_honestly(self):
        responses = [
            completion([tool_call_message("c1", "search_web",
                                          {"query": "pkgone upstream"})]),
            completion([finish_call("c2", {
                "summary": "search unavailable; no candidates",
                "candidates": [],
                "uncertainties": "search disabled by operator",
            })]),
        ]
        code, _, model = self.run_cmd(["--no-search-web"], responses)
        self.assertEqual(code, EXIT_OK)
        result = json.loads(
            [m for m in model.requests[1]["messages"]
             if m["role"] == "tool"][0]["content"])
        self.assertEqual(result["error"]["kind"], "capability-disabled")

    def test_search_web_unreachable_reports_honest_failure(self):
        # 原 DDG robots 拒绝用例的 SearXNG 时代等价覆盖：受信任本地搜索
        # 不可达时显式失败（绝不伪造成成功/空结果）
        unreachable = SearchResponse(
            ok=False, query="pkgone upstream",
            final_url="http://127.0.0.1:8888/search?q=pkgone+upstream"
                      "&format=json",
            error={"kind": "connection-error",
                   "message": "ConnectionRefusedError: [Errno 111]"})
        responses = [
            completion([tool_call_message("c1", "search_web",
                                          {"query": "pkgone upstream"})]),
            completion([finish_call("c2", {
                "summary": "search unreachable; no candidates",
                "candidates": [],
                "uncertainties": "local search down; unknown",
            })]),
        ]
        stub = StubSearch(response=unreachable)
        code, run_dir, model = self.run_cmd([], responses, search_client=stub)
        self.assertEqual(code, EXIT_OK)
        result = json.loads(
            [m for m in model.requests[1]["messages"]
             if m["role"] == "tool"][0]["content"])
        self.assertEqual(result["error"]["kind"], "connection-error")
        self.assertIn("means unknown", result["note"])
        # 失败搜索仍落观测（原文/错误保留），source kind 明示第三方搜索
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["counts"]["observations"], 1)
        with open(os.path.join(run_dir, "observations", "obs-0001.json"),
                  encoding="utf-8") as fh:
            rec = json.load(fh)
        self.assertEqual(rec["outcome"], "TRANSPORT_ERROR")
        self.assertEqual(rec["endpoint"]["label"], "agent-search")

    def test_search_web_all_engines_failed_is_ambiguous_not_empty(self):
        ambiguous = SearchResponse(
            ok=True, status=200,
            final_url="http://127.0.0.1:8888/search?q=x&format=json",
            raw_body=b'{"results": [], "unresponsive_engines": '
                     b'[["duckduckgo", "error"], ["bing", "timeout"]]}',
            query="pkgone upstream", results=[],
            unresponsive_engines=[["duckduckgo", "error"],
                                  ["bing", "timeout"]],
            rendered_text="search results for query: pkgone upstream\n"
                          "unresponsive engines: [...]")
        ambiguous.error = {"kind": "all-engines-failed-or-empty",
                           "message": "no results and some engines failed"}
        responses = [
            completion([tool_call_message("c1", "search_web",
                                          {"query": "pkgone upstream"})]),
            completion([finish_call("c2", {
                "summary": "search ambiguous; no candidates",
                "candidates": [],
                "uncertainties": "engines failed; cannot claim no results",
            })]),
        ]
        code, run_dir, model = self.run_cmd(
            [], responses, search_client=StubSearch(response=ambiguous))
        self.assertEqual(code, EXIT_OK)
        result = json.loads(
            [m for m in model.requests[1]["messages"]
             if m["role"] == "tool"][0]["content"])
        self.assertEqual(result["error"]["kind"],
                         "all-engines-failed-or-empty")
        self.assertIn("do not fabricate", result["note"])
        self.assertEqual(result["unresponsive_engines"],
                         [["duckduckgo", "error"], ["bing", "timeout"]])

    def test_search_web_5xx_is_transport_error_never_baseline(self):
        server_error = SearchResponse(
            ok=False, status=500,
            final_url="http://127.0.0.1:8888/search?q=pkgone+upstream"
                      "&format=json",
            raw_body=b"<html>gateway error page</html>",
            query="pkgone upstream",
            error={"kind": "http-5xx",
                   "message": "HTTP 500 from search endpoint"})
        responses = [
            completion([tool_call_message("c1", "search_web",
                                          {"query": "pkgone upstream"})]),
            completion([finish_call("c2", {
                "summary": "search endpoint errored; no candidates",
                "candidates": [],
                "uncertainties": "5xx from local search; unknown",
            })]),
        ]
        code, run_dir, model = self.run_cmd(
            [], responses, search_client=StubSearch(response=server_error))
        self.assertEqual(code, EXIT_OK)
        result = json.loads(
            [m for m in model.requests[1]["messages"]
             if m["role"] == "tool"][0]["content"])
        self.assertEqual(result["error"]["kind"], "http-5xx")
        self.assertIn("means unknown", result["note"])
        self.assertNotIn("results", result)
        # 观测必须 TRANSPORT_ERROR（绝不 OK）、原始响应与错误原样保留
        with open(os.path.join(run_dir, "observations", "obs-0001.json"),
                  encoding="utf-8") as fh:
            rec = json.load(fh)
        self.assertEqual(rec["outcome"], "TRANSPORT_ERROR")
        self.assertEqual(rec["status_code"], 500)
        self.assertEqual(rec["error"]["kind"], "http-5xx")
        self.assertIn("body", rec["raw_refs"])
        self.assertIn("headers", rec["raw_refs"])
        # 5xx 观测绝不进入基线索引：index 不含该端点 URL
        with open(os.path.join(self.store, "index.json"),
                  encoding="utf-8") as fh:
            index = json.load(fh)
        self.assertNotIn(
            "http://127.0.0.1:8888/search?q=pkgone+upstream&format=json",
            index.get("endpoints") or {})

    def test_search_captcha_with_results_is_partial_not_failure(self):
        kept_results = [{"title": "t", "url": "https://ok.io/",
                         "snippet": "s", "engines": ["duckduckgo"]}]
        captcha = SearchResponse(
            ok=True, status=200,
            final_url="http://127.0.0.1:8888/search?q=x&format=json",
            raw_body=json.dumps({"results": kept_results,
                                 "unresponsive_engines":
                                     [["brave", "captcha"]]}).encode(),
            query="pkgone upstream", results=kept_results,
            unresponsive_engines=[["brave", "captcha"]],
            rendered_text=render_results_text(
                "pkgone upstream", kept_results, [["brave", "captcha"]]))
        captcha.error = {"kind": "captcha-reported",
                         "message": "some engines reported captcha; "
                                    "listed in unresponsive_engines"}
        responses = [
            completion([tool_call_message("c1", "search_web",
                                          {"query": "pkgone upstream"})]),
            completion([finish_call("c2", EMPTY_REPORT)]),
        ]
        code, run_dir, model = self.run_cmd(
            [], responses, search_client=StubSearch(response=captcha))
        self.assertEqual(code, EXIT_OK)
        result = json.loads(
            [m for m in model.requests[1]["messages"]
             if m["role"] == "tool"][0]["content"])
        self.assertEqual(result["error"]["kind"], "captcha-reported")
        self.assertEqual(len(result["results"]), 1,
                         "captcha with usable results must keep results")
        self.assertEqual(result["results"][0]["url"], "https://ok.io/")
        self.assertIn("partial", result["note"])
        self.assertNotIn("do not fabricate", result["note"])
        with open(os.path.join(run_dir, "observations", "obs-0001.json"),
                  encoding="utf-8") as fh:
            rec = json.load(fh)
        self.assertEqual(rec["outcome"], "OK")

    def test_search_results_are_citable_weaker_evidence(self):
        response = search_ok([
            ("librcd homepage", "https://rusxmms.sourceforge.io/librcd/",
             "RusXMMS project librcd library page with downloads",
             ["duckduckgo", "wikipedia"]),
            ("librcd git", "https://github.com/RusXMMS/librcd",
             "mirror repository on GitHub", ["bing"]),
        ])
        report = {
            "summary": "search found upstream presence candidates",
            "candidates": [{
                "url": "https://github.com/RusXMMS/librcd",
                "why": "returned by two independent engines as the project "
                       "repository",
                "supporting_obs_ids": ["obs-0001"],
                "quote": "mirror repository on GitHub",
                "uncertainty": "search snippet only; weaker evidence; not "
                               "identity confirmation",
            }],
            "uncertainties": "snippets are third-party summaries",
        }
        responses = [
            completion([tool_call_message("c1", "search_web",
                                          {"query": "pkgone upstream"})]),
            completion([finish_call("c2", report)]),
        ]
        code, run_dir, model = self.run_cmd(
            [], responses, search_client=StubSearch(response=response))
        self.assertEqual(code, EXIT_OK)
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "completed")
        result = json.loads(
            [m for m in model.requests[1]["messages"]
             if m["role"] == "tool"][0]["content"])
        self.assertEqual(result["engine"], "searxng")
        self.assertEqual(result["source_kind"], "third-party-search-results")
        self.assertEqual(result["results"][0]["engines"],
                         ["duckduckgo", "wikipedia"])
        self.assertIn("weaker evidence", result["note"])
        # 原始提供商 JSON 已作为观测留档（可被 check 校验）
        with open(os.path.join(run_dir, "observations", "obs-0001.json"),
                  encoding="utf-8") as fh:
            rec = json.load(fh)
        self.assertEqual(rec["outcome"], "OK")
        self.assertEqual(rec["endpoint"]["label"], "agent-search")

    def test_search_counts_against_shared_request_budget(self):
        response = search_ok([("t", "https://x.example/a", "s", ["bing"])])
        responses = [
            completion([tool_call_message("c1", "fetch_url",
                                          {"url": PKGONE_HOME})]),
            completion([tool_call_message("c2", "search_web",
                                          {"query": "pkgone"})]),
            completion([finish_call("c3", EMPTY_REPORT)]),
        ]
        fetcher = StubFetcher()
        fetcher.policy["max_requests"] = 1  # fetch 已用掉唯一预算
        code, run_dir, model = self.run_cmd(
            ["--max-requests", "1"], responses, fetcher=fetcher,
            search_client=StubSearch(response=response))
        self.assertEqual(code, EXIT_OK)
        search_result = json.loads(
            [m for m in model.requests[2]["messages"]
             if m["role"] == "tool"][-1]["content"])
        self.assertEqual(search_result["error"]["kind"], "budget-exhausted")
        summary = self.read_summary(run_dir)
        # 失败显式保留；无搜索观测
        self.assertEqual(summary["counts"]["observations"], 1)
        self.assertEqual(summary["counts"]["http_requests"], 1)

    def test_fetch_udd_rejects_unknown_dataset(self):
        responses = [
            completion([tool_call_message("c1", "fetch_udd", {
                "action": "fetch", "datasets": ["no-such-dataset"]})]),
            completion([finish_call("c2", {
                "summary": "facts unavailable", "candidates": [],
                "uncertainties": "none"})]),
        ]
        runner = FakeUddRunner()
        code, _, model = self.run_cmd([], responses, runner=runner)
        self.assertEqual(code, EXIT_OK)
        result = json.loads(
            [m for m in model.requests[1]["messages"]
             if m["role"] == "tool"][0]["content"])
        self.assertEqual(result["error"]["kind"], "argument-invalid")
        self.assertEqual(runner.calls, [],
                         "非法数据集名不得触达 psql")

    # -- fetch_udd：list / 参数校验 / 预算 / 列式输出 / 24k 裁剪 / 零预取 --

    def test_fetch_udd_list_returns_catalog_without_db(self):
        runner = FakeUddRunner()
        responses = [
            completion([tool_call_message("c1", "fetch_udd",
                                          {"action": "list"})]),
            completion([finish_call("c2", EMPTY_REPORT)]),
        ]
        code, run_dir, model = self.run_cmd([], responses, runner=runner)
        self.assertEqual(code, EXIT_OK)
        result = json.loads(
            [m for m in model.requests[1]["messages"]
             if m["role"] == "tool"][0]["content"])
        self.assertEqual(result["action"], "list")
        for name in udd_mod.DATASET_NAMES:
            self.assertIn(f"**{name}**", result["catalog"])
        self.assertIn("no database query", result["note"])
        self.assertEqual(runner.calls, [],
                         "action=list 是纯目录，绝不触达 psql")
        self.assertEqual(
            os.listdir(os.path.join(run_dir, "observations")), [],
            "action=list 不产生观测")

    def test_fetch_udd_list_works_even_when_fetch_disabled(self):
        responses = [
            completion([tool_call_message("c1", "fetch_udd",
                                          {"action": "list"})]),
            completion([tool_call_message("c2", "fetch_udd", {
                "action": "fetch", "datasets": ["sources"]})]),
            completion([finish_call("c3", EMPTY_REPORT)]),
        ]
        code, _, model = self.run_cmd(["--no-udd"], responses)
        self.assertEqual(code, EXIT_OK)
        first = json.loads(
            [m for m in model.requests[1]["messages"]
             if m["role"] == "tool"][0]["content"])
        second = json.loads(
            [m for m in model.requests[2]["messages"]
             if m["role"] == "tool"][-1]["content"])
        self.assertEqual(first["action"], "list")
        self.assertIn("**sources**", first["catalog"])
        self.assertEqual(second["error"]["kind"], "capability-disabled")

    def test_fetch_udd_missing_datasets_rejected(self):
        responses = [
            completion([tool_call_message("c1", "fetch_udd",
                                          {"action": "fetch"})]),
            completion([finish_call("c2", EMPTY_REPORT)]),
        ]
        runner = FakeUddRunner()
        code, _, model = self.run_cmd([], responses, runner=runner)
        self.assertEqual(code, EXIT_OK)
        result = json.loads(
            [m for m in model.requests[1]["messages"]
             if m["role"] == "tool"][0]["content"])
        self.assertEqual(result["error"]["kind"], "argument-invalid")
        self.assertEqual(runner.calls, [])

    def test_fetch_udd_more_than_six_datasets_rejected(self):
        # udd.py 对 >6 数据集在 psql 之前抛 budget-exhausted（非 arg-invalid）
        seven = ["sources", "upstream", "urls", "binaries", "bugs",
                 "bugs_count", "wnpp"]
        responses = [
            completion([tool_call_message("c1", "fetch_udd", {
                "action": "fetch", "datasets": seven})]),
            completion([finish_call("c2", EMPTY_REPORT)]),
        ]
        runner = FakeUddRunner()
        code, _, model = self.run_cmd([], responses, runner=runner)
        self.assertEqual(code, EXIT_OK)
        result = json.loads(
            [m for m in model.requests[1]["messages"]
             if m["role"] == "tool"][0]["content"])
        self.assertEqual(result["error"]["kind"], "budget-exhausted")
        self.assertIn("dataset cap", result["error"]["message"])
        self.assertEqual(runner.calls, [])

    def test_fetch_udd_call_budget_exhaustion(self):
        responses = [
            completion([tool_call_message("c1", "fetch_udd", {
                "action": "fetch", "datasets": ["sources"]})]),
            completion([tool_call_message("c2", "fetch_udd", {
                "action": "fetch", "datasets": ["sources"]})]),
            completion([finish_call("c3", EMPTY_REPORT)]),
        ]
        runner = FakeUddRunner()
        code, _, model = self.run_cmd(["--udd-max-calls", "1"], responses,
                                      runner=runner)
        self.assertEqual(code, EXIT_OK)
        second = json.loads(
            [m for m in model.requests[2]["messages"]
             if m["role"] == "tool"][-1]["content"])
        self.assertEqual(second["error"]["kind"], "budget-exhausted")
        self.assertIn("call cap 1", second["error"]["message"])
        self.assertEqual(len(runner.calls), 1,
                         "超限的第二次调用绝不触达 psql")

    def test_fetch_udd_row_budget_exhaustion(self):
        responses = [
            completion([tool_call_message("c1", "fetch_udd", {
                "action": "fetch",
                "datasets": ["sources", "upstream_metadata"]})]),
            completion([finish_call("c2", EMPTY_REPORT)]),
        ]
        runner = FakeUddRunner()
        code, _, model = self.run_cmd(["--udd-max-rows", "25"], responses,
                                      runner=runner)
        self.assertEqual(code, EXIT_OK)
        result = json.loads(
            [m for m in model.requests[1]["messages"]
             if m["role"] == "tool"][0]["content"])
        self.assertEqual(result["error"]["kind"], "budget-exhausted")
        # 两个数字都在消息里：已用 0 + 预计 40 > 上限 25
        self.assertIn("40", result["error"]["message"])
        self.assertIn("25", result["error"]["message"])
        self.assertEqual(runner.calls, [],
                         "行预算在 psql 之前拒绝")

    def test_fetch_udd_columnar_rows_and_parallel_provenance(self):
        responses = [
            completion([tool_call_message("c1", "fetch_udd", {
                "action": "fetch", "datasets": ["sources"]})]),
            completion([finish_call("c2", EMPTY_REPORT)]),
        ]
        code, run_dir, model = self.run_cmd([], responses)
        self.assertEqual(code, EXIT_OK)
        result = json.loads(
            [m for m in model.requests[1]["messages"]
             if m["role"] == "tool"][0]["content"])
        entry = result["datasets_data"]["sources"]
        self.assertEqual(entry["columns"],
                         list(udd_mod.DATASETS["sources"]["columns"]))
        self.assertTrue(entry["rows"])
        for row in entry["rows"]:
            self.assertNotIsInstance(row, dict,
                                     "行必须是列式数组，不是行字典")
            self.assertEqual(len(row), len(entry["columns"]))
        self.assertEqual(entry["provenance"], ["udd:sources#row=1"])
        self.assertEqual(len(entry["provenance"]), len(entry["rows"]))
        homepage_col = entry["columns"].index("homepage")
        self.assertEqual(entry["rows"][0][homepage_col],
                         "https://example.org/pkgone/")
        summary = self.read_summary(run_dir)
        self.assertEqual(
            summary["fact_citation_index"]["udd:sources#row=1"],
            ["obs-0001"])

    def test_fetch_udd_row_budget_counts_exposed_rows_not_probe(self):
        rows = [["source", "rowpkg", f"1.{i}-1", "sid", QA_MAINTAINER,
                 "https://example.org/rowpkg/", "", "", "", "devel",
                 "optional", ""] for i in range(21)]
        runner = FakeUddRunner(extra_sources={
            "rowpkg": {"source": rows, "upstream": [], "metadata": [],
                       "packaging": []}})
        responses = [
            completion([tool_call_message("c1", "fetch_udd", {
                "action": "fetch", "datasets": ["sources"]})]),
            completion([tool_call_message("c2", "fetch_udd", {
                "action": "fetch", "datasets": ["sources"],
                "limit": 20})]),
            completion([finish_call("c3", EMPTY_REPORT)]),
        ]
        code, run_dir, model = self.run_cmd(
            ["--udd-max-rows", "40"], responses, runner=runner,
            package="rowpkg")
        self.assertEqual(code, EXIT_OK)
        second = json.loads(
            [m for m in model.requests[2]["messages"]
             if m["role"] == "tool"][-1]["content"])
        self.assertNotIn("error", second)
        self.assertEqual(
            second["datasets_data"]["sources"]["row_count"], 20)
        self.assertEqual(self.read_summary(run_dir)["counts"]["tool_calls"],
                         2)

    def test_fetch_udd_result_trimmed_to_24k_valid_json(self):
        big_source_rows = [
            ["source", "bigpkg", f"1.{i}-1", "sid", QA_MAINTAINER,
             "https://up.example.org/" + ("p" * 460) + f"/{i}/",
             "", "", "", "devel", "optional", ""]
            for i in range(1, 61)]
        runner = FakeUddRunner(extra_sources={
            "bigpkg": {"source": big_source_rows, "upstream": [],
                       "metadata": [], "packaging": []}})
        doomed = {"summary": "cite trimmed row", "candidates": [],
                  "uncertainties": "x",
                  "fact_evidence": ["udd:sources#row=50"]}
        responses = [
            completion([tool_call_message("c1", "fetch_udd", {
                "action": "fetch", "datasets": ["sources"],
                "limit": 50})]),
            completion([finish_call("c2", doomed)]),
        ]
        code, run_dir, model = self.run_cmd(
            ["--max-report-repairs", "0"], responses, runner=runner,
            package="bigpkg")
        self.assertEqual(code, EXIT_COMPLETED_WITH_ERRORS)
        content = [m for m in model.requests[1]["messages"]
                   if m["role"] == "tool"][0]["content"]
        self.assertLessEqual(len(content), 24000)
        payload = json.loads(content)   # 裁剪后仍是合法 JSON
        self.assertTrue(payload["trimmed"])
        self.assertIn("24000", payload["trim_note"])
        entry = payload["datasets_data"]["sources"]
        self.assertLess(entry["row_count"], 50)
        self.assertEqual(len(entry["provenance"]), entry["row_count"])
        self.assertTrue(entry["more_available"])
        self.assertGreaterEqual(entry["hidden_row_count"], 1)
        # 被裁掉的行绝不可引用：row=50 未暴露 → 终报被拒
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "invalid-report")
        self.assertTrue(any("udd:sources#row=50" in p for p in
                            summary["final_report_validation"]))

    def test_zero_udd_prefetch_before_model_loop(self):
        runner = FakeUddRunner()
        responses = [
            completion([tool_call_message("c1", "fetch_url",
                                          {"url": PKGONE_HOME})]),
            completion([finish_call("c2", VALID_REPORT)]),
        ]
        code, run_dir, _ = self.run_cmd([], responses, runner=runner)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(runner.calls, [],
                         "investigate 启动阶段零 UDD 预取（不查 facts、"
                         "不枚举 cohort、不校验成员资格）")
        summary = self.read_summary(run_dir)
        self.assertNotIn("input_provenance", summary)
        self.assertEqual(summary["parameters"]["udd_max_calls"], 6)
        self.assertEqual(summary["parameters"]["udd_max_rows"], 200)
        self.assertIn("no startup prefetch", summary["mode"]["udd"])
        raw_dir = os.path.join(run_dir, "raw")
        leftovers = [n for n in os.listdir(raw_dir)
                     if n.startswith("udd-facts-")]
        self.assertEqual(leftovers, [], "investigate 不再落 udd-facts CSV")

    def test_tool_registry_has_seven_tools_no_get_package(self):
        self.assertEqual(len(TOOL_NAMES), 7)
        self.assertNotIn("get_package", TOOL_NAMES)
        self.assertEqual(TOOL_NAMES, {
            "fetch_url", "search_web", "github_api", "bug_fetch",
            "fetch_udd", "read_evidence", "finish_investigation"})

    # -- github_api 工具（经公网 Fetcher + 固定头 + 证据 URL） ----------

    def test_github_repo_tool_fetches_with_fixed_headers(self):
        repo_json = json.dumps({
            "full_name": "RusXMMS/librcd",
            "html_url": "https://github.com/RusXMMS/librcd",
            "homepage": "https://rusxmms.sourceforge.io/",
            "description": "librcd is a character conversion library",
            "archived": False, "disabled": False, "fork": False,
            "created_at": "2015-01-01T00:00:00Z",
            "pushed_at": "2024-05-01T00:00:00Z",
            "default_branch": "master", "language": "C",
            "owner": {"login": "RusXMMS"},
        }).encode()

        class GitHubFetcher(StubFetcher):
            def __init__(self):
                super().__init__(body=repo_json)
                self.seen_headers = None

            def fetch(self, url, headers=None):
                self.seen_headers = headers
                return super().fetch(url, headers=headers)

        report = {
            "summary": "GitHub API confirms an unarchived repository",
            "candidates": [{
                "url": "https://github.com/RusXMMS/librcd",
                "why": "GitHub API repo record with recent push and "
                       "matching project description",
                "supporting_obs_ids": ["obs-0001"],
                "quote": "librcd is a character conversion library",
                "uncertainty": "API facts are account-side records; not "
                               "official identity proof",
            }],
            "uncertainties": "API record alone is weaker than page content",
        }
        responses = [
            completion([tool_call_message("c1", "github_api", {
                "action": "repo", "owner": "RusXMMS", "repo": "librcd"})]),
            completion([finish_call("c2", report)]),
        ]
        fetcher = GitHubFetcher()
        code, run_dir, model = self.run_cmd([], responses, fetcher=fetcher)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("https://api.github.com/repos/RusXMMS/librcd",
                      fetcher.fetched[0])
        self.assertEqual(fetcher.seen_headers["Accept"],
                         "application/vnd.github+json")
        self.assertEqual(fetcher.seen_headers["X-GitHub-Api-Version"],
                         "2022-11-28")
        result = json.loads(
            [m for m in model.requests[1]["messages"]
             if m["role"] == "tool"][0]["content"])
        self.assertEqual(result["data"]["html_url"],
                         "https://github.com/RusXMMS/librcd")
        self.assertFalse(result["data"]["archived"])
        self.assertIn("not official-identity", result["note"])
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "completed")

    def test_github_404_release_explicit_unknown_note(self):
        class NotFoundFetcher(StubFetcher):
            def fetch(self, url, headers=None):
                self.fetched.append(url)
                self.request_count += 1
                return FetchOutcome(ok=True, status=404, final_url=url,
                                    body=b'{"message": "Not Found"}')

        responses = [
            completion([tool_call_message("c1", "github_api", {
                "action": "releases", "owner": "ghost", "repo": "none"})]),
            completion([finish_call("c2", {
                "summary": "no release info obtainable",
                "candidates": [],
                "uncertainties": "404 is unknown, not death",
            })]),
        ]
        code, run_dir, model = self.run_cmd(
            [], responses, fetcher=NotFoundFetcher())
        self.assertEqual(code, EXIT_OK)
        result = json.loads(
            [m for m in model.requests[1]["messages"]
             if m["role"] == "tool"][0]["content"])
        self.assertEqual(result["error"]["kind"], "http-404")
        self.assertIn("NOT proof of death", result["note"])

    def test_github_readme_malformed_array_is_honest_tool_error(self):
        class ArrayReadmeFetcher(StubFetcher):
            def fetch(self, url, headers=None):
                self.fetched.append(url)
                self.request_count += 1
                return FetchOutcome(ok=True, status=200, final_url=url,
                                    body=b'["unexpected", "array"]')

        responses = [
            completion([tool_call_message("c1", "github_api", {
                "action": "readme", "owner": "RusXMMS", "repo": "librcd"})]),
            completion([finish_call("c2", EMPTY_REPORT)]),
        ]
        code, run_dir, model = self.run_cmd(
            [], responses, fetcher=ArrayReadmeFetcher())
        self.assertEqual(code, EXIT_OK)
        result = json.loads(
            [m for m in model.requests[1]["messages"]
             if m["role"] == "tool"][0]["content"])
        self.assertEqual(result["error"]["kind"], "malformed-json",
                         "malformed array must be an honest malformed-json "
                         "tool error")
        self.assertNotIn("tool-internal", json.dumps(result))
        self.assertNotIn("data", result)

    def test_github_rate_limit_headers_exposed(self):
        from gleam.net import HopResult

        class RateLimitedFetcher(StubFetcher):
            def fetch(self, url, headers=None):
                self.fetched.append(url)
                self.request_count += 1
                hop = HopResult(
                    url=url, status=429,
                    headers={"x-ratelimit-limit": ["60"],
                             "x-ratelimit-remaining": ["0"],
                             "x-ratelimit-reset": ["1730000000"],
                             "retry-after": ["42"]},
                    body=b'{"message": "API rate limit exceeded"}')
                return FetchOutcome(ok=True, status=429, final_url=url,
                                    body=hop.body, hops=[hop])

        responses = [
            completion([tool_call_message("c1", "github_api", {
                "action": "commits", "owner": "RusXMMS", "repo": "librcd"})]),
            completion([finish_call("c2", EMPTY_REPORT)]),
        ]
        code, run_dir, model = self.run_cmd(
            [], responses, fetcher=RateLimitedFetcher())
        self.assertEqual(code, EXIT_OK)
        result = json.loads(
            [m for m in model.requests[1]["messages"]
             if m["role"] == "tool"][0]["content"])
        self.assertEqual(result["error"]["kind"], "rate-limited")
        self.assertEqual(result["rate_limit"]["x-ratelimit-remaining"], "0")
        self.assertEqual(result["rate_limit"]["retry-after"], "42")
        self.assertIn("no automatic retry",
                      result["error"]["message"])

    def test_github_disabled_reports_capability(self):
        responses = [
            completion([tool_call_message("c1", "github_api", {
                "action": "repo", "owner": "RusXMMS", "repo": "librcd"})]),
            completion([finish_call("c2", EMPTY_REPORT)]),
        ]
        code, run_dir, model = self.run_cmd(["--no-github"], responses)
        self.assertEqual(code, EXIT_OK)
        result = json.loads(
            [m for m in model.requests[1]["messages"]
             if m["role"] == "tool"][0]["content"])
        self.assertEqual(result["error"]["kind"], "capability-disabled")

    def test_github_invalid_owner_rejected_before_any_fetch(self):
        responses = [
            completion([tool_call_message("c1", "github_api", {
                "action": "repo", "owner": "../etc", "repo": "librcd"})]),
            completion([finish_call("c2", EMPTY_REPORT)]),
        ]
        fetcher = StubFetcher()
        code, run_dir, _ = self.run_cmd([], responses, fetcher=fetcher)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(fetcher.fetched, [],
                         "invalid owner must not reach the network")

    # -- read_evidence 偏移翻页 -------------------------------------------

    def test_read_evidence_offset_pages_forward_not_same_prefix(self):
        marker_far = "FAR MARKER TEXT BEYOND FIRST WINDOW"
        filler = "lorem ipsum dolor sit amet " * 900   # ~23k chars
        body = (f"<html><body><p>{QUOTE}</p><p>{filler}</p>"
                f"<p>{marker_far}</p>"
                f"<a href='https://git.example.org/pkgone.git'>x</a>"
                f"</body></html>").encode()
        responses = [
            completion([tool_call_message("c1", "fetch_url",
                                          {"url": PKGONE_HOME})]),
            completion([tool_call_message("c2", "read_evidence",
                                          {"observation_id": "obs-0001"})]),
            completion([tool_call_message("c3", "read_evidence", {
                "observation_id": "obs-0001", "offset": 16000})]),
            completion([finish_call("c4", {
                "summary": "paged evidence to the far marker",
                "candidates": [{
                    "url": "https://git.example.org/pkgone.git",
                    "why": "link on the fetched page",
                    "supporting_obs_ids": ["obs-0001"],
                    "quote": marker_far,
                    "uncertainty": "none",
                }],
                "uncertainties": "single page",
            })]),
        ]
        code, run_dir, model = self.run_cmd(
            [], responses, fetcher=StubFetcher(body=body))
        self.assertEqual(code, EXIT_OK)
        second = json.loads(
            [m for m in model.requests[2]["messages"]
             if m["role"] == "tool"][-1]["content"])
        self.assertEqual(second["body_text_window"]["offset"], 0)
        self.assertEqual(second["next_offset"], 16000)
        self.assertNotIn(marker_far, second["body_text"])
        third = json.loads(
            [m for m in model.requests[3]["messages"]
             if m["role"] == "tool"][-1]["content"])
        self.assertEqual(third["body_text_window"]["offset"], 16000)
        self.assertIn(marker_far, third["body_text"])
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "completed",
                         "quote from a paged window must validate")

    def test_read_evidence_offset_validation(self):
        responses = [
            completion([tool_call_message("c1", "fetch_url",
                                          {"url": PKGONE_HOME})]),
            completion([tool_call_message("c2", "read_evidence", {
                "observation_id": "obs-0001", "offset": 999999})]),
            completion([tool_call_message("c3", "read_evidence", {
                "observation_id": "obs-0001", "offset": -5})]),
            completion([tool_call_message("c4", "read_evidence", {
                "observation_id": "obs-0001", "links_offset": "zero"})]),
            completion([finish_call("c5", EMPTY_REPORT)]),
        ]
        code, run_dir, model = self.run_cmd([], responses)
        self.assertEqual(code, EXIT_OK)
        tool_results = [json.loads(m["content"]) for m in
                        model.requests[4]["messages"]
                        if m["role"] == "tool"][-3:]
        self.assertEqual(tool_results[0]["error"]["kind"],
                         "argument-invalid")
        self.assertIn("beyond the end", tool_results[0]["error"]["message"])
        self.assertEqual(tool_results[1]["error"]["kind"],
                         "argument-invalid")
        self.assertEqual(tool_results[2]["error"]["kind"],
                         "argument-invalid")

    def test_tool_counts_by_name_preserved_in_summary(self):
        response = search_ok([("t", "https://x.example/a", "s", ["bing"])])
        responses = [
            completion([tool_call_message("c1", "search_web",
                                          {"query": "pkgone"})]),
            completion([tool_call_message("c2", "fetch_url",
                                          {"url": "https://bad.example/"})]),
            completion([finish_call("c3", EMPTY_REPORT)]),
        ]
        code, run_dir, _ = self.run_cmd(
            ["--max-report-repairs", "0"], responses,
            fetcher=StubFetcher(error={"kind": "timeout",
                                       "message": "deadline"}),
            search_client=StubSearch(response=response))
        self.assertEqual(code, EXIT_OK)
        summary = self.read_summary(run_dir)
        by_name = summary["counts"]["tool_calls_by_name"]
        self.assertEqual(by_name["search_web"],
                         {"calls": 1, "errors": 0})
        self.assertEqual(by_name["fetch_url"]["calls"], 1)
        self.assertEqual(by_name["fetch_url"]["errors"], 1)

    # -- 模型端错误 / CLI 用法 / 存储 ------------------------------------

    def test_model_endpoint_unreachable_is_model_error(self):
        def unreachable(*_a, **_k):
            raise OSError("connection refused")

        args = build_parser().parse_args(
            ["investigate", "--package", "pkgone",
             "--store", self.store])
        runs_root = os.path.join(self.store, "runs")
        before = set(os.listdir(runs_root)) if os.path.isdir(runs_root) else set()
        with patch_udd_client(FakeUddRunner()):
            with mock.patch("gleam.cli.Fetcher", return_value=StubFetcher()):
                with mock.patch("gleam.models._http_json",
                                side_effect=unreachable):
                    code = cmd_investigate(args)
        self.assertEqual(code, EXIT_COMPLETED_WITH_ERRORS)
        new_dirs = set(os.listdir(runs_root)) - before
        run_dir = os.path.join(runs_root, sorted(new_dirs)[0])
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "model-error")
        record_path = os.path.join(run_dir, "models", "inv-r001.json")
        with open(record_path, encoding="utf-8") as fh:
            record = json.load(fh)
        self.assertEqual(record["error"]["kind"], "connection-error")

    def test_usage_errors_rejected_before_run_created(self):
        bad_argvs = [
            ["--max-rounds", "0"],
            ["--max-tool-calls", "0"],
            ["--max-rounds", "65"],
            ["--max-context-chars", "100"],
            ["--max-body-chars", "10"],
            ["--max-requests", "0"],
            ["--timeout", "-1"],
            ["--timeout", "nan"],
            ["--model-max-tokens", "0"],
            ["--model-timeout", "601"],
            ["--udd-max-calls", "0"],
            ["--udd-max-calls", "51"],
            ["--udd-max-rows", "0"],
            ["--udd-max-rows", "2001"],
        ]
        for extra in bad_argvs:
            responses = [completion([finish_call("c1", VALID_REPORT)])]
            code, run_dir, _ = self.run_cmd(extra, responses)
            self.assertEqual(code, EXIT_USAGE, extra)
            self.assertIsNone(run_dir, f"非法参数不得创建运行: {extra}")

    def test_investigate_requires_exactly_one_package(self):
        args = build_parser().parse_args(
            ["investigate",
             "--package", "pkgone", "--package", "pkgtwo",
             "--store", self.store])
        with mock.patch("gleam.models._http_json",
                        side_effect=AssertionError("must not call model")):
            code = cmd_investigate(args)
        self.assertEqual(code, EXIT_USAGE)

    def test_investigate_invalid_package_name_is_usage_error(self):
        # 零预取语义：包名只做 Debian 源名策略校验（不查 UDD、无 exit 3）
        args = build_parser().parse_args(
            ["investigate", "--package", "Bad Name!", "--store", self.store])
        with mock.patch("gleam.models._http_json",
                        side_effect=AssertionError("must not call model")):
            code = cmd_investigate(args)
        self.assertEqual(code, EXIT_USAGE)

    def test_store_check_passes_after_investigate_run(self):
        responses = [
            completion([tool_call_message("c1", "fetch_url",
                                          {"url": PKGONE_HOME})]),
            completion([finish_call("c2", VALID_REPORT)]),
        ]
        code, run_dir, _ = self.run_cmd([], responses)
        self.assertEqual(code, EXIT_OK)
        from gleam.store import verify_store
        problems = [p for p in verify_store(self.store)
                    if "out of sync" not in p]
        self.assertEqual(problems, [],
                         f"investigate 产物必须通过 check: {problems}")

    # -- 主审 blocker 回归：畸形类型不得以 TypeError 逃逸 ----------------

    def test_unhashable_id_and_citation_elements_rejected_not_crash(self):
        bad = json.loads(json.dumps(VALID_REPORT))
        bad["candidates"][0]["supporting_obs_ids"] = [["obs-0001"]]
        worse = json.loads(json.dumps(bad))
        worse["fact_evidence"] = [{"x": 1}]
        responses = [
            completion([tool_call_message("c1", "fetch_url",
                                          {"url": PKGONE_HOME})]),
            completion([finish_call("c2", bad)]),
            completion([finish_call("c3", worse)]),
        ]
        code, run_dir, _ = self.run_cmd(["--max-report-repairs", "1"],
                                        responses)
        self.assertEqual(code, EXIT_COMPLETED_WITH_ERRORS)
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "invalid-report")
        problems = summary["final_report_validation"]
        self.assertTrue(any("must be a string, got list" in p
                            for p in problems), problems)
        self.assertTrue(any("must be a string, got dict" in p
                            for p in problems), problems)

    def test_report_unknown_keys_rejected(self):
        bad = json.loads(json.dumps(VALID_REPORT))
        bad["candidates"][0]["injected_key"] = "x"
        responses = [
            completion([tool_call_message("c1", "fetch_url",
                                          {"url": PKGONE_HOME})]),
            completion([finish_call("c2", bad)]),
        ]
        code, run_dir, _ = self.run_cmd(["--max-report-repairs", "0"],
                                        responses)
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "invalid-report")
        self.assertTrue(any("unknown keys" in p for p in
                            summary["final_report_validation"]))

    def test_envelope_missing_id_rejected_nothing_executed(self):
        raw = {"id": None, "type": "function",
               "function": {"name": "fetch_url",
                            "arguments": json.dumps({"url": PKGONE_HOME})}}
        responses = [completion([raw])]
        code, run_dir, _ = self.run_cmd(["--max-rounds", "1"], responses)
        self.assertEqual(code, EXIT_COMPLETED_WITH_ERRORS)
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "model-error")
        self.assertIn("malformed-tool-calls",
                      summary["status_detail"] + json.dumps(summary))
        self.assertEqual(os.listdir(os.path.join(run_dir, "observations")),
                         [], "malformed envelope must execute nothing")
        self.assertFalse(os.path.exists(
            os.path.join(run_dir, "models", "inv-tools-r001.json")))

    def test_envelope_duplicate_ids_rejected_nothing_executed(self):
        call = tool_call_message("same-id", "fetch_udd",
                                 {"action": "list"})
        responses = [completion([call, dict(call)])]
        code, run_dir, _ = self.run_cmd(["--max-rounds", "1"], responses)
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "model-error")
        self.assertEqual(os.listdir(os.path.join(run_dir, "observations")),
                         [])

    def test_envelope_function_string_rejected_not_crash(self):
        raw = {"id": "c1", "type": "function",
               "function": "fetch_url"}
        responses = [completion([raw])]
        code, run_dir, _ = self.run_cmd(["--max-rounds", "1"], responses)
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "model-error")
        self.assertIn("malformed-tool-calls", json.dumps(summary["rounds"]))

    # -- 主审 blocker：上下文硬上限 --------------------------------------

    def test_context_hard_cap_stops_before_request(self):
        huge_prose = "I will now reason at length. " * 260  # ~5.5k chars
        responses = [
            completion([tool_call_message("c1", "fetch_udd", {
                "action": "fetch", "datasets": ["sources"]})],
                       content=huge_prose),
            completion([tool_call_message("c2", "fetch_udd", {
                "action": "fetch", "datasets": ["sources"]})]),
        ]
        code, run_dir, model = self.run_cmd(
            ["--max-context-chars", "4000", "--max-rounds", "3"],
            responses)
        self.assertEqual(code, EXIT_COMPLETED_WITH_ERRORS)
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "context-budget-exhausted")
        self.assertEqual(len(model.requests), 1,
                         "no request may be sent once the cap cannot hold")
        self.assertFalse(os.path.exists(
            os.path.join(run_dir, "models", "inv-r002.json")))
        self.assertTrue(os.path.exists(
            os.path.join(run_dir, "models", "inv-tools-r001.json")),
            "partial trajectory (tool results) preserved")

    # -- 主审 blocker：证据绑定到被引观测自身 ----------------------------

    def test_url_must_be_in_cited_observation_not_elsewhere(self):
        other_page = (b"<html><body><p>completely unrelated page content"
                      b"</p></body></html>")

        class TwoPageFetcher(StubFetcher):
            def fetch(self, url):
                self.fetched.append(url)
                self.request_count += 1
                body = other_page if url.endswith("2") else self.body
                return FetchOutcome(ok=True, status=200, final_url=url,
                                    redirect_chain=[], body=body)

        misattributed = json.loads(json.dumps(VALID_REPORT))
        misattributed["candidates"][0]["supporting_obs_ids"] = ["obs-0002"]
        responses = [
            completion([tool_call_message("c1", "fetch_url",
                                          {"url": PKGONE_HOME})]),
            completion([tool_call_message("c2", "fetch_url",
                                          {"url": PKGONE_HOME + "2"})]),
            completion([finish_call("c3", misattributed)]),
            completion([finish_call("c4", VALID_REPORT)]),
        ]
        code, run_dir, _ = self.run_cmd([], responses, fetcher=TwoPageFetcher())
        self.assertEqual(code, EXIT_OK)
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "completed")
        with open(os.path.join(run_dir, "models",
                               "inv-tools-r003.json"),
                  encoding="utf-8") as fh:
            rejection = json.load(fh)["extra"]["tool_calls"][0]["result"]
        self.assertFalse(rejection["accepted"])
        self.assertTrue(any("not among the URLs observed by the cited"
                            in p for p in rejection["problems"]),
                        rejection["problems"])

    def test_quote_beyond_exposed_window_rejected_until_read_evidence(self):
        marker = "SECRET MARKER TEXT FOR EXPOSURE TEST"
        long_body = ((b"<html><body>" + b"filler text block " * 800)
                     + f"<p>{marker}</p>".encode()
                     + b"<a href='https://git.example.org/pkgone.git'>x</a>"
                     + b"</body></html>")
        beyond = json.loads(json.dumps(VALID_REPORT))
        beyond["candidates"][0]["quote"] = marker
        responses = [
            completion([tool_call_message("c1", "fetch_url",
                                          {"url": PKGONE_HOME})]),
            completion([finish_call("c2", beyond)]),
            completion([tool_call_message("c3", "read_evidence",
                                          {"observation_id": "obs-0001"})]),
            completion([finish_call("c4", beyond)]),
        ]
        code, run_dir, _ = self.run_cmd([], responses,
                                        fetcher=StubFetcher(body=long_body))
        self.assertEqual(code, EXIT_OK)
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "completed")
        with open(os.path.join(run_dir, "models",
                               "inv-tools-r002.json"),
                  encoding="utf-8") as fh:
            rejected = json.load(fh)["extra"]["tool_calls"][0]["result"]
        self.assertFalse(rejected["accepted"])
        self.assertTrue(any("as returned to you in tool results" in p
                            for p in rejected["problems"]),
                        rejected["problems"])

    def test_failed_fetch_requested_url_alone_is_not_evidence(self):
        doomed = json.loads(json.dumps(VALID_REPORT))
        doomed["candidates"][0]["url"] = "https://dead.example/never-fetched/"
        doomed["candidates"][0]["supporting_obs_ids"] = ["obs-0001"]
        doomed["candidates"][0]["quote"] = "error means unknown filler"
        responses = [
            completion([tool_call_message("c1", "fetch_url",
                                          {"url": "https://dead.example/x"})]),
            completion([finish_call("c2", doomed)]),
        ]
        code, run_dir, _ = self.run_cmd(
            ["--max-report-repairs", "0"], responses,
            fetcher=StubFetcher(error={"kind": "timeout",
                                       "message": "deadline"}))
        self.assertEqual(code, EXIT_COMPLETED_WITH_ERRORS)
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "invalid-report")
        self.assertTrue(any("not among the URLs observed" in p for p in
                            summary["final_report_validation"]))

    # -- 主审 blocker：每轮工具结果独立落盘 -------------------------------

    def test_tool_results_persisted_even_when_budget_ends(self):
        responses = [
            completion([tool_call_message("c1", "fetch_udd", {
                "action": "fetch", "datasets": ["sources"]})]),
            completion([tool_call_message("c2", "fetch_udd", {
                "action": "fetch", "datasets": ["sources"]})]),
        ]
        code, run_dir, _ = self.run_cmd(["--max-rounds", "2"], responses)
        self.assertEqual(code, EXIT_COMPLETED_WITH_ERRORS)
        for i in (1, 2):
            path = os.path.join(run_dir, "models", f"inv-tools-r{i:03d}.json")
            with open(path, encoding="utf-8") as fh:
                record = json.load(fh)
            result = record["extra"]["tool_calls"][0]["result"]
            self.assertIn("pkgone", result["package"])
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["rounds"][0]["tools_file"],
                         "models/inv-tools-r001.json")

    def test_finish_results_persisted_with_problems(self):
        bad = json.loads(json.dumps(VALID_REPORT))
        bad["candidates"][0]["supporting_obs_ids"] = ["obs-4242"]
        responses = [
            completion([tool_call_message("c1", "fetch_url",
                                          {"url": PKGONE_HOME})]),
            completion([finish_call("c2", bad)]),
            completion([finish_call("c3", VALID_REPORT)]),
        ]
        code, run_dir, _ = self.run_cmd([], responses)
        self.assertEqual(code, EXIT_OK)
        with open(os.path.join(run_dir, "models",
                               "inv-tools-r002.json"),
                  encoding="utf-8") as fh:
            rejected = json.load(fh)["extra"]["tool_calls"][0]["result"]
        self.assertFalse(rejected["accepted"])
        with open(os.path.join(run_dir, "models",
                               "inv-tools-r003.json"),
                  encoding="utf-8") as fh:
            accepted = json.load(fh)["extra"]["tool_calls"][0]["result"]
        self.assertTrue(accepted["accepted"])
        self.assertEqual(accepted["report"]["candidates"][0]["url"],
                         "https://git.example.org/pkgone.git")

    def test_accepted_finish_skips_remaining_siblings_no_network(self):
        responses = [
            completion([tool_call_message("c1", "fetch_url",
                                          {"url": PKGONE_HOME})]),
            completion([
                finish_call("c2", VALID_REPORT),
                tool_call_message("c3", "fetch_url", {"url": PKGONE_HOME}),
            ]),
        ]
        fetcher = StubFetcher()
        code, run_dir, _ = self.run_cmd([], responses, fetcher=fetcher)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(fetcher.fetched, [PKGONE_HOME],
                         "sibling after accepted finish must not execute")
        obs_dir = os.path.join(run_dir, "observations")
        self.assertEqual(sorted(os.listdir(obs_dir)), ["obs-0001.json"])
        with open(os.path.join(run_dir, "models", "inv-tools-r002.json"),
                  encoding="utf-8") as fh:
            tools = json.load(fh)["extra"]["tool_calls"]
        self.assertEqual(tools[1]["name"], "fetch_url")
        self.assertFalse(tools[1]["executed"])
        self.assertEqual(tools[1]["result_kind"], "skipped-after-finish")
        self.assertEqual(tools[1]["result"],
                         {"skipped": "investigation finished"})

    def test_terminal_rejected_finish_skips_siblings_no_network(self):
        bad = json.loads(json.dumps(VALID_REPORT))
        bad["candidates"][0]["supporting_obs_ids"] = ["obs-4242"]
        responses = [
            completion([tool_call_message("c1", "fetch_url",
                                          {"url": PKGONE_HOME})]),
            completion([
                finish_call("c2", bad),
                tool_call_message("c3", "fetch_url", {"url": PKGONE_HOME}),
            ]),
        ]
        fetcher = StubFetcher()
        code, run_dir, _ = self.run_cmd(["--max-report-repairs", "0"],
                                        responses, fetcher=fetcher)
        self.assertEqual(code, EXIT_COMPLETED_WITH_ERRORS)
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "invalid-report")
        self.assertEqual(fetcher.fetched, [PKGONE_HOME],
                         "sibling after terminal rejection must not execute")
        with open(os.path.join(run_dir, "models", "inv-tools-r002.json"),
                  encoding="utf-8") as fh:
            tools = json.load(fh)["extra"]["tool_calls"]
        self.assertEqual(tools[1]["result_kind"], "skipped-after-finish")
        self.assertFalse(tools[1]["executed"])

    def test_second_finish_in_same_message_does_not_revalidate(self):
        other = json.loads(json.dumps(VALID_REPORT))
        other["summary"] = "a second, different report that must be ignored"
        other["candidates"] = []
        responses = [
            completion([tool_call_message("c1", "fetch_url",
                                          {"url": PKGONE_HOME})]),
            completion([
                finish_call("c2", VALID_REPORT),
                finish_call("c3", other),
            ]),
        ]
        code, run_dir, _ = self.run_cmd([], responses)
        self.assertEqual(code, EXIT_OK)
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["final_report_validation"], "ok")
        self.assertEqual(summary["final_report"]["summary"],
                         VALID_REPORT["summary"],
                         "first accepted report must be retained")
        with open(os.path.join(run_dir, "models", "inv-tools-r002.json"),
                  encoding="utf-8") as fh:
            tools = json.load(fh)["extra"]["tool_calls"]
        self.assertEqual(tools[1]["result_kind"], "skipped-after-finish")
        self.assertEqual(tools[1]["result"],
                         {"skipped": "investigation finished"})

    def test_fetch_udd_exposes_row_provenance_for_citation(self):
        report = {"summary": "live UDD rows cited correctly",
                  "candidates": [], "uncertainties": "none",
                  "fact_evidence": ["udd:upstream#row=1"]}
        responses = [
            completion([tool_call_message("c1", "fetch_udd", {
                "action": "fetch", "datasets": ["upstream"]})]),
            completion([finish_call("c2", report)]),
        ]
        code, run_dir, model = self.run_cmd([], responses)
        self.assertEqual(code, EXIT_OK)
        result = json.loads(
            [m for m in model.requests[1]["messages"]
             if m["role"] == "tool"][0]["content"])
        upstream = result["datasets_data"]["upstream"]
        self.assertEqual(upstream["provenance"], ["udd:upstream#row=1"])
        self.assertEqual(result["template_version"],
                         "udd-fetch-templates-v3")
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "completed",
                         "fetch_udd 暴露过的 provenance 必须可被终报引用")

    # -- 复审缺陷回归 -----------------------------------------------------

    def test_github_malformed_shapes_are_honest_malformed_json(self):
        # repo 收到数组 / releases、commits 收到对象：整形视图只有 error
        # 键时必须与 readme 分支同款以 malformed-json 暴露，绝不伪装成
        # 成功 data
        for action, body in (
                ("repo", b'[{"full_name": "o/r"}]'),
                ("releases", b'{"error": "not an array"}'),
                ("commits", b'{"message": "not an array"}')):
            with self.subTest(action=action):
                responses = [
                    completion([tool_call_message("c1", "github_api", {
                        "action": action, "owner": "RusXMMS",
                        "repo": "librcd"})]),
                    completion([finish_call("c2", EMPTY_REPORT)]),
                ]
                code, _, model = self.run_cmd(
                    [], responses, fetcher=StubFetcher(body=body))
                self.assertEqual(code, EXIT_OK)
                result = json.loads(
                    [m for m in model.requests[1]["messages"]
                     if m["role"] == "tool"][0]["content"])
                self.assertEqual(result["error"]["kind"], "malformed-json",
                                 result)
                self.assertNotIn("data", result)
                self.assertNotIn("tool-internal", json.dumps(result))

    def test_tool_internal_error_counts_in_by_name_errors(self):
        # 派发异常（→ tool-internal-error）也必须计入 by-name 错误数
        responses = [
            completion([tool_call_message("c1", "search_web",
                                          {"query": "pkgone"})]),
            completion([finish_call("c2", EMPTY_REPORT)]),
        ]
        code, run_dir, _ = self.run_cmd(
            [], responses, search_client=StubSearch(
                error=RuntimeError("boom")))
        self.assertEqual(code, EXIT_OK)
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["counts"]["tool_calls_by_name"]
                         ["search_web"], {"calls": 1, "errors": 1})
        self.assertTrue(any("tool-internal-error" in e
                            for e in summary["errors"]), summary["errors"])
        self.assertEqual(summary["status"], "completed")

    def test_non_string_content_replayed_as_empty_string(self):
        # OpenAI 兼容端点可能给数组形态 content：回放只接受 str，其余按 ""
        responses = [
            completion([tool_call_message("c1", "fetch_udd",
                                          {"action": "list"})],
                       content=["array", "content"]),
            completion([finish_call("c2", EMPTY_REPORT)]),
        ]
        code, _, model = self.run_cmd([], responses)
        self.assertEqual(code, EXIT_OK)
        replay = [m for m in model.requests[1]["messages"]
                  if m["role"] == "assistant"][0]
        self.assertEqual(replay["content"], "")

    def test_udd_url_with_fragment_is_valid_candidate(self):
        # UDD 行 URL 带 #fragment：登记时规范化，verbatim 引用必须通过
        rows = [["source", "fragpkg", "1.0-1", "sid", QA_MAINTAINER,
                 "https://frag.example.org/upstream/#home", "", "", "",
                 "devel", "optional", ""]]
        runner = FakeUddRunner(extra_sources={
            "fragpkg": {"source": rows, "upstream": [], "metadata": [],
                        "packaging": []}})
        report = {
            "summary": "UDD homepage row with fragment cited verbatim",
            "candidates": [{
                "url": "https://frag.example.org/upstream/#home",
                "why": "homepage field of the live UDD sources row",
                "supporting_obs_ids": ["obs-0001"],
                "quote": "https://frag.example.org/upstream/#home",
                "uncertainty": "database fact, weaker than page content",
            }],
            "uncertainties": "none",
        }
        responses = [
            completion([tool_call_message("c1", "fetch_udd", {
                "action": "fetch", "datasets": ["sources"]})]),
            completion([finish_call("c2", report)]),
        ]
        code, run_dir, _ = self.run_cmd([], responses, runner=runner,
                                        package="fragpkg")
        self.assertEqual(code, EXIT_OK)
        summary = self.read_summary(run_dir)
        self.assertEqual(summary["status"], "completed")


class CapContextTest(unittest.TestCase):
    """cap_context 收敛回归：400–600 字符区间信封反膨胀缺陷。"""

    @staticmethod
    def tool_body(chars):
        return json.dumps({"observation_id": "obs-0001",
                           "body": "x" * chars})

    def test_truncated_tool_content_is_valid_json(self):
        from gleam.agent import cap_context
        messages = [
            {"role": "system", "content": "sys " * 20},
            {"role": "user", "content": "task " * 20},
            {"role": "tool", "tool_call_id": "c1",
             "content": self.tool_body(900)},
            {"role": "tool", "tool_call_id": "c2",
             "content": self.tool_body(700)},
        ]
        log = []
        self.assertTrue(cap_context(messages, 1500, log, 1))
        tool_contents = [m["content"] for m in messages
                         if m.get("role") == "tool"]
        self.assertTrue(tool_contents)
        for content in tool_contents:
            parsed = json.loads(content)  # 永远必须可解析
            self.assertIs(parsed.get("truncated"), True)

    def test_mid_size_tool_messages_converge(self):
        # ~450 字符工具消息：旧实现信封（≈250 开销+保留文本）反而更大，
        # 护栏空转后谎报 context-budget-exhausted；现在必须收敛
        from gleam.agent import cap_context
        messages = [
            {"role": "system", "content": "sys " * 20},
            {"role": "user", "content": "task " * 20},
            {"role": "tool", "tool_call_id": "c1",
             "content": self.tool_body(400)},
            {"role": "tool", "tool_call_id": "c2",
             "content": self.tool_body(400)},
        ]
        log = []
        self.assertTrue(cap_context(messages, 1200, log, 1),
                        json.dumps(messages))
        self.assertTrue(all(json.loads(m["content"]) for m in messages
                            if m.get("role") == "tool"))

    def test_no_nested_envelopes_on_repeated_capping(self):
        from gleam.agent import cap_context
        messages = [
            {"role": "system", "content": "sys " * 20},
            {"role": "user", "content": "task " * 20},
            {"role": "tool", "tool_call_id": "c1",
             "content": self.tool_body(900)},
        ]
        log = []
        self.assertTrue(cap_context(messages, 1000, log, 1))
        self.assertTrue(cap_context(messages, 700, log, 2))
        for m in messages:
            if m.get("role") != "tool":
                continue
            env = json.loads(m["content"])
            self.assertIsInstance(env["text"], str)
            self.assertFalse(env["text"].startswith('{"truncated"'),
                             "envelope text must never nest an envelope")


if __name__ == "__main__":
    unittest.main()
