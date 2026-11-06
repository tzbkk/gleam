"""UDD 实时抓取（fetch_udd）测试：合成 CSV + 假 runner / 真本地子进程，
全部离线（不连 UDD 镜像）。

覆盖：参数校验（包名/groups/release/limit/DSN 允许清单）、固定模板脚本
构造、多语句 CSV 解析（多行字段/空组/meta）、逐表多行保留、空 bugs/
空 sources 语义、bugs 关联出处（via_* 布尔、package≠source 原样保留）、
limit+1 截断探测、psql 错误分级（连接失败/SQL 错误/超时/输出超限/psql
缺失）、有界子进程读取器（真子进程）、候选 URL 抽取、CLI 落盘与 check、
investigate 工具集成（限定包名/禁用如实报告/错误如实/证据 URL 注册）。
"""

import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

from gleam import udd
from gleam.agent import InvestigationContext, TOOL_SPECS
from gleam.cli import EXIT_OK, EXIT_USAGE, build_parser, cmd_fetch_udd
from gleam.net import FetchOutcome
from gleam.store import RunStore, verify_store

from .fixtures import csv_text


def make_client(runner, **policy):
    merged = dict(udd.DEFAULT_POLICY)
    merged.update(policy)
    return udd.UddClient(runner=runner, policy=merged)


class RecordingRunner:
    """假 psql：记录 argv/stdin/env，回放固定 (stdout, stderr, code)。"""

    def __init__(self, stdout=b"", stderr=b"", code=0, kill_kind=None):
        self.calls = []
        self.stdout, self.stderr = stdout, stderr
        self.code, self.kill_kind = code, kill_kind

    def __call__(self, argv, stdin_bytes, *, timeout_s,
                 max_output_bytes, env=None):
        self.calls.append({"argv": argv, "stdin": stdin_bytes,
                           "timeout_s": timeout_s,
                           "max_output_bytes": max_output_bytes,
                           "env": env})
        return self.stdout, self.stderr, self.code, self.kill_kind


# ---- 校验与模板 -------------------------------------------------------------

class ValidationTest(unittest.TestCase):
    def test_source_policy(self):
        self.assertEqual(udd.validate_source("librcd"), "librcd")
        self.assertEqual(udd.validate_source("0ad"), "0ad")
        self.assertEqual(udd.validate_source("c++-helper"), "c++-helper")
        for bad in ("Librcd", "-lib", ".x", "a b", "a;b", "", None, 42,
                    "lib'--", 'x"y', "../etc", "a:b", True,
                    "x" * 65, "src:librcd"):
            with self.assertRaises(udd.UddError, msg=repr(bad)):
                udd.validate_source(bad)

    def test_groups_allowlist(self):
        self.assertEqual(udd.validate_groups(None), udd.DEFAULT_GROUPS)
        self.assertEqual(udd.validate_groups("source,bugs"), ("source",
                                                              "bugs"))
        self.assertEqual(udd.validate_groups(["bugs", "source"]),
                         ("bugs", "source"))
        self.assertEqual(udd.validate_groups(["bugs", "bugs"]), ("bugs",))
        for bad in ("drop", ["all"], "source;DROP", 5, ["source", "evil"]):
            with self.assertRaises(udd.UddError):
                udd.validate_groups(bad)

    def test_groups_none_default_but_explicit_empty_rejected(self):
        # None=默认五组；显式空选择（列表/元组/空串/纯分隔符）拒绝——
        # 否则只会得到 meta-only 脚本
        self.assertEqual(udd.validate_groups(None), udd.DEFAULT_GROUPS)
        for empty in ([], (), "", " , "):
            with self.assertRaises(udd.UddError) as ctx:
                udd.validate_groups(empty)
            self.assertEqual(ctx.exception.kind, "argument-invalid")
        runner = RecordingRunner(stdout=b"")
        with self.assertRaises(udd.UddError) as ctx:
            make_client(runner).fetch("pkgone", groups=[])
        self.assertEqual(ctx.exception.kind, "argument-invalid")
        self.assertEqual(runner.calls, [], "空 groups 在 psql 之前拒绝")

    def test_release_allowlist(self):
        self.assertIsNone(udd.validate_release(None))
        self.assertIsNone(udd.validate_release(""))
        for good in ("sid", "trixie", "bookworm", "experimental", "stable"):
            self.assertEqual(udd.validate_release(good), good)
        for bad in ("sid; DROP", "custom", 1, "SID"):
            with self.assertRaises(udd.UddError):
                udd.validate_release(bad)

    def test_limit_bounds(self):
        self.assertEqual(udd.validate_limit(30), 30)
        for bad in (0, -1, 101, "30", 1.5, True, None):
            with self.assertRaises(udd.UddError):
                udd.validate_limit(bad)

    def test_limit_hard_zero_is_a_bound_not_absent(self):
        # hard=0 是显式上限而非"缺省"（hard or default 会把 0 误当 100）
        with self.assertRaises(udd.UddError):
            udd.validate_limit(5, hard=0)
        self.assertEqual(udd.validate_limit(2, hard=3), 2)
        self.assertEqual(udd.validate_limit(100), 100)  # 无 hard=默认上限

    def test_dsn_form(self):
        self.assertEqual(udd.validate_dsn(udd.DEFAULT_UDD_DSN),
                         udd.DEFAULT_UDD_DSN)
        for bad in ("mysql://x", "psql y", "postgresql://a b/c", 5):
            with self.assertRaises(udd.UddError):
                udd.validate_dsn(bad)
        self.assertEqual(udd.dsn_display(udd.DEFAULT_UDD_DSN),
                         "postgresql://udd-mirror.debian.net/udd")

    def test_script_is_single_readonly_transaction(self):
        script = udd.build_script(("source", "bugs"), "sid")
        self.assertTrue(script.startswith("BEGIN;"))
        self.assertTrue(script.rstrip().endswith("COMMIT;"))
        lowered = script.lower()
        for forbidden in ("insert into", "update ", "delete from",
                          "create table", "drop ", "alter ", "grant ",
                          "copy "):
            self.assertNotIn(forbidden, lowered)
        statement_count = script.count(";")
        # meta + source + binaries + bugs = 4 条语句（BEGIN/COMMIT 各 1）
        self.assertEqual(statement_count, 4 + 2)
        # 输入只经 psql 变量进入（模板里没有 Python 侧插值占位）
        self.assertIn(":'source'", script)
        self.assertIn(":limit1", script)

    def test_query_cap(self):
        runner = RecordingRunner(stdout=csv_text(
            (["grp", "server_version", "server_now"],
             [["meta", "PostgreSQL 17.5", "2026-11-02 00:00:00"]])))
        client = make_client(runner, max_queries=3)
        with self.assertRaises(udd.UddError) as ctx:
            client.fetch("librcd", groups=udd.GROUPS)
        self.assertEqual(ctx.exception.kind, "budget-exhausted")
        self.assertEqual(runner.calls, [])  # 超预算在发进程之前拒绝


# ---- 解析：多行/多行组/空组 ---------------------------------------------------

META_HEADER = ["grp", "server_version", "server_now"]
SOURCE_HEADER = ["grp", "source", "version", "release", "homepage", "bin",
                 "vcs_type", "vcs_url", "section", "priority",
                 "build_depends"]
UPSTREAM_HEADER = ["grp", "source", "version", "release", "status",
                   "upstream_version", "upstream_url", "watch_file",
                   "errors", "last_check"]
METADATA_HEADER = ["grp", "source", "key", "value"]
PACKAGING_HEADER = ["grp", "source", "version", "vcs", "url", "browser",
                    "status", "last_scan", "changelog_distribution",
                    "commit_id"]
