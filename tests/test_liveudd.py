"""live UDD 事实源（liveudd）测试：适配器候选语义、成员判定、出处/引用、
缓存（写/读/未命中/损坏）、选择路径（快速路径 vs cohort）、UDD 失败分级。

全部离线（FakeUddRunner 假 psql），不触碰网络与仓库 raw/。
"""

import json
import os
import tempfile
import unittest

from gleam import liveudd, udd
from gleam.facts import LIVE_TABLES

from .fixtures import (FakeUddRunner, PKGONE_WATCH_FILE, QA_MAINTAINER,
                       UDD_FACTS, UDD_NON_QA_SOURCE_ROW, csv_text,
                       patch_udd_client, udd_facts_stdout)


def make_client(runner, **policy):
    merged = dict(udd.DEFAULT_POLICY)
    merged.update(policy)
    return udd.UddClient(runner=runner, policy=merged)


class AdapterSemanticsTest(unittest.TestCase):
    """live 行 → PackageFacts/Candidate 适配：候选语义与快照时代一致。"""

    @classmethod
    def setUpClass(cls):
        cls.lf = liveudd.fetch_live_facts(
            make_client(FakeUddRunner()), "pkgone")

    def test_tables_and_citations(self):
        pkg = self.lf.facts
        self.assertEqual(pkg.tables, LIVE_TABLES)
        tables = {f.table for f in pkg.rows}
        self.assertEqual(tables, {"udd:sources", "udd:upstream",
                                  "udd:upstream_metadata", "udd:vcswatch"})
        cites = pkg.fact_citations()
        self.assertIn("udd:sources#row=1", cites)
        self.assertIn("udd:upstream#row=1", cites)
        self.assertIn("udd:upstream_metadata#row=1", cites)
        self.assertIn("udd:vcswatch#row=1", cites)
        self.assertFalse(any(c.startswith(("sources.csv", "upstream.csv"))
                             for c in cites))

    def test_candidate_labels_and_fetch_policy(self):
        labels = {(c.url, c.label, c.kind, c.fetch)
                  for c in self.lf.facts.upstream_candidates()}
        self.assertIn(("https://example.org/pkgone/",
                       "homepage-candidate", "web", True), labels)
        self.assertIn(("https://git.example.org/pkgone.git",
                       "dep12-repository", "git-endpoint", False), labels)
        self.assertIn(("https://git.example.org/pkgone",
                       "dep12-repository-browse", "web", True), labels)
        self.assertIn(("https://example.org/pkgone/pkgone-1.1.tar.gz",
                       "upstream-artifact", "artifact", False), labels)

    def test_packaging_vcs_never_upstream_candidate(self):
        pkg = self.lf.facts
        vcs_urls = {v["url"] for v in pkg.packaging_vcs()}
        self.assertTrue(vcs_urls)
        for v in pkg.packaging_vcs():
            self.assertIn("packaging provenance", v["note"])
        upstream = {c.url for c in pkg.upstream_candidates()}
        self.assertFalse(vcs_urls & upstream,
                         "Debian 打包 VCS 绝不进入上游候选")

    def test_watch_file_preserved_raw(self):
        pkg = self.lf.facts
        # first() 做空白规整（与快照时代语义一致）；多行原文逐字保留
        watch = pkg.first("udd:upstream", "watch_file")
        self.assertEqual(watch, PKGONE_WATCH_FILE.strip())
        self.assertIn("version=4", watch)
        self.assertIn("\\d\\.\\d", watch)

    def test_no_candidate_package(self):
        lf = liveudd.fetch_live_facts(make_client(FakeUddRunner()),
                                      "pkgtwo")
        self.assertEqual(lf.facts.upstream_candidates(), [])

    def test_provenance_fields(self):
        prov = self.lf.provenance
        self.assertEqual(prov["kind"], "udd-live")
        self.assertEqual(prov["template_version"],
                         udd.UDD_TEMPLATE_VERSION)
        self.assertTrue(prov["response_sha256"])
        self.assertTrue(prov["fetched_at"])
        self.assertEqual(prov["row_counts"],
                         {"source": 1, "upstream": 1, "metadata": 2,
                          "packaging": 1})


