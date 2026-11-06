import hashlib
import json
import os
import tempfile
import unittest

from gleam import observe
from gleam.net import FetchOutcome
from gleam.snapshot import load_snapshot
from gleam.store import RunStore

from .fixtures import write_fixture


class StubFetcher:
    """可控假抓取器：collect_observations 只用到 fetch/policy/_robots_bodies。

    results 队列按抓取顺序消耗（homepage 先于 dep12-repository-browse），
    队列耗尽后回落到默认 200 响应。
    """

    def __init__(self, results, default=None):
        self.results = list(results)
        self.default = default or ok_result(b"page")
        self.policy = {"max_bytes": 1024, "timeout_s": 5.0,
                       "max_redirects": 3, "max_requests": 10,
                       "min_interval_s": 0}
        self._robots_bodies = {}
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        maker = self.results.pop(0) if self.results else self.default
        return maker(url)


def ok_result(body=b"page"):
    def make(url):
        return FetchOutcome(ok=True, status=200, final_url=url,
                            redirect_chain=[], body=body)
    return make


def status_result(status, body=b"", truncated=False):
    def make(url):
        return FetchOutcome(ok=True, status=status, final_url=url,
                            redirect_chain=[], body=body,
                            body_truncated=truncated)
    return make


def error_result(kind="timeout", message="simulated"):
    def make(url):
        return FetchOutcome(ok=False, error={"kind": kind, "message": message})
    return make


def dir_fingerprint(path):
    out = {}
    for root, _dirs, files in os.walk(path):
        for name in sorted(files):
            full = os.path.join(root, name)
            with open(full, "rb") as fh:
                out[os.path.relpath(full, path)] = hashlib.sha256(fh.read()).hexdigest()
    return out


class BaselineAndAppendOnlyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.raw = write_fixture(os.path.join(cls.tmp.name, "raw"))
        cls.snapshot = load_snapshot(cls.raw)
        cls.pkg = cls.snapshot.packages["pkgone"]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        # 每个测试独立存储，避免共享基线造成的顺序耦合
        self.store_root = os.path.join(self.tmp.name,
                                       "store-" + self.id().rsplit(".", 1)[-1])
        os.makedirs(self.store_root, exist_ok=True)

    def _run(self, results):
        store = RunStore(self.store_root)
        run_id = store.create_run()
        fetcher = StubFetcher(results)
        # 只观测 homepage 候选，减少噪音：直接调用 collect，跳过 register-only 候选
        # （register-only 候选不触发 fetch，不影响 StubFetcher 的结果队列）
        records = observe.collect_observations(
            store, [self.pkg], fetcher, {},
            offline=False, run_id=run_id)
        store.update_index(records)
        return store, run_id, records

    def test_first_run_then_changed_then_failed_then_restored(self):
        _, run1, rec1 = self._run([ok_result(b"AAA")])
        home1 = [r for r in rec1 if r["endpoint"]["label"] == "homepage-candidate"]
        self.assertEqual(len(home1), 1)
        self.assertEqual(home1[0]["outcome"], "OK")
        self.assertEqual(home1[0]["comparison"], "first-observation")
        self.assertIsNone(home1[0]["baseline"])

        fingerprint_run1 = dir_fingerprint(os.path.join(self.store_root,
                                                        "runs", run1))

        _, run2, rec2 = self._run([ok_result(b"BBB")])
        home2 = [r for r in rec2 if r["endpoint"]["label"] == "homepage-candidate"][0]
        self.assertEqual(home2["outcome"], "REPRESENTATION_CHANGED")
        self.assertEqual(home2["comparison"], "changed")
        self.assertEqual(home2["baseline"]["run_id"], run1)
        self.assertEqual(home2["baseline"]["observation_id"], home1[0]["observation_id"])

        # 历史运行不可被改写：run1 目录逐文件哈希不变
        self.assertEqual(fingerprint_run1,
                         dir_fingerprint(os.path.join(self.store_root, "runs", run1)))

        # 失败扫描：TRANSPORT_ERROR，不成为 baseline
        _, run3, rec3 = self._run([error_result("timeout", "boom")])
        home3 = [r for r in rec3 if r["endpoint"]["label"] == "homepage-candidate"][0]
        self.assertEqual(home3["outcome"], "TRANSPORT_ERROR")
        self.assertIsNone(home3["comparison"])

        # 下一次成功仍与 run2（上一次成功）比较，而不是失败的 run3
        _, run4, rec4 = self._run([ok_result(b"AAA")])
        home4 = [r for r in rec4 if r["endpoint"]["label"] == "homepage-candidate"][0]
        self.assertEqual(home4["baseline"]["run_id"], run2)
        self.assertEqual(home4["comparison"], "changed")

        # 完全相同 → NOT_MODIFIED
        _, _run5, rec5 = self._run([ok_result(b"AAA")])
        home5 = [r for r in rec5 if r["endpoint"]["label"] == "homepage-candidate"][0]
        self.assertEqual(home5["outcome"], "NOT_MODIFIED")
        self.assertEqual(home5["comparison"], "unchanged")

        # run1 至此仍未被触碰
        self.assertEqual(fingerprint_run1,
                         dir_fingerprint(os.path.join(self.store_root, "runs", run1)))

    def test_register_only_candidates_are_not_probed(self):
        store = RunStore(self.store_root)
        run_id = store.create_run()
        fetcher = StubFetcher([ok_result(b"X")])
        records = observe.collect_observations(
            store, [self.pkg], fetcher, {},
            offline=False, run_id=run_id)
        not_probed = [r for r in records if r["outcome"] == "NOT_PROBED"]
        labels = {r["endpoint"]["label"] for r in not_probed}
        self.assertIn("dep12-repository", labels)
        self.assertIn("upstream-artifact", labels)
        probed = [r for r in records if r["outcome"] != "NOT_PROBED"]
        self.assertTrue(all(r["endpoint"]["kind"] == "web" for r in probed))

    def test_offline_run_abstains_without_network(self):
        store = RunStore(self.store_root)
        run_id = store.create_run()
        records = observe.collect_observations(
            store, [self.pkg], None, {},
            offline=True, run_id=run_id)
        self.assertTrue(records)
        for rec in records:
            self.assertEqual(rec["outcome"], "ABSTAIN")
            self.assertIn("offline", rec["reason"])
        self.assertTrue(all(rec["status_code"] is None for rec in records))

    def test_run_dirs_exclusive_and_unique(self):
        store = RunStore(self.store_root)
        ids = {store.create_run() for _ in range(8)}
        self.assertEqual(len(ids), 8)
        for run_id in ids:
            self.assertTrue(os.path.isdir(os.path.join(store.runs_root, run_id)))

    def test_raw_body_and_headers_persisted(self):
        _, run_id, records = self._run([ok_result(b"CONTENT")])
        home = [r for r in records if r["endpoint"]["label"] == "homepage-candidate"][0]
        run_dir = os.path.join(self.store_root, "runs", run_id)
        body_path = os.path.join(run_dir, home["raw_refs"]["body"])
        with open(body_path, "rb") as fh:
            self.assertEqual(fh.read(), b"CONTENT")
        headers_path = os.path.join(run_dir, home["raw_refs"]["headers"])
        self.assertTrue(os.path.isfile(headers_path))
        self.assertEqual(home["content_sha256"],
                         hashlib.sha256(b"CONTENT").hexdigest())

    def test_304_keeps_baseline_hash(self):
        _, run1, rec1 = self._run([ok_result(b"AAA")])
        first = [r for r in rec1
                 if r["endpoint"]["label"] == "homepage-candidate"][0]
        self.assertEqual(first["outcome"], "OK")

        _, _run2, rec2 = self._run([status_result(304, b"")])
        nm = [r for r in rec2
              if r["endpoint"]["label"] == "homepage-candidate"][0]
        # 304：沿用基线哈希，绝不用空 body 哈希覆盖
        self.assertEqual(nm["outcome"], "NOT_MODIFIED")
        self.assertEqual(nm["comparison"], "unchanged")
        self.assertEqual(nm["content_sha256"], first["content_sha256"])
        self.assertTrue(nm["effective_hash_from_baseline"])
        index = RunStore(self.store_root).load_index()["endpoints"]
        self.assertEqual(
            index["https://example.org/pkgone/"]["content_sha256"],
            first["content_sha256"])

        # 下一轮 200 同内容仍应判 unchanged（证明基线哈希未被 304 破坏）
        _, _run3, rec3 = self._run([ok_result(b"AAA")])
        again = [r for r in rec3
                 if r["endpoint"]["label"] == "homepage-candidate"][0]
        self.assertEqual(again["outcome"], "NOT_MODIFIED")
        self.assertEqual(again["comparison"], "unchanged")

    def test_http_4xx_is_error_not_baseline(self):
        _, run1, rec1 = self._run([ok_result(b"AAA")])
        first = [r for r in rec1
                 if r["endpoint"]["label"] == "homepage-candidate"][0]

        _, _run2, rec2 = self._run([status_result(404, b"<html>gone")])
        err = [r for r in rec2
               if r["endpoint"]["label"] == "homepage-candidate"][0]
        # 4xx：显式 HTTP_ERROR，保留响应，不作死亡推断，不成为基线
        self.assertEqual(err["outcome"], "HTTP_ERROR")
        self.assertEqual(err["status_code"], 404)
        run_dir = os.path.join(self.store_root, "runs", _run2)
        with open(os.path.join(run_dir, err["raw_refs"]["body"]), "rb") as fh:
            self.assertEqual(fh.read(), b"<html>gone")

        # 下一轮仍与 run1（成功基线）比较，而不是 404 那轮
        _, _run3, rec3 = self._run([ok_result(b"BBB")])
        changed = [r for r in rec3
                   if r["endpoint"]["label"] == "homepage-candidate"][0]
        self.assertEqual(changed["outcome"], "REPRESENTATION_CHANGED")
        self.assertEqual(changed["baseline"]["run_id"], run1)
        self.assertEqual(first["content_sha256"],
                         changed["baseline"]["content_sha256"])

    def test_truncated_body_is_unknown_and_not_baseline(self):
        # 第一轮截断：comparison unknown、不进基线
        _, _run1, rec1 = self._run(
            [status_result(200, b"partial", truncated=True)])
        trunc = [r for r in rec1
                 if r["endpoint"]["label"] == "homepage-candidate"][0]
        self.assertEqual(trunc["comparison"], "unknown-truncated-body")
        self.assertTrue(trunc["body_truncated"])

        # 第二轮完整内容：无基线可比 → first-observation
        _, _run2, rec2 = self._run([ok_result(b"FULL")])
        full = [r for r in rec2
                if r["endpoint"]["label"] == "homepage-candidate"][0]
        self.assertEqual(full["comparison"], "first-observation")

        # 第三轮再截断：与未截断基线比较 → unknown-truncated-body
        _, _run3, rec3 = self._run(
            [status_result(200, b"partial", truncated=True)])
        again = [r for r in rec3
                 if r["endpoint"]["label"] == "homepage-candidate"][0]
        self.assertEqual(again["comparison"], "unknown-truncated-body")
        self.assertEqual(again["baseline"]["observation_id"],
                         full["observation_id"])

    def test_failed_fetch_retains_raw_and_robots_ref(self):
        from gleam.net import HopResult, origin_key

        class RetainingStub(StubFetcher):
            def fetch(self, url):
                self.calls.append(url)
                hop = HopResult(url=url, status=503,
                                pinned_ip="93.184.216.34",
                                headers={"x-test": "1"}, body=b"partial-5xx")
                return FetchOutcome(
                    ok=False, status=503, final_url=url, redirect_chain=[],
                    hops=[hop],
                    error={"kind": "http-5xx", "message": "server error"})

        store = RunStore(self.store_root)
        run_id = store.create_run()
        fetcher = RetainingStub([])
        fetcher._robots_bodies = {
            origin_key("https://example.org/pkgone/"): b"User-agent: *\nAllow: /"}
        records = observe.collect_observations(
            store, [self.pkg], fetcher, {},
            offline=False, run_id=run_id)
        home = [r for r in records
                if r["endpoint"]["label"] == "homepage-candidate"][0]
        run_dir = os.path.join(self.store_root, "runs", run_id)
        # 失败抓取的响应头与部分 body 仍保留
        with open(os.path.join(run_dir, home["raw_refs"]["body"]), "rb") as fh:
            self.assertEqual(fh.read(), b"partial-5xx")
        self.assertTrue(os.path.isfile(
            os.path.join(run_dir, home["raw_refs"]["headers"])))
        # robots 原文按 origin 哈希命名，并在观测里带引用
        key = origin_key("https://example.org/pkgone/")
        hashed = "robots-" + hashlib.sha256(key.encode()).hexdigest()[:16] + ".txt"
        self.assertTrue(os.path.isfile(os.path.join(run_dir, "raw", hashed)))
        self.assertEqual(home["raw_refs"]["robots"],
                         {key: f"raw/{hashed}"})


if __name__ == "__main__":
    unittest.main()