BINARIES_HEADER = ["grp", "package", "release"]
BUGS_HEADER = ["grp", "id", "package", "source", "status", "severity",
               "title", "arrival", "last_modified", "done", "tags",
               "via_source_mapping", "via_bugs_packages"]
WNPP_HEADER = ["grp", "id", "type", "source", "title"]
HISTORY_HEADER = ["grp", "version", "date", "changed_by"]
COHORT_HEADER = ["grp", "source", "version", "release", "maintainer"]

FULL_STDOUT = csv_text(
    (META_HEADER,
     [["meta", "PostgreSQL 17.5 (Debian)", "2026-11-02 05:40:17.028825+00"]]),
    (SOURCE_HEADER,
     [["source", "cdrkit", "9:1.1.11-5", "sid", "", "wodim, genisoimage",
       "", "", "otherosfs", "optional", ""],
      ["source", "cdrkit", "9:1.1.11-5", "trixie", "", "wodim, genisoimage",
       "", "", "otherosfs", "optional", ""]]),
    (UPSTREAM_HEADER,
     [["upstream", "cdrkit", "9:1.1.11-5", "sid", "", "", "", "",
       "version=4\nhttps://example.org/cdrkit/cdrkit-(\\d)\\.tar\\.gz\n",
       "", ""]]),
    (METADATA_HEADER,
     [["metadata", "cdrkit", "Repository",
       "https://salsa.debian.org/debian/cdrkit.git"],
      ["metadata", "cdrkit", "Repository-Browse",
       "multiline value\nsecond line, with comma"]]),
    (PACKAGING_HEADER,
     [["packaging", "cdrkit", "9:1.1.11-5", "Git",
       "https://salsa.debian.org/debian/cdrkit.git",
       "https://salsa.debian.org/debian/cdrkit", "ok",
       "2026-10-30 04:00:53.123+00", "unstable", "abc123"]]),
    (BINARIES_HEADER,
     [["binaries", "wodim", "sid"], ["binaries", "genisoimage", "sid"],
      ["binaries", "wodim", "trixie"]]),
    (BUGS_HEADER,
     [["bugs", "199352", "cdrkit-doc", "cdrkit", "pending", "wishlist",
       "cdrecord lacks of a standard, convenient \"documentation\"",
       "2003-06-30 09:18:06", "2006-09-17 17:18:23", "", "moreinfo",
       "true", "true"],
      ["bugs", "267233", "wodim", "cdrkit", "pending", "wishlist",
       "Please add -raw support for CUE/BIN",
       "2004-08-21 10:03:03", "2006-12-18 18:48:28", "", "", "true",
       "false"],
      ["bugs", "999999", "genisoimage", "", "pending", "normal",
       "binary-only bug without source mapping",
       "2020-01-01 00:00:00", "2020-01-02 00:00:00", "", "d-i", "false",
       "false"]]),
    (WNPP_HEADER,
     [["wnpp", "974914", "ITA", "cdrkit",
       "ITA: cdrkit -- collection of programs"]]),
    (HISTORY_HEADER,
     [["history", "9:1.1.11-5", "2025-09-28 21:18:58+00",
       "An Example <dev@example.org>"]]),
)


class FetchParseTest(unittest.TestCase):
    def test_full_fetch_multigroup_multiline(self):
        runner = RecordingRunner(stdout=FULL_STDOUT)
        client = make_client(runner)
        result = client.fetch("cdrkit", groups=udd.GROUPS, limit=50)
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["error"])
        self.assertEqual(result["query_count"], 9)
        gd = result["groups_data"]
        # 逐 release 多行保留（不按 source 去重覆盖）
        self.assertEqual(gd["source"]["row_count"], 2)
        self.assertEqual([r["release"] for r in gd["source"]["rows"]],
                         ["sid", "trixie"])
        # 多行字段（引号内换行 + 逗号）经 csv 透明还原
        metadata_rows = gd["metadata"]["rows"]
        self.assertEqual(metadata_rows[1]["value"],
                         "multiline value\nsecond line, with comma")
        # bugs：引号内逗号标题、via_* 布尔转换、package≠source 原样保留
        bugs = gd["bugs"]["rows"]
        self.assertEqual(bugs[0]["title"],
                         'cdrecord lacks of a standard, convenient '
                         '"documentation"')
        self.assertIs(bugs[0]["via_source_mapping"], True)
        self.assertIs(bugs[0]["via_bugs_packages"], True)
        self.assertIs(bugs[1]["via_bugs_packages"], False)
        self.assertEqual(bugs[1]["package"], "wodim")
        self.assertEqual(bugs[1]["source"], "cdrkit")
        self.assertEqual(bugs[2]["source"], "")  # 无源码映射的 binary bug
        self.assertIn("association_note", gd["bugs"])
        # 关联出处的时间戳为服务端渲染原文
        self.assertEqual(bugs[0]["arrival"], "2003-06-30 09:18:06")
        # fetched_at 与数据时间分开
        self.assertNotIn(result["fetched_at"],
                         json.dumps(gd["bugs"]["rows"]))
        # binaries 逐行保留
        self.assertEqual(gd["binaries"]["row_count"], 3)
        self.assertFalse(any(g["more_available"] for g in gd.values()))
        # meta：服务端时钟进 server 字段
        self.assertIn("PostgreSQL 17.5",
                      result["server"]["server_version"])
        # 请求形态：argv 是列表（无 shell）、变量带值、stdin 是脚本
        call = runner.calls[0]
        self.assertTrue(call["argv"][0] in ("psql",))
        flat = call["argv"]
        self.assertIn("-X", flat)
        self.assertIn("--csv", flat)
        self.assertIn("source=cdrkit", flat)
        self.assertIn("release=", flat)
        self.assertIn("limit1=51", flat)
        self.assertIn("ON_ERROR_STOP=1", flat)
        self.assertTrue(all(isinstance(a, str) for a in flat))
        script = call["stdin"].decode()
        self.assertIn("BEGIN;", script)
        self.assertIn("COMMIT;", script)
        # PGOPTIONS：语句超时 + 只读强制
        pgopts = call["env"]["PGOPTIONS"]
        self.assertIn("statement_timeout=15000", pgopts)
        self.assertIn("default_transaction_read_only=on", pgopts)
        # LC_ALL=C 固定：错误分级按英文 stderr 匹配，不受操作员 locale
        # 影响（live QA 复审发现 zh_CN 下连接拒绝误分 query-error）
        self.assertEqual(call["env"]["LC_ALL"], "C")
        # provenance：模板版本、DSN 展示形、fetched_at、脚本经 stdin
        self.assertEqual(result["template_version"],
                         udd.UDD_TEMPLATE_VERSION)
        self.assertEqual(result["dsn_display"],
                         "postgresql://udd-mirror.debian.net/udd")
        self.assertNotIn("udd-mirror:udd-mirror",
                         json.dumps(result["psql_argv_safe_form"]))

    def test_empty_sources_and_empty_bugs_are_ok_no_rows(self):
        stdout = csv_text(
            (META_HEADER,
             [["meta", "PostgreSQL 17.5", "2026-11-02 05:40:17+00"]]),
            (SOURCE_HEADER, []),
            (UPSTREAM_HEADER, []),
            (METADATA_HEADER, []),
            (PACKAGING_HEADER, []),
            (BINARIES_HEADER, []),
            (BUGS_HEADER, []),
        )
        runner = RecordingRunner(stdout=stdout)
        result = make_client(runner).fetch("nosuchpkg-xyz")
        self.assertEqual(result["status"], "ok")
        gd = result["groups_data"]
        for group in ("source", "upstream", "metadata", "packaging",
                      "bugs", "binaries"):
            self.assertEqual(gd[group]["status"], "ok")
            self.assertEqual(gd[group]["row_count"], 0)
            self.assertIn("0 rows returned", gd[group]["note"])
            self.assertFalse(gd[group]["more_available"])

    def test_truncation_more_available(self):
        bugs_rows = [[f"bugs", str(1000 + i), "p", "s", "pending", "minor",
                      f"title {i}", "2020-01-01 00:00:00",
                      "2020-01-01 00:00:00", "", "", "true", "false"]
                     for i in range(31)]
        stdout = csv_text(
            (META_HEADER, [["meta", "x", "now"]]),
            (SOURCE_HEADER, []),
            (UPSTREAM_HEADER, []),
            (METADATA_HEADER, []),
            (PACKAGING_HEADER, []),
            (BINARIES_HEADER, []),
            (BUGS_HEADER, bugs_rows),
        )
        runner = RecordingRunner(stdout=stdout)
        result = make_client(runner).fetch("pkgone", groups=["bugs"],
                                           limit=30)
        bugs = result["groups_data"]["bugs"]
        self.assertEqual(bugs["row_count"], 30)
        self.assertTrue(bugs["more_available"])
        self.assertIn("truncated at 30", bugs["note"])

    def test_release_filter_variable_passed(self):
        runner = RecordingRunner(stdout=csv_text(
            (META_HEADER, [["meta", "x", "now"]])))
        make_client(runner).fetch("pkgone", release="sid")
        self.assertIn("release=sid", runner.calls[0]["argv"])


