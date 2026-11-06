import argparse
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from gleam import udd as udd_mod
from gleam.cli import (EXIT_FATAL, EXIT_OK, EXIT_USAGE, build_parser,
                       cmd_fetch_udd, cmd_run, main)
from gleam.net import FetchOutcome
from gleam.store import RunStore

from .fixtures import FakeUddRunner, patch_udd_client, write_fixture

VALID_ANSWER = {
    "abstain": False,
    "summary": "reachable",
    "proposals": [{"action": "no-action",
                    "rationale": "first observation",
                    "evidence_obs_ids": ["obs-0001"]}],
}


class StubFetcher:
    def __init__(self, body=b"page"):
        self.policy = {"max_bytes": 1024, "timeout_s": 5.0, "max_redirects": 3,
                       "max_requests": 10, "min_interval_s": 0}
        self._robots_bodies = {}
        self.body = body

    def fetch(self, url):
        return FetchOutcome(ok=True, status=200, final_url=url,
                            redirect_chain=[], body=self.body)


class RunCliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = os.path.join(cls.tmp.name, "store")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_cmd(self, argv, fetcher=None, http=None, runner=None):
        args = build_parser().parse_args(
            ["run"] + argv)
        runs_root = os.path.join(self.store, "runs")
        before = set(os.listdir(runs_root)) if os.path.isdir(runs_root) else set()
        if http is None:
            def http(*_a, **_k):
                raise OSError("no model in unit test")
        udd_patch = patch_udd_client(runner or FakeUddRunner())
        with udd_patch:
            with mock.patch("gleam.cli.Fetcher",
                            return_value=fetcher or StubFetcher()):
                with mock.patch("gleam.models._http_json", side_effect=http):
                    code = cmd_run(args)
        new_dirs = set(os.listdir(runs_root)) - before if os.path.isdir(runs_root) else set()
        self.assertLessEqual(len(new_dirs), 1, "at most one new run dir per run")
        run_dir = os.path.join(runs_root, sorted(new_dirs)[0]) if new_dirs else None
        return code, run_dir

    def test_url_requires_exactly_one_package(self):
        self.assertEqual(self.run_cmd(["--url", "https://x.example/",
                                       "--store", self.store])[0], EXIT_USAGE)
        self.assertEqual(
            self.run_cmd(["--url", "https://x.example/",
                          "--package", "pkgone", "--package", "pkgtwo",
                          "--store", self.store])[0], EXIT_USAGE)

    def test_unknown_package_is_fatal(self):
        self.assertEqual(
            self.run_cmd(["--package", "nosuch", "--store", self.store])[0],
            EXIT_FATAL)

    def test_limit_must_be_positive(self):
        self.assertEqual(
            self.run_cmd(["--limit", "0", "--store", self.store])[0], EXIT_USAGE)

    def test_model_max_tokens_must_be_bounded_positive(self):
        for bad in ("0", "-5", "70000"):
            code, _ = self.run_cmd(["--model-max-tokens", bad,
                                    "--package", "pkgone", "--no-model",
                                    "--store", self.store])
            self.assertEqual(code, EXIT_USAGE, bad)

    def test_model_timeout_must_be_finite_positive_bounded(self):
        for bad in ("0", "-5", "nan", "inf", "601"):
            code, _ = self.run_cmd(["--model-timeout", bad,
                                    "--package", "pkgone", "--no-model",
                                    "--store", self.store])
            self.assertEqual(code, EXIT_USAGE, bad)

    def test_network_limits_validated_before_run_created(self):
        bad_argvs = [
            ["--max-bytes", "0"],
            ["--timeout", "-1"],
            ["--timeout", "nan"],
            ["--max-redirects", "-1"],
            ["--max-requests", "0"],
            ["--min-interval", "-2"],
            ["--min-interval", "inf"],
        ]
        for extra in bad_argvs:
            code, run_dir = self.run_cmd(
                extra + ["--package", "pkgone", "--no-model",
                         "--store", self.store])
            self.assertEqual(code, EXIT_USAGE, extra)
            self.assertIsNone(run_dir, f"非法参数不得创建运行: {extra}")

    def test_repeated_package_processed_once(self):
        code, run_dir = self.run_cmd(
            ["--package", "pkgone", "--package", "pkgone", "--no-model",
             "--store", self.store])
        self.assertEqual(code, EXIT_OK)
        obs_dir = os.path.join(run_dir, "observations")
        self.assertEqual(len(os.listdir(obs_dir)), 4,
                         "pkgone 恰有 4 个候选，重复 --package 不得重复观测")

    def test_model_budget_and_timeout_passed_to_qwen(self):
        captured = {}

        def http(base, path, method, payload, timeout):
            if path == "/v1/systemone":
                return 404, '"Unexpected endpoint or method"'
            captured.update(payload=payload, timeout=timeout)
            return 200, json.dumps({
                "choices": [{"message": {"content": json.dumps(VALID_ANSWER)},
                             "finish_reason": "stop"}]})
        code, run_dir = self.run_cmd(
            ["--package", "pkgone", "--store", self.store,
             "--model-max-tokens", "4096", "--model-timeout", "30"],
            fetcher=StubFetcher(), http=http)
        self.assertEqual(code, 1)  # laya 404 → 模型错误可见
        self.assertEqual(captured["payload"]["max_tokens"], 4096)
        self.assertEqual(captured["timeout"], 30.0)
        with open(os.path.join(run_dir, "models", "qwen-pkgone.json"),
                  encoding="utf-8") as fh:
            record = json.load(fh)
        self.assertEqual(record["extra"]["max_tokens"], 4096)
        self.assertEqual(record["extra"]["timeout_s"], 30.0)

    def test_offline_run_reads_facts_cache_and_abstains(self):
        cache_dir = os.path.join(self.tmp.name, "facts-cache-offline")
        # 先在线跑一次（--facts-cache 写缓存；--no-model 保证确定性）
        code, run_dir = self.run_cmd(
            ["--package", "pkgone", "--no-model", "--facts-cache", cache_dir,
             "--store", self.store])
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(os.path.isfile(os.path.join(cache_dir,
                                                    "pkgone.json")))
        # 再离线：事实只读缓存、观测 ABSTAIN、不触网（假 runner 会因
        # offline 不被调用而保持零调用）
        runner = FakeUddRunner()
        code, run_dir = self.run_cmd(
            ["--offline", "--package", "pkgone", "--no-model",
             "--facts-cache", cache_dir, "--store", self.store],
            runner=runner)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(runner.calls, [], "offline 不得发起任何 UDD 查询")
        with open(os.path.join(run_dir, "run.json"),
                  encoding="utf-8") as fh:
            summary = json.load(fh)
        self.assertTrue(summary["mode"]["offline"])
        self.assertEqual(summary["model_calls"], [])
        self.assertTrue(summary["counts"]["observations"] > 0)
        self.assertTrue(all(o == "ABSTAIN"
                            for o in summary["counts"]["by_outcome"]))
        prov = summary["input_provenance"]
        self.assertEqual(prov["kind"], "udd-cache")
        self.assertEqual(prov["packages"]["pkgone"]["kind"], "udd-cache")
        self.assertIn("original_fetched_at",
                      prov["packages"]["pkgone"]["cache"])

    def test_offline_without_cache_is_usage_error(self):
        code, run_dir = self.run_cmd(
            ["--offline", "--package", "pkgone", "--no-model",
             "--store", self.store])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIsNone(run_dir)

    def test_offline_cache_miss_is_fatal_no_run_dir(self):
        cache_dir = os.path.join(self.tmp.name, "facts-cache-miss")
        code, run_dir = self.run_cmd(
            ["--offline", "--package", "pkgone", "--no-model",
             "--facts-cache", cache_dir, "--store", self.store])
        self.assertEqual(code, EXIT_FATAL)
        self.assertIsNone(run_dir, "缓存未命中不得创建运行目录")

    def test_no_model_run_uses_fetcher_without_models(self):
        fetcher = StubFetcher()
        code, _run_dir = self.run_cmd(["--no-model", "--package", "pkgone",
                                       "--store", self.store], fetcher=fetcher)
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(fetcher.policy is not None)

    def test_laya_error_falls_back_to_qwen_and_exit_signals_error(self):
        def http(base, path, method, payload, timeout):
            if path == "/v1/systemone":
                return 404, '"Unexpected endpoint or method"'
            return 200, json.dumps({
                "choices": [{"message": {"content": json.dumps(VALID_ANSWER)},
                             "finish_reason": "stop"}]})
        code, run_dir = self.run_cmd(["--package", "pkgone",
                                      "--store", self.store],
                                     fetcher=StubFetcher(), http=http)
        self.assertEqual(code, 1)  # 完成但存在模型错误，须显式非零
        with open(os.path.join(run_dir, "run.json"),
                  encoding="utf-8") as fh:
            summary = json.load(fh)
        kinds = [(c["kind"], c["error"] is not None) for c in summary["model_calls"]]
        self.assertIn(("laya-triage", True), kinds)
        self.assertIn(("qwen-analysis", False), kinds)
        laya_file = os.path.join(run_dir, "models", "laya-pkgone.json")
        with open(laya_file, encoding="utf-8") as fh:
            laya_record = json.load(fh)
        self.assertIn("Unexpected endpoint", laya_record["response_body_raw"])
        self.assertIn("fallback", laya_record["extra"]["fallback_note"])
        with open(os.path.join(run_dir, "report.md"),
                  encoding="utf-8") as fh:
            report = fh.read()
        self.assertIn("laya-pkgone", report)
        self.assertIn("模型错误", report)

    def test_qwen_invalid_citation_exposed_not_silent(self):
        bad_answer = json.loads(json.dumps(VALID_ANSWER))
        bad_answer["proposals"][0]["evidence_obs_ids"] = ["obs-4242"]

        def http(base, path, method, payload, timeout):
            if path == "/v1/systemone":
                return 404, '"Unexpected endpoint or method"'
            return 200, json.dumps({
                "choices": [{"message": {"content": json.dumps(bad_answer)},
                             "finish_reason": "stop"}]})
        code, run_dir = self.run_cmd(["--package", "pkgone",
                                      "--store", self.store],
                                     fetcher=StubFetcher(), http=http)
        self.assertEqual(code, 1)
        with open(os.path.join(run_dir, "run.json"),
                  encoding="utf-8") as fh:
            summary = json.load(fh)
        qwen = [c for c in summary["model_calls"] if c["kind"] == "qwen-analysis"][0]
        self.assertEqual(qwen["error"]["kind"], "schema-invalid")

    def test_manual_url_bound_to_package(self):
        fetcher = StubFetcher()
        code, run_dir = self.run_cmd(["--package", "pkgtwo", "--no-model",
                                      "--url", "https://manual.example/pkgtwo/",
                                      "--store", self.store], fetcher=fetcher)
        self.assertEqual(code, EXIT_OK)
        obs_dir = os.path.join(run_dir, "observations")
        manual = []
        for name in os.listdir(obs_dir):
            with open(os.path.join(obs_dir, name), encoding="utf-8") as fh:
                rec = json.load(fh)
            if rec["endpoint"]["label"] == "manual-candidate":
                manual.append(rec)
        self.assertEqual(len(manual), 1)
        self.assertEqual(manual[0]["endpoint"]["origin"],
                         "pkgtwo:operator:--url")
        self.assertEqual(manual[0]["package"], "pkgtwo")

    def test_transport_error_exit_code(self):
        class ErrorFetcher(StubFetcher):
            def fetch(self, url):
                return FetchOutcome(ok=False,
                                    error={"kind": "timeout", "message": "t"})
        code, _run_dir = self.run_cmd(["--no-model", "--package", "pkgone",
                                       "--store", self.store],
                                      fetcher=ErrorFetcher())
        self.assertEqual(code, 1)

    def test_live_facts_failure_is_fatal_without_run_dir(self):
        # UDD 连接失败 = 输入事实不可得：致命错误、不创建运行目录、不编造
        runner = FakeUddRunner(
            error=("connection",
                   b"psql: error: could not connect to server: "
                   b"Connection refused"))
        code, run_dir = self.run_cmd(["--package", "pkgone", "--no-model",
                                      "--store", self.store], runner=runner)
        self.assertEqual(code, EXIT_FATAL)
        self.assertIsNone(run_dir)

    def test_run_rejects_invalid_package_names_as_fatal(self):
        # 非法包名在 validate_source 即被拒：致命、无运行目录、不触网
        runner = FakeUddRunner()
        code, run_dir = self.run_cmd(["--package", "Bad Name!",
                                      "--no-model", "--store", self.store],
                                     runner=runner)
        self.assertEqual(code, EXIT_FATAL)
        self.assertIsNone(run_dir)
        self.assertEqual(runner.calls, [])

    def test_run_report_uses_live_udd_provenance(self):
        code, run_dir = self.run_cmd(
            ["--package", "pkgone", "--no-model", "--store", self.store])
        self.assertEqual(code, EXIT_OK)
        with open(os.path.join(run_dir, "run.json"), encoding="utf-8") as fh:
            summary = json.load(fh)
        prov = summary["input_provenance"]
        self.assertEqual(prov["kind"], "udd-live")
        self.assertIn("udd-mirror.debian.net", prov["dsn_display"])
        self.assertEqual(prov["release"], "sid")
        self.assertIn("Debian QA Group", prov["qa_filter"])
        pkg_prov = prov["packages"]["pkgone"]
        self.assertEqual(pkg_prov["template_version"],
                         "udd-fetch-templates-v2")
        self.assertTrue(pkg_prov["response_sha256"])
        self.assertTrue(pkg_prov["fetched_at"])
        self.assertEqual(prov["selection"]["mode"], "named-fast-path")
        with open(os.path.join(run_dir, "report.md"), encoding="utf-8") as fh:
            report = fh.read()
        self.assertIn("udd-live", report)
        self.assertIn("归档快照，不是本次运行的输入", report)

    def test_run_named_fast_path_never_enumerates_cohort(self):
        runner = FakeUddRunner()
        code, _ = self.run_cmd(["--package", "pkgone", "--no-model",
                                "--store", self.store], runner=runner)
        self.assertEqual(code, EXIT_OK)
        scripts = [c["stdin"].decode() for c in runner.calls]
        self.assertEqual(len(scripts), 1, "单包快速路径恰一次 facts 查询")
        self.assertFalse(any("'cohort' AS grp" in s for s in scripts),
                         "快速路径不得枚举 cohort")

    def test_run_cohort_limit_selects_packages(self):
        runner = FakeUddRunner(cohort_names=("pkgone", "pkgthree"))
        code, run_dir = self.run_cmd(["--limit", "2", "--no-model",
                                      "--store", self.store], runner=runner)
        self.assertEqual(code, EXIT_OK)
        with open(os.path.join(run_dir, "run.json"), encoding="utf-8") as fh:
            summary = json.load(fh)
        names = sorted(p["name"] for p in summary["packages"])
        self.assertEqual(names, ["pkgone", "pkgthree"])
        self.assertEqual(summary["input_provenance"]["selection"]["mode"],
                         "cohort")
        # 一次 cohort + 两次 facts
        self.assertEqual(len(runner.calls), 3)

    def test_run_cohort_limit_bounded(self):
        code, run_dir = self.run_cmd(["--limit", "101", "--no-model",
                                      "--store", self.store])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIsNone(run_dir)

    def test_bad_model_base_fatal_before_run_dir(self):
        # 畸形 base（无 host / 端口越界）= 模型配置致命错误（exit 3），
        # 必须在 create_run 之前拒绝：不得留下缺 run.json 的运行目录
        for flag in ("--model-base", "--laya-base"):
            for bad in ("127.0.0.1:1234/v1", "http://127.0.0.1:99999"):
                runner = FakeUddRunner()
                code, run_dir = self.run_cmd(
                    [flag, bad, "--package", "pkgone",
                     "--store", self.store], runner=runner)
                self.assertEqual(code, EXIT_FATAL, (flag, bad))
                self.assertIsNone(run_dir, (flag, bad))
                self.assertEqual(runner.calls, [], (flag, bad))

    def test_bad_model_base_not_rejected_when_no_model(self):
        # --no-model 不触模型：base 不被使用，无需拦截
        code, _run_dir = self.run_cmd(
            ["--model-base", "127.0.0.1:1234/v1", "--no-model",
             "--package", "pkgone", "--store", self.store])
        self.assertEqual(code, EXIT_OK)