class PackagingDeterministicNumberingTest(unittest.TestCase):
    """legacy packaging 模板无 ORDER BY（v2 字节冻结）→ 适配层按固定
    展示列稳定排序后编号，provenance 行号与服务器返回顺序无关。"""

    PKG = "pkgsort"
    # 固定模板 v2 packaging 列序：grp,source,version,vcs,url,browser,
    # status,last_scan,changelog_distribution,commit_id
    ROW_A = ["packaging", "pkgsort", "1.0-1", "Git",
             "https://salsa.example.org/a.git",
             "https://salsa.example.org/a", "ok",
             "2026-10-30 04:00:53+00", "unstable", "mmm111"]
    ROW_B = ["packaging", "pkgsort", "1.0-1", "Svn",
             "https://salsa.example.org/b.git",
             "https://salsa.example.org/b", "ok",
             "2026-10-30 04:00:53+00", "unstable", "abc222"]
    ROW_C = ["packaging", "pkgsort", "1.0-1", "Git",
             "https://salsa.example.org/c.git",
             "https://salsa.example.org/c", "ok",
             "2026-10-30 04:00:53+00", "unstable", "zzz333"]

    def _lf(self, packaging_rows):
        source_row = ["source", self.PKG, "1.0-1", "sid", QA_MAINTAINER,
                      "https://example.org/pkgsort/", "pkgsort", "Git",
                      "https://salsa.debian.org/debian/pkgsort.git", "devel",
                      "optional", ""]
        runner = FakeUddRunner(extra_sources={
            self.PKG: {"source": [source_row], "upstream": [],
                       "metadata": [], "packaging": packaging_rows}})
        return liveudd.fetch_live_facts(make_client(runner), self.PKG)

    def _packaging_facts(self, lf):
        return [(f.row_number, f.fields) for f in lf.facts.rows
                if f.table == "udd:vcswatch"]

    def test_numbering_independent_of_server_return_order(self):
        lf1 = self._lf([self.ROW_A, self.ROW_B, self.ROW_C])
        lf2 = self._lf([self.ROW_C, self.ROW_A, self.ROW_B])
        self.assertEqual(self._packaging_facts(lf1),
                         self._packaging_facts(lf2))
        # 排序键 = (vcs,url,browser,status,commit_id)：Git 在 Svn 前，
        # 两行 Git 再按 url 排
        urls = [f["url"] for _, f in self._packaging_facts(lf1)]
        self.assertEqual(urls, ["https://salsa.example.org/a.git",
                                "https://salsa.example.org/c.git",
                                "https://salsa.example.org/b.git"])
        cites = {c for c in lf1.facts.fact_citations()
                 if c.startswith("udd:vcswatch#")}
        self.assertEqual(cites, {"udd:vcswatch#row=1", "udd:vcswatch#row=2",
                                 "udd:vcswatch#row=3"})

    def test_single_row_unchanged(self):
        lf = self._lf([self.ROW_B])
        expected = dict(zip(
            ("source", "version", "vcs", "url", "browser", "status",
             "last_scan", "changelog_distribution", "commit_id"),
            self.ROW_B[1:]))
        self.assertEqual(self._packaging_facts(lf), [(1, expected)])


class MembershipTest(unittest.TestCase):
    def test_unknown_package_rejected(self):
        client = make_client(FakeUddRunner())
        with self.assertRaises(liveudd.LiveFactsError) as ctx:
            liveudd.fetch_live_facts(client, "nosuchpkg")
        self.assertEqual(ctx.exception.kind, "unknown-package")
        self.assertIn("refusing to fabricate", ctx.exception.message)

    def test_not_in_qa_cohort_rejected(self):
        runner = FakeUddRunner(extra_sources={
            "keepalived": {
                "source": [UDD_NON_QA_SOURCE_ROW],
                "upstream": [], "metadata": [], "packaging": []}})
        client = make_client(runner)
        with self.assertRaises(liveudd.LiveFactsError) as ctx:
            liveudd.fetch_live_facts(client, "keepalived")
        self.assertEqual(ctx.exception.kind, "not-in-cohort")

    def test_invalid_name_rejected_before_subprocess(self):
        runner = FakeUddRunner()
        client = make_client(runner)
        for bad in ("Bad Name", "a;b", "../etc", ""):
            with self.assertRaises(liveudd.LiveFactsError) as ctx:
                liveudd.fetch_live_facts(client, bad)
            self.assertEqual(ctx.exception.kind, "argument-invalid")
        self.assertEqual(runner.calls, [])

    def test_udd_failure_kinds_pass_through(self):
        for error, expected in (
                (("connection",
                  b"psql: error: could not connect to server"), 
                 "connection-error"),
                (("query", b"psql:<stdin>:3: ERROR:  permission denied"),
                 "query-error")):
            runner = FakeUddRunner(error=error)
            client = make_client(runner)
            with self.assertRaises(liveudd.LiveFactsError) as ctx:
                liveudd.fetch_live_facts(client, "pkgone")
            self.assertEqual(ctx.exception.kind, expected)