# ---- 错误分级 -----------------------------------------------------------------

class ErrorPathsTest(unittest.TestCase):
    def test_connection_error(self):
        runner = RecordingRunner(
            stderr=b"psql: error: could not connect to server: "
                   b"Connection refused\n", code=2)
        result = make_client(runner).fetch("pkgone")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["kind"], "connection-error")
        self.assertIn("Connection refused", result["error"]["message"])
        self.assertEqual(result["groups_data"], {})

    def test_query_error_with_stderr(self):
        runner = RecordingRunner(
            stderr=b"psql:<stdin>:3: ERROR:  permission denied",
            code=3)
        result = make_client(runner).fetch("pkgone")
        self.assertEqual(result["error"]["kind"], "query-error")
        self.assertIn("permission denied", result["error"]["message"])

    def test_statement_timeout_classified_as_timeout(self):
        # 服务端 statement_timeout（PGOPTIONS 每语句时限）取消：非零退出 +
        # stderr "canceling statement due to statement timeout" → timeout
        runner = RecordingRunner(
            stderr=b"psql:<stdin>:2: ERROR:  canceling statement due to "
                   b"statement timeout\n", code=3)
        result = make_client(runner).fetch("pkgone")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["kind"], "timeout")
        self.assertIn("canceling statement due to statement timeout",
                      result["error"]["message"])

    def test_timeout_kill(self):
        runner = RecordingRunner(kill_kind="timeout")
        result = make_client(runner).fetch("pkgone")
        self.assertEqual(result["error"]["kind"], "timeout")
        self.assertIn("killed", result["error"]["message"])

    def test_output_too_large_kill(self):
        runner = RecordingRunner(kill_kind="output-too-large")
        result = make_client(runner, max_output_bytes=100).fetch("pkgone")
        self.assertEqual(result["error"]["kind"], "output-too-large")

    def test_psql_missing(self):
        client = udd.UddClient(psql_path="/nonexistent/psql-definitely")
        with self.assertRaises(udd.UddError) as ctx:
            client.fetch("pkgone")
        self.assertEqual(ctx.exception.kind, "psql-unavailable")

    def test_data_before_header_is_parse_error(self):
        runner = RecordingRunner(stdout=b"source,pkgone,1\n")
        result = make_client(runner).fetch("pkgone")
        self.assertEqual(result["error"]["kind"], "parse-error")


# ---- 有界子进程读取器（真本地子进程，无网络）-----------------------------------

class BoundedRunnerTest(unittest.TestCase):
    def test_normal_completion(self):
        out, err, code, kill = udd._run_bounded(
            [sys.executable, "-c", "import sys; "
             "sys.stdout.write('hello'); sys.stderr.write('warn')"],
            b"", timeout_s=10, max_output_bytes=10000)
        self.assertEqual(out, b"hello")
        self.assertEqual(err, b"warn")
        self.assertEqual(code, 0)
        self.assertIsNone(kill)

    def test_timeout_kills(self):
        out, err, code, kill = udd._run_bounded(
            [sys.executable, "-c", "import time; time.sleep(30)"], b"",
            timeout_s=1.0, max_output_bytes=10000)
        self.assertIsNone(code)
        self.assertEqual(kill, "timeout")

    def test_eof_then_hang_still_killed_at_deadline(self):
        # 子进程输出后即关 stdout/stderr 再永久挂起：双管道 EOF 后的
        # wait 仍受墙上时限约束，不得阻塞超过 timeout+slack
        child = ("import sys, time\n"
                 "sys.stdout.write('done')\n"
                 "sys.stdout.close()\n"
                 "sys.stderr.close()\n"
                 "time.sleep(30)\n")
        started = time.monotonic()
        out, err, code, kill = udd._run_bounded(
            [sys.executable, "-c", child], b"",
            timeout_s=1.0, max_output_bytes=10000)
        elapsed = time.monotonic() - started
        self.assertEqual(kill, "timeout")
        self.assertIsNone(code)
        self.assertEqual(out, b"done")  # EOF 前的输出仍完整带回
        self.assertLess(elapsed, 1.0 + 5.0)

    def test_output_cap_kills(self):
        out, err, code, kill = udd._run_bounded(
            [sys.executable, "-c",
             "import sys\nfor _ in range(100): sys.stdout.write('x'*4096)\n"
             "sys.stdout.flush()"], b"",
            timeout_s=10, max_output_bytes=8192)
        self.assertEqual(kill, "output-too-large")
        self.assertLessEqual(len(out), 8192 + 4096)  # 超限即停（允许一块余量）

    def test_stdin_delivered(self):
        out, _, code, _ = udd._run_bounded(
            [sys.executable, "-c", "import sys; "
             "sys.stdout.write(sys.stdin.read().upper())"],
            b"script-body", timeout_s=10, max_output_bytes=10000)
        self.assertEqual(code, 0)
        self.assertEqual(out, b"SCRIPT-BODY")

    def test_spawn_error(self):
        with self.assertRaises(udd.UddError) as ctx:
            udd._run_bounded(["/definitely/not/a/program"], b"",
                             timeout_s=5, max_output_bytes=100)
        self.assertEqual(ctx.exception.kind, "psql-unavailable")


# ---- cohort 枚举（QA maintainer + sid 固定口径） --------------------------------