class CliMiscTest(unittest.TestCase):
    def test_missing_command_exits_two(self):
        with self.assertRaises(SystemExit) as ctx:
            main([])
        self.assertEqual(ctx.exception.code, 2)

    def test_run_help_has_budget_flags_and_no_stale_laya_claim(self):
        parser = build_parser()
        run_help = ""
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                run_help = action.choices["run"].format_help()
        self.assertIn("--model-max-tokens", run_help)
        self.assertIn("--model-timeout", run_help)
        self.assertIn("--facts-cache", run_help)
        # Laya 接口实测可用与否随版本变化；帮助文案不得写死"当前不可用"
        self.assertNotIn("不可用", run_help)

    def test_inspect_json_writes_package_facts(self):
        buf = io.StringIO()
        with patch_udd_client(FakeUddRunner()):
            with mock.patch("sys.stdout", buf):
                code = main(["inspect", "--package", "pkgone", "--json"])
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["packages"][0]["name"], "pkgone")
        prov = payload["provenance"]
        self.assertEqual(prov["kind"], "udd-live")
        self.assertEqual(prov["release"], "sid")
        self.assertEqual(prov["selection"]["mode"], "named-fast-path")
        view = payload["packages"][0]
        labels = {(c["url"], c["label"], c["fetch"])
                  for c in view["candidates"]}
        self.assertIn(("https://example.org/pkgone/",
                       "homepage-candidate", True), labels)
        self.assertIn(("https://git.example.org/pkgone.git",
                       "dep12-repository", False), labels)
        self.assertIn(("https://git.example.org/pkgone",
                       "dep12-repository-browse", True), labels)
        self.assertIn(("https://example.org/pkgone/pkgone-1.1.tar.gz",
                       "upstream-artifact", False), labels)

    def test_inspect_limit_must_be_positive_and_bounded(self):
        with patch_udd_client(FakeUddRunner()):
            self.assertEqual(main(["inspect", "--limit", "0"]),
                             EXIT_USAGE)
            self.assertEqual(main(["inspect", "--limit", "101"]),
                             EXIT_USAGE)

    def test_inspect_repeated_package_deduped(self):
        runner = FakeUddRunner()
        buf = io.StringIO()
        with patch_udd_client(runner):
            with mock.patch("sys.stdout", buf):
                code = main(["inspect", "--package", "pkgone",
                             "--package", "pkgone", "--json"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(runner.calls), 1, "重复 --package 去重后单查询")
        payload = json.loads(buf.getvalue())
        self.assertEqual(len(payload["packages"]), 1)

    def test_inspect_cohort_limit_and_truncation_note(self):
        runner = FakeUddRunner(cohort_names=("pkgone", "pkgthree"),
                               cohort_more_names=("zzz-extra",))
        buf = io.StringIO()
        with patch_udd_client(runner):
            with mock.patch("sys.stdout", buf):
                code = main(["inspect", "--limit", "2"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("more available", buf.getvalue())
        payload_scripts = [c["stdin"].decode() for c in runner.calls]
        self.assertTrue(payload_scripts[0].startswith("BEGIN;"))
        self.assertIn("'cohort' AS grp", payload_scripts[0])

    def test_active_commands_reject_raw_flag(self):
        # --raw 已移除：活跃命令不再接受任何 root raw 输入
        for argv in (["inspect", "--raw", "raw"],
                     ["run", "--raw", "raw"],
                     ["investigate", "--package", "x", "--raw", "raw"]):
            with self.assertRaises(SystemExit) as ctx:
                build_parser().parse_args(argv)
            self.assertEqual(ctx.exception.code, 2, argv)

    def test_active_commands_never_load_root_raw_snapshot(self):
        # 行为级保证：即使 raw/ 存在且 load_snapshot 被人为替换成炸弹，
        # 活跃命令路径也绝不触碰它（inspect/run 全流程）
        def bomb(*_args, **_kwargs):
            raise AssertionError("active CLI must not read the root raw/ "
                                 "snapshot")

        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeUddRunner()
            with mock.patch("gleam.snapshot.load_snapshot",
                            side_effect=bomb):
                with patch_udd_client(runner):
                    with mock.patch("gleam.cli.Fetcher",
                                    return_value=StubFetcher()):
                        with mock.patch(
                                "gleam.models._http_json",
                                side_effect=OSError(
                                    "no model in unit test")):
                            self.assertEqual(
                                main(["inspect", "--package", "pkgone"]),
                                EXIT_OK)
                            self.assertEqual(
                                main(["run", "--package", "pkgone",
                                      "--no-model", "--store",
                                      os.path.join(tmp, "s2")]),
                                EXIT_OK)

    def test_check_empty_store_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = main(["check", "--store", tmp])
            self.assertEqual(code, EXIT_OK)

    def test_models_command_reports_endpoint_error(self):
        with mock.patch("gleam.models._http_json",
                        side_effect=OSError("refused")):
            code = main(["models", "--model-base", "http://127.0.0.1:1"])
        self.assertEqual(code, EXIT_FATAL)

    def test_models_command_lists(self):
        body = json.dumps({"data": [{"id": "qwen3.8-27b"}, {"id": "laya"}]})
        buf = io.StringIO()
        with mock.patch("gleam.models._http_json", return_value=(200, body)):
            with mock.patch("sys.stdout", buf):
                code = main(["models"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("qwen3.8-27b", buf.getvalue())
        self.assertIn("laya", buf.getvalue())

    def test_models_command_rejects_malformed_base_cleanly(self):
        # 无 scheme（→无 host）与端口越界：干净 fatal 报错（exit 3），
        # 无 traceback、不发请求
        for bad in ("127.0.0.1:1234/v1", "http://127.0.0.1:99999"):
            err = io.StringIO()
            with mock.patch("gleam.models._http_json") as http:
                with mock.patch("sys.stderr", err):
                    code = main(["models", "--model-base", bad])
            self.assertEqual(code, EXIT_FATAL, bad)
            self.assertIn("--model-base", err.getvalue(), bad)
            self.assertNotIn("Traceback", err.getvalue(), bad)
            http.assert_not_called()

    def test_investigate_rejects_malformed_model_base_before_run_dir(self):
        # investigate 的 model_base 进入模型循环之前（create_run 之前）拒绝
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                code = main(["investigate", "--package", "pkgone",
                             "--model-base", "127.0.0.1:1234/v1",
                             "--store", tmp])
            self.assertEqual(code, EXIT_FATAL)
            self.assertIn("--model-base", err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())
            self.assertFalse(os.path.isdir(os.path.join(tmp, "runs")),
                             "模型配置致命错误不得创建运行目录")


class FetchUddCliTest(unittest.TestCase):
    """fetch-udd CLI：--list-datasets（无 run 目录）/ --datasets（v3）/
    legacy --groups（v2，默认值不变）。全部走 FakeUddRunner，不触网。"""

    def read_summary(self, tmp):
        store = RunStore(tmp)
        run_id = store.list_runs()[0]
        with open(os.path.join(store.runs_root, run_id, "run.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)

    def test_list_datasets_prints_catalog_without_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with patch_udd_client(FakeUddRunner()):
                with mock.patch("sys.stdout", buf):
                    code = main(["fetch-udd", "--list-datasets",
                                 "--store", tmp])
            self.assertEqual(code, EXIT_OK)
            out = buf.getvalue()
            self.assertIn("UDD datasets for fetch_udd", out)
            for name in ("sources", "lintian", "upload_history_closes",
                         "testing_autoremovals"):
                self.assertIn(f"**{name}**", out)
            self.assertFalse(os.path.isdir(os.path.join(tmp, "runs")),
                             "目录命令不得创建运行目录")

    def test_datasets_mode_uses_template_v3_and_datasets_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeUddRunner()
            with patch_udd_client(runner):
                code = main(["fetch-udd", "--source", "pkgone",
                             "--datasets", "sources,upstream_metadata",
                             "--store", tmp])
            self.assertEqual(code, EXIT_OK)
            summary = self.read_summary(tmp)
            self.assertEqual(summary["mode"]["template_version"],
                             "udd-fetch-templates-v3")
            self.assertEqual(summary["mode"]["datasets"],
                             ["sources", "upstream_metadata"])
            self.assertNotIn("groups", summary["mode"])
            self.assertEqual(summary["counts"]["datasets"], 2)
            self.assertNotIn("groups", summary["counts"])
            gd = summary["groups_data"]
            self.assertEqual(gd["sources"]["row_count"], 1)
            self.assertEqual(gd["upstream_metadata"]["row_count"], 2)
            self.assertEqual(gd["sources"]["rows"][0]["provenance"],
                             "udd:sources#row=1")

    def test_legacy_groups_mode_still_template_v2(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch_udd_client(FakeUddRunner()):
                code = main(["fetch-udd", "--source", "pkgone",
                             "--groups", "source,upstream,metadata,packaging",
                             "--store", tmp])
            self.assertEqual(code, EXIT_OK)
            summary = self.read_summary(tmp)
            self.assertEqual(summary["mode"]["template_version"],
                             "udd-fetch-templates-v2")
            self.assertEqual(summary["mode"]["groups"],
                             ["source", "upstream", "metadata", "packaging"])
            self.assertNotIn("datasets", summary["mode"])
            self.assertEqual(summary["counts"]["groups"], 4)

    def test_neither_datasets_nor_groups_defaults_to_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch_udd_client(FakeUddRunner()):
                code = main(["fetch-udd", "--source", "pkgone",
                             "--store", tmp])
            self.assertEqual(code, EXIT_OK)
            summary = self.read_summary(tmp)
            self.assertEqual(summary["mode"]["template_version"],
                             "udd-fetch-templates-v2")
            self.assertEqual(summary["mode"]["groups"],
                             list(udd_mod.DEFAULT_GROUPS))

    def test_datasets_and_groups_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeUddRunner()
            with patch_udd_client(runner):
                code = main(["fetch-udd", "--source", "pkgone",
                             "--datasets", "sources", "--groups", "source",
                             "--store", tmp])
            self.assertEqual(code, EXIT_USAGE)
            self.assertEqual(runner.calls, [])
            self.assertFalse(os.path.isdir(os.path.join(tmp, "runs")))

    def test_datasets_over_budget_rejected_before_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeUddRunner()
            with patch_udd_client(runner):
                code = main([
                    "fetch-udd", "--source", "pkgone", "--datasets",
                    "sources,upstream,upstream_metadata,vcswatch,urls,"
                    "binaries,bugs", "--store", tmp])
            self.assertEqual(code, EXIT_USAGE)
            self.assertEqual(runner.calls, [])
            self.assertFalse(os.path.isdir(os.path.join(tmp, "runs")))
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeUddRunner()
            with patch_udd_client(runner):
                code = main([
                    "fetch-udd", "--source", "pkgone", "--datasets",
                    "sources,upstream,upstream_metadata,vcswatch,urls,"
                    "binaries", "--limit", "21", "--store", tmp])
            self.assertEqual(code, EXIT_USAGE)
            self.assertEqual(runner.calls, [])
            self.assertFalse(os.path.isdir(os.path.join(tmp, "runs")))

    def test_source_required_unless_list_datasets(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeUddRunner()
            with patch_udd_client(runner):
                code = main(["fetch-udd", "--store", tmp])
            self.assertEqual(code, EXIT_USAGE)
            self.assertEqual(runner.calls, [])
            self.assertFalse(os.path.isdir(os.path.join(tmp, "runs")))

    def test_list_datasets_with_both_selectors_is_usage_error(self):
        # --list-datasets 不得短路用法互斥：--datasets 与 --groups 同传
        # 仍 exit 2、不打印目录
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeUddRunner()
            buf, err = io.StringIO(), io.StringIO()
            with patch_udd_client(runner):
                with mock.patch("sys.stdout", buf), \
                        mock.patch("sys.stderr", err):
                    code = main(["fetch-udd", "--list-datasets",
                                 "--datasets", "sources", "--groups",
                                 "source", "--store", tmp])
            self.assertEqual(code, EXIT_USAGE)
            self.assertEqual(buf.getvalue(), "")
            self.assertIn("互斥", err.getvalue())
            self.assertEqual(runner.calls, [])
            self.assertFalse(os.path.isdir(os.path.join(tmp, "runs")))
            # 单独 --list-datasets 仍正常：纯函数输出、无运行目录
            buf2 = io.StringIO()
            with patch_udd_client(FakeUddRunner()):
                with mock.patch("sys.stdout", buf2):
                    code = main(["fetch-udd", "--list-datasets",
                                 "--store", tmp])
            self.assertEqual(code, EXIT_OK)
            self.assertIn("UDD datasets for fetch_udd", buf2.getvalue())
            self.assertFalse(os.path.isdir(os.path.join(tmp, "runs")))

    def test_absolute_nonexistent_psql_rejected_pre_run(self):
        # 绝对路径的 psql 不再绕过预检：不存在即 exit 3、无运行目录
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeUddRunner()
            err = io.StringIO()
            with patch_udd_client(runner):
                with mock.patch("sys.stderr", err):
                    code = main(["fetch-udd", "--source", "pkgone",
                                 "--udd-psql", "/nonexistent/abs/psql",
                                 "--store", tmp])
            self.assertEqual(code, EXIT_FATAL)
            self.assertIn("psql 不可用", err.getvalue())
            self.assertEqual(runner.calls, [])
            self.assertFalse(os.path.isdir(os.path.join(tmp, "runs")))


if __name__ == "__main__":
    unittest.main()