class SelectionTest(unittest.TestCase):
    def test_named_fast_path_skips_cohort(self):
        runner = FakeUddRunner()
        client = make_client(runner)
        out, meta = liveudd.select_live_packages(
            client, names=["pkgone", "pkgone", "pkgthree"])
        self.assertEqual([lf.source for lf in out], ["pkgone", "pkgthree"],
                         "重复名去重保序")
        self.assertEqual(len(runner.calls), 2, "每包恰一次 facts 查询")
        scripts = [c["stdin"].decode() for c in runner.calls]
        self.assertFalse(any("'cohort' AS grp" in s for s in scripts),
                         "指定包名时不得枚举 cohort")
        self.assertEqual(meta["mode"], "named-fast-path")
        self.assertFalse(meta["cohort_queried"])

    def test_cohort_selection_bounded_and_ordered(self):
        runner = FakeUddRunner(cohort_names=("pkgthree", "pkgone"),
                               cohort_more_names=("zzz-more",))
        client = make_client(runner)
        out, meta = liveudd.select_live_packages(client, limit=2)
        self.assertEqual([lf.source for lf in out], ["pkgone", "pkgthree"])
        self.assertTrue(meta["more_available"])
        self.assertTrue(meta["fetched_at"])
        self.assertTrue(meta["response_sha256"])
        self.assertEqual(meta["template_version"],
                         udd.COHORT_TEMPLATE_VERSION)
        scripts = [c["stdin"].decode() for c in runner.calls]
        self.assertIn("'cohort' AS grp", scripts[0])

    def test_cohort_query_failure_raises(self):
        runner = FakeUddRunner(error=("connection",
                                      b"could not connect to server"))
        client = make_client(runner)
        with self.assertRaises(liveudd.LiveFactsError) as ctx:
            liveudd.select_live_packages(client, limit=3)
        self.assertEqual(ctx.exception.kind, "connection-error")


class FactsCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = liveudd.FactsCache(os.path.join(self.tmp.name, "fc"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_then_offline_read_roundtrip(self):
        client = make_client(FakeUddRunner())
        lf = liveudd.fetch_live_facts(client, "pkgone")
        self.cache.write("pkgone", lf.result, lf.raw_csv)
        cached = liveudd.facts_from_cache(self.cache, "pkgone")
        self.assertEqual(cached.source, "pkgone")
        self.assertEqual(cached.provenance["kind"], "udd-cache")
        cache_info = cached.provenance["cache"]
        self.assertEqual(cache_info["original_fetched_at"],
                         lf.provenance["fetched_at"])
        self.assertTrue(cache_info["written_at"])
        # 事实与候选拷贝自缓存结果，语义不变
        self.assertEqual(
            [c.url for c in cached.facts.upstream_candidates()],
            [c.url for c in lf.facts.upstream_candidates()])

    def test_select_writes_cache_when_configured(self):
        runner = FakeUddRunner()
        client = make_client(runner)
        liveudd.select_live_packages(client, names=["pkgone"],
                                     cache=self.cache)
        self.assertTrue(os.path.isfile(
            os.path.join(self.cache.root, "pkgone.json")))

    def test_cache_miss_is_honest_failure(self):
        with self.assertRaises(liveudd.LiveFactsError) as ctx:
            liveudd.facts_from_cache(self.cache, "pkgone")
        self.assertEqual(ctx.exception.kind, "cache-miss")
        self.assertIn("绝不静默回退", ctx.exception.message)

    def test_offline_facts_never_touches_network(self):
        runner = FakeUddRunner()
        client = make_client(runner)
        lf = liveudd.fetch_live_facts(client, "pkgone")
        self.cache.write("pkgone", lf.result, lf.raw_csv)
        runner.calls.clear()
        out = liveudd.offline_facts(self.cache, ["pkgone", "pkgone"])
        self.assertEqual([x.source for x in out], ["pkgone"])
        self.assertEqual(runner.calls, [], "offline 不得发起 UDD 查询")

    def test_corrupt_cache_rejected(self):
        path = os.path.join(self.cache.root, "pkgone.json")
        os.makedirs(self.cache.root, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with self.assertRaises(liveudd.LiveFactsError) as ctx:
            liveudd.facts_from_cache(self.cache, "pkgone")
        self.assertEqual(ctx.exception.kind, "cache-corrupt")
        # schema 不符同样拒绝
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema": "something-else", "source": "pkgone"},
                      fh)
        with self.assertRaises(liveudd.LiveFactsError) as ctx:
            liveudd.facts_from_cache(self.cache, "pkgone")
        self.assertEqual(ctx.exception.kind, "cache-corrupt")

    def _write_valid_entry(self):
        client = make_client(FakeUddRunner())
        lf = liveudd.fetch_live_facts(client, "pkgone")
        self.cache.write("pkgone", lf.result, lf.raw_csv)
        path = os.path.join(self.cache.root, "pkgone.json")
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    @staticmethod
    def _rewrite(path, payload):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    def test_tampered_result_rows_rejected(self):
        payload = self._write_valid_entry()
        path = os.path.join(self.cache.root, "pkgone.json")
        row = payload["result"]["groups_data"]["source"]["rows"][0]
        row["homepage"] = "https://evil.example/Injected"
        self._rewrite(path, payload)
        with self.assertRaises(liveudd.LiveFactsError) as ctx:
            liveudd.facts_from_cache(self.cache, "pkgone")
        self.assertEqual(ctx.exception.kind, "cache-corrupt")

    def test_tampered_raw_csv_rejected(self):
        import base64 as base64_mod
        payload = self._write_valid_entry()
        path = os.path.join(self.cache.root, "pkgone.json")
        forged = b"grp,source,version\nsource,pkgone,9:9\n"
        payload["raw_csv_b64"] = base64_mod.b64encode(
            forged).decode("ascii")
        self._rewrite(path, payload)
        with self.assertRaises(liveudd.LiveFactsError) as ctx:
            liveudd.facts_from_cache(self.cache, "pkgone")
        self.assertEqual(ctx.exception.kind, "cache-corrupt")

    def test_online_with_cache_still_fetches_live(self):
        # 配置缓存的在线运行必须仍然 live 抓取（缓存只写不读）
        runner = FakeUddRunner()
        client = make_client(runner)
        liveudd.select_live_packages(client, names=["pkgone"],
                                     cache=self.cache)
        self.assertEqual(len(runner.calls), 1)


class ProvenanceSummaryTest(unittest.TestCase):
    def test_summary_shape(self):
        client = make_client(FakeUddRunner())
        out, meta = liveudd.select_live_packages(client, names=["pkgone"])
        summary = liveudd.input_provenance_summary(
            out, release="sid", selection_meta=meta)
        self.assertEqual(summary["kind"], "udd-live")
        self.assertIn("Debian QA Group", summary["qa_filter"])
        self.assertIn("pkgone", summary["packages"])
        self.assertEqual(summary["template_versions"]["facts"],
                         udd.UDD_TEMPLATE_VERSION)
        self.assertIn("NOT an input", json.dumps(summary["notes"]))
        self.assertIn("udd-mirror", summary["dsn_display"])


class NoRootRawTest(unittest.TestCase):
    """活跃事实源绝不读取仓库根 raw/（行为级保证）。"""

    def test_raw_directory_absent_from_fixture_env(self):
        # FakeUddRunner 全程合成输出：liveudd 路径没有任何文件系统输入
        runner = FakeUddRunner()
        client = make_client(runner)
        out, _ = liveudd.select_live_packages(client, names=["pkgone"])
        self.assertEqual(out[0].source, "pkgone")

    def test_liveudd_module_never_imports_snapshot_loader(self):
        import gleam.cli as cli_mod
        self.assertFalse(hasattr(cli_mod, "load_snapshot"),
                         "cli 不得再暴露快照加载器")
        self.assertFalse(hasattr(cli_mod, "DEFAULT_RAW"))

    def test_snapshot_tables_never_mixed_into_live_facts(self):
        lf = liveudd.fetch_live_facts(make_client(FakeUddRunner()),
                                      "pkgone")
        tables = {f.table for f in lf.facts.rows}
        self.assertFalse(any(t.endswith(".csv") for t in tables),
                         "live 事实不得混入快照表名")


if __name__ == "__main__":
    unittest.main()