class CohortTest(unittest.TestCase):
    def cohort_stdout(self, names):
        return csv_text(
            (META_HEADER,
             [["meta", "PostgreSQL 17.5", "2026-11-03 00:00:00+00"]]),
            (COHORT_HEADER,
             [["cohort", n, "1.0-1", "sid", udd.QA_MAINTAINER]
              for n in names]))

    def test_script_is_single_readonly_fixed_template(self):
        script = udd.build_cohort_script()
        self.assertTrue(script.startswith("BEGIN;"))
        self.assertTrue(script.rstrip().endswith("COMMIT;"))
        lowered = script.lower()
        for forbidden in ("insert into", "update ", "delete from",
                          "create table", "drop ", "alter ", "grant ",
                          "copy "):
            self.assertNotIn(forbidden, lowered)
        self.assertEqual(script.count(";"), 2 + 2)  # meta + cohort
        # 确定性行序 + 截断探测 + 应用侧常量经 psql 变量进入
        self.assertIn("ORDER BY source LIMIT :limit1", script)
        self.assertIn("DISTINCT ON (source)", script)
        self.assertIn(":'maintainer'", script)
        self.assertIn(":'release'", script)

    def test_maintainer_is_fixed_constant(self):
        for bad in ("Debian QA Group <packages@qa.debian.org> ",
                    "someone else", "", None, 5,
                    "Debian QA Group <packages@qa.debian.org>'; DROP--"):
            with self.assertRaises(udd.UddError):
                udd.validate_maintainer(bad)
        self.assertEqual(udd.validate_maintainer(udd.QA_MAINTAINER),
                         udd.QA_MAINTAINER)

    def test_cohort_names_order_and_argv(self):
        runner = RecordingRunner(stdout=self.cohort_stdout(
            ["cdrkit", "librcd", "wizznic"]))
        result = make_client(runner).fetch_cohort(limit=3)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["names"], ["cdrkit", "librcd", "wizznic"])
        self.assertEqual(result["row_count"], 3)
        self.assertFalse(result["more_available"])
        self.assertEqual(result["template_version"],
                         udd.COHORT_TEMPLATE_VERSION)
        self.assertTrue(result["response_sha256"])
        call = runner.calls[0]
        flat = call["argv"]
        self.assertIn("maintainer=Debian QA Group "
                      "<packages@qa.debian.org>", flat)
        self.assertIn("release=sid", flat)
        self.assertIn("limit1=4", flat)
        self.assertTrue(all(isinstance(a, str) for a in flat))
        # 安全形态：只读 PGOPTIONS、凭据脱敏
        self.assertIn("default_transaction_read_only=on",
                      call["env"]["PGOPTIONS"])
        self.assertNotIn("udd-mirror:udd-mirror",
                         json.dumps(result["psql_argv_safe_form"]))

    def test_cohort_limit_truncation_probe(self):
        names = [f"pkg{i:02d}" for i in range(4)]
        runner = RecordingRunner(stdout=self.cohort_stdout(names))
        result = make_client(runner).fetch_cohort(limit=3)
        self.assertEqual(result["names"], names[:3])
        self.assertTrue(result["more_available"])
        self.assertEqual(result["hidden_row_count"], 1)
        self.assertIn("truncated", result["note"])

    def test_cohort_limit_strict_integer_bounds(self):
        runner = RecordingRunner(stdout=self.cohort_stdout(["a-pkg"]))
        for bad in (0, -1, 101, "3", 1.5, True):
            with self.assertRaises(udd.UddError):
                make_client(runner).fetch_cohort(limit=bad)
        self.assertEqual(runner.calls, [], "非法 limit 在子进程之前拒绝")

    def test_cohort_bad_release_rejected(self):
        runner = RecordingRunner(stdout=self.cohort_stdout(["a-pkg"]))
        with self.assertRaises(udd.UddError):
            make_client(runner).fetch_cohort(limit=3,
                                             release="sid; DROP")
        self.assertEqual(runner.calls, [])

    def test_cohort_error_kinds_honest(self):
        runner = RecordingRunner(
            stderr=b"psql: error: could not connect to server", code=2)
        result = make_client(runner).fetch_cohort(limit=3)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["kind"], "connection-error")
        self.assertEqual(result["names"], [])

        runner = RecordingRunner(kill_kind="timeout")
        result = make_client(runner).fetch_cohort(limit=3)
        self.assertEqual(result["error"]["kind"], "timeout")

    def test_cohort_empty_is_honest_no_rows(self):
        runner = RecordingRunner(stdout=csv_text(
            (META_HEADER, [["meta", "x", "now"]]),
            (COHORT_HEADER, [])))
        result = make_client(runner).fetch_cohort(limit=3)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["names"], [])
        self.assertIn("real empty result", result["note"])


# ---- datasets= 新路径（template v3）--------------------------------------------

ALL_DATASETS = ("sources", "upstream", "upstream_metadata", "vcswatch",
                "urls", "binaries", "bugs", "bugs_count", "archived_bugs",
                "wnpp", "upload_history", "upload_history_closes",
                "removals", "testing_autoremovals", "popcon_src", "ci",
                "reproducible", "lintian")

DS_SOURCES_HEADER = ["grp", "source", "version", "release", "maintainer",
                     "homepage", "bin", "vcs_type", "vcs_url",
                     "vcs_browser", "section", "priority"]
DS_BUGS_COUNT_HEADER = ["grp", "source", "rc_bugs", "all_bugs"]
DS_REMOVALS_HEADER = ["grp", "batch_id", "name", "version", "arch_array",
                      "time", "ftpmaster", "distribution", "requestor",
                      "reasons"]
DS_LINTIAN_HEADER = ["grp", "package", "tag_type", "package_type",
                     "package_version", "package_arch", "tag",
                     "information"]


class DatasetValidationTest(unittest.TestCase):
    def test_all_18_names_accepted_in_registry_order(self):
        self.assertEqual(udd.DATASET_NAMES, ALL_DATASETS)
        self.assertEqual(len(udd.DATASETS), 18)
        self.assertEqual(udd.validate_datasets(list(ALL_DATASETS)),
                         ALL_DATASETS)
        # 去重保序
        self.assertEqual(udd.validate_datasets(
            ["sources", "bugs", "sources"]), ("sources", "bugs"))

    def test_unknown_dataset_rejected_with_allowed_names(self):
        with self.assertRaises(udd.UddError) as ctx:
            udd.validate_datasets(["sources", "rmh"])
        self.assertEqual(ctx.exception.kind, "argument-invalid")
        self.assertIn("rmh", ctx.exception.message)
        for name in ("sources", "lintian"):
            self.assertIn(name, ctx.exception.message)

    def test_requires_nonempty_list_of_known_strings(self):
        for bad in ([], (), "sources", "sources,lintian", 5, None, [5],
                    [None], ["SOURCES"], ["sources; DROP"], True):
            with self.assertRaises(udd.UddError, msg=repr(bad)):
                udd.validate_datasets(bad)

    def test_groups_and_datasets_mutually_exclusive(self):
        runner = RecordingRunner(stdout=b"")
        with self.assertRaises(udd.UddError) as ctx:
            make_client(runner).fetch("pkgone", groups=["source"],
                                      datasets=["sources"])
        self.assertEqual(ctx.exception.kind, "argument-invalid")
        self.assertEqual(runner.calls, [])

    def test_limit_bounds_datasets_path(self):
        runner = RecordingRunner(stdout=b"")
        for bad in (0, -1, 51, "20", 1.5, True):
            with self.assertRaises(udd.UddError, msg=repr(bad)):
                make_client(runner).fetch("pkgone",
                                          datasets=["sources"], limit=bad)
        self.assertEqual(runner.calls, [])

    def test_more_than_six_datasets_rejected_before_psql(self):
        runner = RecordingRunner(stdout=b"")
        seven = ["sources", "upstream", "upstream_metadata", "vcswatch",
                 "urls", "binaries", "bugs"]
        with self.assertRaises(udd.UddError) as ctx:
            make_client(runner).fetch("pkgone", datasets=seven)
        self.assertEqual(ctx.exception.kind, "budget-exhausted")
        self.assertEqual(runner.calls, [])

    def test_total_row_budget_120_enforced_before_psql(self):
        runner = RecordingRunner(stdout=b"")
        six = ["sources", "upstream", "upstream_metadata", "vcswatch",
               "urls", "binaries"]
        # 6 × 21 = 126 > 120 → psql 之前拒绝
        with self.assertRaises(udd.UddError) as ctx:
            make_client(runner).fetch("pkgone", datasets=six, limit=21)
        self.assertEqual(ctx.exception.kind, "budget-exhausted")
        self.assertEqual(runner.calls, [])
        # 恰好 6 × 20 = 120 → 放行（0 行 = 如实空结果）
        ok = RecordingRunner(stdout=csv_text(
            (META_HEADER, [["meta", "x", "now"]]),
            *[(["grp"] + list(udd.DATASETS[n]["columns"]), [])
              for n in six]))
        result = make_client(ok).fetch("pkgone", datasets=six)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["query_count"], 7)  # meta + 6

    def test_heavy_rows_lower_the_budget(self):
        runner = RecordingRunner(stdout=b"")
        mix = ["archived_bugs", "lintian", "sources", "upstream",
               "urls", "binaries"]
        # 10 + 10 + 4×50 = 220 > 120 → 拒绝
        with self.assertRaises(udd.UddError) as ctx:
            make_client(runner).fetch("pkgone", datasets=mix, limit=50)
        self.assertEqual(ctx.exception.kind, "budget-exhausted")
        self.assertEqual(runner.calls, [])
        # limit 20：10 + 10 + 4×20 = 100 ≤ 120 → 放行
        ok = RecordingRunner(stdout=csv_text(
            (META_HEADER, [["meta", "x", "now"]]),
            *[(["grp"] + list(udd.DATASETS[n]["columns"]), [])
              for n in mix]))
        self.assertEqual(
            make_client(ok).fetch("pkgone", datasets=mix)["status"], "ok")


class DatasetsFetchTest(unittest.TestCase):
    def sources_stdout(self, n_rows):
        rows = [["sources", "pkgone", f"1.{i}-1", "sid", "", "", "", "",
                 "", "", "", ""] for i in range(n_rows)]
        return csv_text(
            (META_HEADER, [["meta", "PostgreSQL 17.5", "now"]]),
            (DS_SOURCES_HEADER, rows))

    def lintian_stdout(self, n_rows):
        rows = [["lintian", "pkgone", "warning", "source", "1.0-1",
                 "source", "debian-watch-file-in-native-package", ""]
                for _ in range(n_rows)]
        return csv_text(
            (META_HEADER, [["meta", "PostgreSQL 17.5", "now"]]),
            (DS_LINTIAN_HEADER, rows))

    def test_result_keys_columns_and_provenance(self):
        runner = RecordingRunner(stdout=csv_text(
            (META_HEADER, [["meta", "PostgreSQL 17.5", "now"]]),
            (DS_SOURCES_HEADER, [["sources", "pkgone", f"1.{i}-1", "sid",
                                  "", "", "", "", "", "", "", ""]
                                 for i in range(2)]),
            (DS_BUGS_COUNT_HEADER, [])))
        result = make_client(runner).fetch(
            "pkgone", datasets=["sources", "bugs_count"])
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["error"])
        self.assertEqual(result["datasets"], ["sources", "bugs_count"])
        self.assertNotIn("groups", result)  # 新路径键是 datasets=
        self.assertEqual(result["template_version"],
                         udd.DATASETS_TEMPLATE_VERSION)
        self.assertEqual(result["limit"], 20)
        gd = result["groups_data"]
        self.assertEqual(set(gd), {"sources", "bugs_count"})
        src = gd["sources"]
        self.assertEqual(src["row_count"], 2)
        self.assertEqual(src["columns"],
                         udd.DATASETS["sources"]["columns"])
        self.assertEqual(src["columns"][0], "source")
        self.assertFalse(src["more_available"])
        self.assertEqual([r["provenance"] for r in src["rows"]],
                         ["udd:sources#row=1", "udd:sources#row=2"])
        # 0 行数据集 = 如实空结果（不是失败）
        bc = gd["bugs_count"]
        self.assertEqual(bc["row_count"], 0)
        self.assertEqual(bc["columns"],
                         udd.DATASETS["bugs_count"]["columns"])
        self.assertIn("0 rows returned", bc["note"])
        # 请求形态：meta 先行、默认 limit 20 → limit1=21
        call = runner.calls[0]
        self.assertIn("limit1=21", call["argv"])
        script = call["stdin"].decode()
        self.assertLess(script.index("SELECT 'meta' AS grp"),
                        script.index("SELECT 'sources' AS grp"))
        self.assertIn("PostgreSQL 17.5",
                      result["server"]["server_version"])

    def test_truncation_probe_and_provenance_numbering(self):
        runner = RecordingRunner(stdout=self.sources_stdout(21))
        result = make_client(runner).fetch("pkgone",
                                           datasets=["sources"])
        src = result["groups_data"]["sources"]
        self.assertEqual(src["row_count"], 20)
        self.assertTrue(src["more_available"])
        self.assertEqual(src["hidden_row_count"], 1)
        self.assertIn("truncated at 20", src["note"])
        # 行号 = LIMIT 之后的 1 起序号；被丢弃的探测行不编号
        self.assertEqual(src["rows"][0]["provenance"],
                         "udd:sources#row=1")
        self.assertEqual(src["rows"][-1]["provenance"],
                         "udd:sources#row=20")

    def test_dataset_csv_header_drift_is_parse_error(self):
        runner = RecordingRunner(stdout=csv_text(
            (META_HEADER, [["meta", "x", "now"]]),
            (["grp", "nope"], [])))
        result = make_client(runner).fetch("pkgone", datasets=["sources"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["kind"], "parse-error")
        self.assertIn("vcs_browser", result["error"]["message"])

    def test_heavy_dataset_effective_cap_ten(self):
        runner = RecordingRunner(stdout=self.lintian_stdout(11))
        result = make_client(runner).fetch("pkgone",
                                           datasets=["lintian"], limit=50)
        script = runner.calls[0]["stdin"].decode()
        self.assertIn("LIMIT LEAST(:limit1, 11);", script)
        lin = result["groups_data"]["lintian"]
        self.assertEqual(lin["limit"], 10)      # 有效上限 10（非请求的 50）
        self.assertEqual(lin["row_count"], 10)
        self.assertTrue(lin["more_available"])
        self.assertEqual(lin["hidden_row_count"], 1)
        self.assertEqual(lin["rows"][-1]["provenance"],
                         "udd:lintian#row=10")
        self.assertIn("heavy dataset effective cap 10", lin["note"])

    def test_heavy_dataset_low_request_uses_requested_cap(self):
        runner = RecordingRunner(stdout=self.lintian_stdout(4))
        result = make_client(runner).fetch("pkgone",
                                           datasets=["lintian"], limit=3)
        script = runner.calls[0]["stdin"].decode()
        self.assertIn("LIMIT LEAST(:limit1, 4);", script)  # min(3,10)=3
        lin = result["groups_data"]["lintian"]
        self.assertEqual(lin["limit"], 3)
        self.assertEqual(lin["row_count"], 3)
        self.assertTrue(lin["more_available"])

    def test_archived_bugs_is_also_heavy(self):
        runner = RecordingRunner(stdout=csv_text(
            (META_HEADER, [["meta", "x", "now"]])))
        make_client(runner).fetch("pkgone",
                                  datasets=["archived_bugs"], limit=50)
        self.assertIn("LIMIT LEAST(:limit1, 11);",
                      runner.calls[0]["stdin"].decode())

    def test_release_filter_only_for_release_scoped_datasets(self):
        scoped = {"sources", "upstream", "binaries", "reproducible"}
        marker = "OR release = :'release'"
        for name in udd.DATASET_NAMES:
            script = udd.build_script(datasets=(name,))
            self.assertTrue(script.startswith("BEGIN;"), name)
            self.assertTrue(script.rstrip().endswith("COMMIT;"), name)
            if name in scoped:
                self.assertIn(marker, script, name)
            else:
                self.assertNotIn(marker, script, name)
            self.assertRegex(
                script, r"LIMIT (:limit1|LEAST\(:limit1, \d+\));")

    def test_join_determinism_and_readonly_templates(self):
        script = udd.build_script(datasets=("urls", "removals", "lintian"))
        # urls：无主键 → 全展示列 + ctid 收尾
        self.assertIn(
            "ORDER BY d.source, d.version, d.component, d.url, "
            "coalesce(d.filename,''), d.checkmethod, d.ctid", script)
        # removals：与批次表 LEFT JOIN（缺批次 = 未知）
        self.assertIn(
            "LEFT JOIN package_removal_batch b ON b.id = r.batch_id",
            script)
        # lintian：物化视图无唯一键 → ctid 收尾
        self.assertIn("ORDER BY tag, package_version, package_arch, ctid",
                      script)
        lowered = script.lower()
        for forbidden in ("insert into", "update ", "delete from",
                          "create table", "drop ", "alter ", "grant ",
                          "copy "):
            self.assertNotIn(forbidden, lowered)
        self.assertEqual(script.count(";"), 1 + 3 + 2)  # meta + 3 + 事务

    def test_upload_history_closes_order_by_has_tiebreakers(self):
        # 同一 bug 被多次上传关闭 → bug DESC 平局：加 version/file
        # tiebreaker 保证 provenance 行号跨抓取稳定
        script = udd.build_script(datasets=("upload_history_closes",))
        self.assertIn("ORDER BY bug DESC, version DESC, file", script)
        self.assertNotIn("ORDER BY bug DESC LIMIT", script)
        self.assertEqual(
            udd.DATASETS["upload_history_closes"]["order_by"],
            "bug DESC, version DESC, file")

    def test_zero_rows_ok_vs_error_semantics(self):
        stdout = csv_text(
            (META_HEADER, [["meta", "x", "now"]]),
            (DS_REMOVALS_HEADER, []),
            (DS_LINTIAN_HEADER, []))
        runner = RecordingRunner(stdout=stdout)
        result = make_client(runner).fetch(
            "pkgone", datasets=["removals", "lintian"])
        self.assertEqual(result["status"], "ok")
        for name in ("removals", "lintian"):
            entry = result["groups_data"][name]
            self.assertEqual(entry["status"], "ok")
            self.assertEqual(entry["row_count"], 0)
            self.assertIn("0 rows returned", entry["note"])
            self.assertFalse(entry["more_available"])
        # 失败 = 未知：groups_data 为空、绝不读作"无行"
        failing = RecordingRunner(
            stderr=b"psql: error: could not connect", code=2)
        result = make_client(failing).fetch("pkgone",
                                            datasets=["removals"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["kind"], "connection-error")
        self.assertEqual(result["groups_data"], {})

    def test_legacy_groups_path_rows_have_no_provenance(self):
        runner = RecordingRunner(stdout=FULL_STDOUT)
        result = make_client(runner).fetch("cdrkit", groups=udd.GROUPS,
                                           limit=50)
        self.assertEqual(result["groups"], list(udd.GROUPS))
        self.assertNotIn("datasets", result)
        self.assertEqual(result["template_version"],
                         udd.UDD_TEMPLATE_VERSION)
        for entry in result["groups_data"].values():
            for row in entry["rows"]:
                self.assertNotIn("provenance", row)

    def test_legacy_groups_script_is_byte_frozen(self):
        script = udd.build_script(udd.DEFAULT_GROUPS, None)
        self.assertEqual(
            hashlib.sha256(script.encode()).hexdigest(),
            "c411127c09fcaa6b3683d94f8d9646f041acce8240c26c81a7eb4cb68dc4ecc4")


class DatasetCatalogTest(unittest.TestCase):
    def test_list_datasets_shape_and_deterministic_order(self):
        listing = udd.list_datasets()
        self.assertEqual(len(listing), 18)
        self.assertEqual([d["name"] for d in listing],
                         list(udd.DATASET_NAMES))
        expected_keys = {"name", "question", "physical", "filter",
                         "order_by", "columns", "default_limit",
                         "max_limit", "release_scoped", "caveats",
                         "joins"}
        for entry in listing:
            self.assertEqual(set(entry), expected_keys, entry["name"])
        # 纯函数：两次调用结果一致（且返回拷贝，改写不污染注册表）
        self.assertEqual(udd.list_datasets(), listing)
        listing[0]["caveats"].append("mutated")
        self.assertNotIn("mutated",
                         udd.list_datasets()[0]["caveats"])
        scoped = {e["name"] for e in udd.list_datasets()
                  if e["release_scoped"]}
        self.assertEqual(
            scoped, {"sources", "upstream", "binaries", "reproducible"})
        heavy = {e["name"] for e in udd.list_datasets()
                 if e["default_limit"] == 10}
        self.assertEqual(heavy, {"archived_bugs", "lintian"})

    def test_required_caveats_present(self):
        caveats = {e["name"]: " ".join(e["caveats"])
                   for e in udd.list_datasets()}
        for name in ("removals", "testing_autoremovals",
                     "upstream_metadata"):
            self.assertIn("0 rows", caveats[name], name)
        self.assertIn("NULL", caveats["wnpp"])
        self.assertIn("unknown", caveats["urls"])
        self.assertIn("heavy", caveats["lintian"])
        self.assertIn("heavy", caveats["archived_bugs"])

    def test_render_catalog_compact_markdown_deterministic(self):
        text = udd.render_catalog()
        self.assertEqual(udd.render_catalog(), text)
        for name in udd.DATASET_NAMES:
            self.assertIn(name, text)
        self.assertIn("NULL and empty string", text)   # 全局 psql 坑
        self.assertIn("release-scoped", text)          # release 适用范围
        self.assertIn("udd:<dataset>#row=N", text)     # provenance 格式
        self.assertIn("max 6 datasets", text)          # 预算披露


class ExtractUrlsDatasetsTest(unittest.TestCase):
    def test_new_path_keys_harvested_urls_excluded(self):
        result = {"groups_data": {
            "sources": {"rows": [
                {"homepage": "https://example.org/a/",
                 "vcs_url": "https://salsa.debian.org/debian/a.git",
                 "vcs_browser": "https://browse.example.org/a"}]},
            "upstream_metadata": {"rows": [
                {"key": "Repository",
                 "value": "https://git.example.org/a.git"},
                {"key": "Homepage", "value": "https://example.org/a/"},
                {"key": "Name", "value": "a"}]},
            "vcswatch": {"rows": [
                {"url": "https://salsa.debian.org/debian/a.git",
                 "browser": "https://salsa.debian.org/debian/a"}]},
            "urls": {"rows": [
                {"url": "https://tracker.example.org/duck-target",
                 "redirect_url": "https://other.example.org/duck-redirect",
                 "status_code": "200", "is_ok": "true"}]},
        }}
        urls = udd.extract_candidate_urls(result)
        # 顺序沿用旧路径：sources 字段 → upstream → vcswatch → DEP-12
        self.assertEqual(urls, [
            "https://example.org/a/",
            "https://salsa.debian.org/debian/a.git",
            "https://browse.example.org/a",
            "https://salsa.debian.org/debian/a",
            "https://git.example.org/a.git",
        ])
        # duck_urls 的 URL/重定向不进候选（非上游身份证明）
        self.assertFalse(any("duck-target" in u or "duck-redirect" in u
                             for u in urls))

    def test_both_path_keys_coexist_deduped(self):
        result = {"groups_data": {
            "source": {"rows": [
                {"homepage": "https://example.org/a/",
                 "vcs_url": "https://salsa.debian.org/debian/a.git"}]},
            "sources": {"rows": [
                {"homepage": "https://example.org/a/",
                 "vcs_url": ""}]},
            "packaging": {"rows": [
                {"url": "https://salsa.debian.org/debian/a.git",
                 "browser": "https://salsa.debian.org/debian/a"}]},
            "vcswatch": {"rows": []},
        }}
        self.assertEqual(udd.extract_candidate_urls(result), [
            "https://example.org/a/",
            "https://salsa.debian.org/debian/a.git",
            "https://salsa.debian.org/debian/a",
        ])


# ---- 候选 URL 抽取 -------------------------------------------------------------

class ExtractUrlsTest(unittest.TestCase):
    def test_extracts_and_dedupes_only_http(self):
        runner = RecordingRunner(stdout=FULL_STDOUT)
        result = make_client(runner).fetch("cdrkit", groups=udd.GROUPS,
                                           limit=50)
        urls = udd.extract_candidate_urls(result)
        self.assertIn("https://salsa.debian.org/debian/cdrkit.git", urls)
        self.assertIn("https://salsa.debian.org/debian/cdrkit", urls)
        self.assertEqual(len(urls), len(set(urls)))
        self.assertTrue(all(u.startswith(("http://", "https://"))
                            for u in urls))
        # git://、空值、非 URL 不进入
        self.assertFalse(any(u.startswith("git://") for u in urls))


# ---- CLI -----------------------------------------------------------------------

class FetchUddCliTest(unittest.TestCase):
    def run_cli(self, argv, runner):
        store_dir = tempfile.mkdtemp()
        real_client = udd.UddClient  # 先捕获真类，防 patch 后自递归

        def factory(**kwargs):
            kwargs.pop("policy", None)
            return real_client(runner=runner,
                               dsn=kwargs.get("dsn",
                                              udd.DEFAULT_UDD_DSN),
                               psql_path=kwargs.get("psql_path", "psql"))
        with mock.patch("gleam.udd.UddClient", factory):
            parser = build_parser()
            args = parser.parse_args(
                ["fetch-udd", "--source", "cdrkit", "--udd-psql", "psql",
                 "--store", store_dir] + argv)
            code = cmd_fetch_udd(args)
        return code, store_dir

    def test_success_writes_store_and_check_passes(self):
        runner = RecordingRunner(stdout=FULL_STDOUT)
        code, store_dir = self.run_cli(
            ["--groups", ",".join(udd.GROUPS)], runner)
        self.assertEqual(code, EXIT_OK)
        store = RunStore(store_dir)
        run_id = store.list_runs()[0]
        with open(f"{store.runs_root}/{run_id}/run.json") as fh:
            summary = json.load(fh)
        self.assertEqual(summary["kind"], "udd-fetch")
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["counts"]["rows"],
                         2 + 1 + 2 + 1 + 3 + 3 + 1 + 1)
        self.assertEqual(summary["mode"]["template_version"],
                         udd.UDD_TEMPLATE_VERSION)
        self.assertEqual(summary["counts"]["queries"], 9)
        self.assertEqual(verify_store(store_dir), [])
        raw_csv = os.path.join(store.runs_root, run_id, "raw",
                               "obs-0001.udd.csv")
        self.assertTrue(os.path.isfile(raw_csv))
        with open(raw_csv, "rb") as fh:
            self.assertEqual(fh.read(), FULL_STDOUT)  # 原始 CSV 原样留存

    def test_failure_exit_1_with_artifacts(self):
        runner = RecordingRunner(
            stderr=b"psql: error: could not connect to server", code=2)
        code, store_dir = self.run_cli([], runner)
        self.assertEqual(code, 1)
        store = RunStore(store_dir)
        run_id = store.list_runs()[0]
        with open(f"{store.runs_root}/{run_id}/run.json") as fh:
            summary = json.load(fh)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["status_detail"]["kind"],
                         "connection-error")

    def test_check_deep_verifies_csv_hash(self):
        # 回归（复审）：raw_refs["csv"] 也必须被 check 重算哈希——
        # 损坏的 CSV 应报 mismatch，篡改 content_sha256 也逃不掉
        runner = RecordingRunner(stdout=FULL_STDOUT)
        code, store_dir = self.run_cli(
            ["--groups", ",".join(udd.GROUPS)], runner)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(verify_store(store_dir), [])
        store = RunStore(store_dir)
        run_id = store.list_runs()[0]
        csv_path = os.path.join(store.runs_root, run_id, "raw",
                                "obs-0001.udd.csv")
        with open(csv_path, "rb") as fh:
            original = fh.read()
        with open(csv_path, "wb") as fh:
            fh.write(original[:-1] + b"X")  # 篡改一个字节
        problems = verify_store(store_dir)
        self.assertTrue(any("raw body hash mismatch" in p
                            for p in problems), problems)
        with open(csv_path, "wb") as fh:  # 还原后恢复通过
            fh.write(original)
        self.assertEqual(verify_store(store_dir), [])

    def test_usage_errors(self):
        runner = RecordingRunner(stdout=b"")
        for bad_argv in (["--groups", "drop-table"],   # 组不在允许清单
                         ["--release", "hack'"],       # release 不在允许清单
                         ["--limit", "0"],             # limit 越界
                         ["--udd-dsn", "mysql://x"]):  # DSN 形态
            code, _ = self.run_cli(bad_argv, runner)
            self.assertEqual(code, EXIT_USAGE, msg=bad_argv)
        self.assertEqual(runner.calls, [])  # 用法错误不触网
        parser = build_parser()
        args = parser.parse_args(["fetch-udd", "--source", "Bad Name"])
        self.assertEqual(cmd_fetch_udd(args), EXIT_USAGE)
        args = parser.parse_args(["fetch-udd", "--source",
                                  "lib'; DROP TABLE bugs;--"])
        self.assertEqual(cmd_fetch_udd(args), EXIT_USAGE)


# ---- investigate 工具集成（datasets= 新路径）------------------------------------

class StubFetcher:
    def __init__(self):
        self.policy = {"max_bytes": 1024, "timeout_s": 5.0,
                       "max_redirects": 3, "max_requests": 12,
                       "min_interval_s": 0, "user_agent": "test",
                       "robots_max_bytes": 1024}
        self._robots_bodies = {}
        self.request_count = 0

    def fetch(self, url, headers=None):
        self.request_count += 1
        return FetchOutcome(ok=True, status=200, final_url=url,
                            redirect_chain=[], body=b"unused")


DS_SOURCES_ROW = ["cdrkit", "9:1.1.11-3", "sid",
                  "Example Maintainers <pkg@example.org>",
                  "https://unofficial.dk/cdrkit/", "", "Git",
                  "https://salsa.debian.org/debian/cdrkit.git", "",
                  "otherosfs", "optional"]
DS_BUGS_ROW = ["123", "cdrkit", "cdrkit", "open", "normal",
               "wodim does not burn", "2020-01-01 00:00:00+00",
               "2020-01-02 00:00:00+00", "", "", "true", "false"]


def datasets_stdout(*names, bug_title="wodim does not burn"):
    """按 datasets= 路径列序合成 meta + 各数据集的 psql --csv 输出。"""

    bugs_row = ["bugs"] + list(DS_BUGS_ROW)
    bugs_row[6] = bug_title  # grp + title 列（grp 之后第 6 列）
    rows_by_name = {"sources": ["sources"] + DS_SOURCES_ROW,
                    "bugs": bugs_row}
    statements = [(META_HEADER, [["meta", "PostgreSQL 17.5",
                                  "2026-11-06 00:00:00+00"]])]
    for name in names:
        statements.append(
            (["grp"] + list(udd.DATASETS[name]["columns"]),
             [rows_by_name[name]] if name in rows_by_name else []))
    return csv_text(*statements)


class AgentToolTest(unittest.TestCase):
    def make_context(self, udd_enabled=True, client=None):
        store_dir = tempfile.mkdtemp()
        store = RunStore(store_dir)
        run_id = store.create_run()
        return InvestigationContext(
            store=store, run_id=run_id, package="cdrkit",
            udd_client=client, default_release=None,
            udd_max_calls=6, udd_max_rows=200,
            fetcher=StubFetcher(),
            limits={"max_body_chars": 4000, "max_links": 40,
                    "max_context_chars": 60000, "max_rounds": 8,
                    "max_tool_calls": 24, "max_report_repairs": 2},
            search_enabled=False, search_client=None, github_base=None,
            udd_enabled=udd_enabled)

    def test_tool_spec_registered(self):
        names = {spec["function"]["name"] for spec in TOOL_SPECS}
        self.assertIn("fetch_udd", names)
        udd_spec = next(s for s in TOOL_SPECS
                        if s["function"]["name"] == "fetch_udd")
        params = udd_spec["function"]["parameters"]
        self.assertEqual(params["required"], ["action"])
        self.assertEqual(params["properties"]["action"]["enum"],
                         ["list", "fetch"])
        self.assertEqual(params["properties"]["datasets"]["items"]["enum"],
                         list(udd.DATASET_NAMES))
        self.assertEqual(params["properties"]["limit"]["maximum"], 50)
        bts_spec = next(s for s in TOOL_SPECS
                        if s["function"]["name"] == "bug_fetch")
        self.assertEqual(
            bts_spec["function"]["parameters"]["properties"]["source"]
            ["enum"], ["auto", "soap", "mbox"])

    def test_list_action_returns_catalog_without_client(self):
        # action=list 纯目录：client 为 None / 工具被禁用也照常工作
        ctx = self.make_context(udd_enabled=False, client=None)
        payload = ctx.execute("fetch_udd", {"action": "list"})
        self.assertEqual(payload["action"], "list")
        for name in udd.DATASET_NAMES:
            self.assertIn(f"**{name}**", payload["catalog"])
        self.assertIn("no database query", payload["note"])
        self.assertEqual(ctx.records, [])
        self.assertEqual(ctx.udd_calls, 0)
        self.assertEqual(set(ctx.fact_citations), set())

    def test_bad_action_rejected(self):
        ctx = self.make_context()
        for bad in ({"action": "drop"}, {}, {"action": None}):
            payload = ctx.execute("fetch_udd", bad)
            self.assertEqual(payload["error"]["kind"], "argument-invalid",
                             msg=repr(bad))
            self.assertIn("action must be", payload["error"]["message"])

    def test_success_payload_columnar_and_evidence(self):
        ctx = self.make_context(
            client=make_client(
                RecordingRunner(stdout=datasets_stdout("sources", "bugs"))))
        payload = ctx.execute("fetch_udd", {
            "action": "fetch", "datasets": ["sources", "bugs"]})
        self.assertNotIn("error", payload)
        self.assertEqual(payload["action"], "fetch")
        self.assertEqual(payload["package"], "cdrkit")
        self.assertEqual(payload["source_kind"], "udd-mirror-readonly-rows")
        self.assertEqual(payload["template_version"],
                         udd.DATASETS_TEMPLATE_VERSION)
        self.assertEqual(payload["query_count"], 3)
        src = payload["datasets_data"]["sources"]
        self.assertEqual(src["columns"],
                         list(udd.DATASETS["sources"]["columns"]))
        self.assertEqual(src["row_count"], 1)
        self.assertEqual(len(src["rows"][0]), len(src["columns"]))
        self.assertEqual(src["provenance"], ["udd:sources#row=1"])
        bugs = payload["datasets_data"]["bugs"]
        # datasets= 路径逐字保留 CSV 单元格（via_* 不做 bool 转换——
        # 与 legacy bugs 组不同，这是 stage 1 已定的 udd.py 语义）
        self.assertEqual(
            bugs["rows"][0][bugs["columns"].index("via_source_mapping")],
            "true")
        obs_id = payload["observation_id"]
        entry = ctx.evidence[obs_id]
        self.assertIn("https://salsa.debian.org/debian/cdrkit.git",
                      entry["qualifying_urls_norm"])
        self.assertEqual(ctx.fact_citations,
                         {"udd:sources#row=1", "udd:bugs#row=1"})
        self.assertEqual(ctx.udd_calls, 1)
        self.assertEqual(ctx.udd_rows_used, 2)
        self.assertEqual(ctx.tool_counts["fetch_udd"]["calls"], 1)
        self.assertEqual(ctx.tool_counts["fetch_udd"]["errors"], 0)
        # 原始 CSV 已随观测落盘
        self.assertIn("csv", next(r for r in ctx.records
                                  if r["observation_id"] == obs_id)
                      ["raw_refs"])

    def test_unknown_dataset_rejected_before_subprocess(self):
        runner = RecordingRunner(stdout=b"")
        ctx = self.make_context(client=make_client(runner))
        payload = ctx.execute("fetch_udd", {
            "action": "fetch", "datasets": ["DROP TABLE"]})
        self.assertEqual(payload["error"]["kind"], "argument-invalid")
        self.assertEqual(runner.calls, [])
        self.assertEqual(ctx.udd_calls, 0)

    def test_budgets_enforced_before_subprocess(self):
        runner = RecordingRunner(stdout=datasets_stdout("sources"))
        ctx = self.make_context(client=make_client(runner))
        first = ctx.execute("fetch_udd", {
            "action": "fetch", "datasets": ["sources"]})
        self.assertNotIn("error", first)
        capped = InvestigationContext(
            store=ctx.store, run_id=ctx.run_id, package="cdrkit",
            udd_client=make_client(runner), default_release=None,
            udd_max_calls=1, udd_max_rows=200,
            fetcher=StubFetcher(),
            limits={"max_body_chars": 4000, "max_links": 40,
                    "max_context_chars": 60000, "max_rounds": 8,
                    "max_tool_calls": 24, "max_report_repairs": 2},
            search_enabled=False, search_client=None, github_base=None)
        capped.udd_calls = 1  # 模拟预算已被此前调用消耗
        payload = capped.execute("fetch_udd", {
            "action": "fetch", "datasets": ["sources"]})
        self.assertEqual(payload["error"]["kind"], "budget-exhausted")
        self.assertIn("call cap 1", payload["error"]["message"])
        self.assertEqual(len(runner.calls), 1)

    def test_disabled_honest(self):
        ctx = self.make_context(udd_enabled=False, client=None)
        payload = ctx.execute("fetch_udd", {
            "action": "fetch", "datasets": ["sources"]})
        self.assertEqual(payload["error"]["kind"], "capability-disabled")

    def test_error_honest_note(self):
        failing = RecordingRunner(
            stderr=b"psql: error: could not connect", code=2)
        ctx = self.make_context(client=make_client(failing))
        payload = ctx.execute("fetch_udd", {
            "action": "fetch", "datasets": ["sources"]})
        self.assertEqual(payload["error"]["kind"], "connection-error")
        self.assertIn("never read as", payload["note"])
        self.assertEqual(ctx.tool_counts["fetch_udd"]["errors"], 1)
        # 失败也写了观测（错误 = 未知，不冒充无数据）；psql 确实跑过 → 计数
        self.assertEqual(len(ctx.records), 1)
        self.assertEqual(ctx.udd_calls, 1)
        self.assertEqual(ctx.udd_rows_used, 0)

    def test_field_truncation_in_payload_only(self):
        long_title = "T" * 2000
        stdout = datasets_stdout("bugs", bug_title=long_title)
        ctx = self.make_context(client=make_client(
            RecordingRunner(stdout=stdout)))
        payload = ctx.execute("fetch_udd", {
            "action": "fetch", "datasets": ["bugs"]})
        entry = payload["datasets_data"]["bugs"]
        title = entry["rows"][0][entry["columns"].index("title")]
        self.assertIn("field truncated at", title)
        self.assertLess(len(title),
                        udd.DEFAULT_POLICY["field_char_cap"] + 100)
        with open(os.path.join(ctx.store.run_dir, "raw",
                               f"{payload['observation_id']}.udd.csv"),
                  "rb") as fh:
            self.assertIn(long_title.encode(), fh.read(),
                          "原始 CSV 不截断")


if __name__ == "__main__":
    unittest.main()
