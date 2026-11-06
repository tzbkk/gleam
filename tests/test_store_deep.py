import json
import os
import tempfile
import unittest

from gleam.store import RunStore, _write_json, verify_store

import hashlib


def make_record(run_id, obs_id, url="https://x.example/a", outcome="OK",
                observed_at="2026-11-01T00:00:00Z", sha=None, body_ref=None,
                body_truncated=False):
    return {
        "observation_id": obs_id,
        "run_id": run_id,
        "package": "pkgx",
        "endpoint": {"url": url, "label": "homepage-candidate",
                     "kind": "web", "origin": "pkgx:sources.csv:homepage"},
        "observed_at": observed_at,
        "outcome": outcome,
        "status_code": 200,
        "content_sha256": sha,
        "content_bytes": None,
        "body_truncated": body_truncated,
        "effective_hash_from_baseline": False,
        "raw_refs": ({"body": body_ref} if body_ref else {}),
    }


def seed_run(root, recs, summary_extra=None):
    store = RunStore(root)
    run_id = store.create_run()
    for rec in recs:
        store.write_observation(rec)
    store.write_summary(
        {"run_id": run_id,
         "observation_ids": [r["observation_id"] for r in recs],
         **(summary_extra or {})},
        "# report\n")
    return store, run_id


class SafeIdTest(unittest.TestCase):
    def test_writer_rejects_path_separators_and_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            store.create_run()
            for bad in ("../evil", "a/b", "a\\b", "..", ".", ".hidden",
                        "a:b"):
                with self.subTest(bad=bad):
                    with self.assertRaises(ValueError):
                        store.write_raw(bad, b"x")
                    with self.assertRaises(ValueError):
                        store.write_observation(
                            make_record(store.run_id, bad))
                    with self.assertRaises(ValueError):
                        store.write_model_call({"call_id": bad})
            self.assertEqual(os.listdir(os.path.join(store.run_dir, "raw")),
                             [], "no partial writes may survive")

    def test_run_id_validated_in_writers(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            store.create_run()
            store.run_id = "../../etc"
            with self.assertRaises(ValueError):
                store.write_raw("ok.body", b"x")


class StrictJsonTest(unittest.TestCase):
    def test_write_json_rejects_nan_and_inf(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.json")
            for bad in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(bad=bad):
                    with self.assertRaises(ValueError):
                        _write_json(path, {"v": bad})
            self.assertEqual(os.listdir(tmp), [],
                             "失败的写入不得留下临时文件")

    def test_overflow_1e999_text_rejected_by_models_strict_loads(self):
        from gleam.models import _loads_strict
        with self.assertRaises(ValueError):
            _loads_strict('{"p": 1e999}')
        with self.assertRaises(ValueError):
            _loads_strict('{"p": NaN}')
        with self.assertRaises(ValueError):
            _loads_strict('{"p": Infinity}')
        self.assertEqual(_loads_strict('{"p": 0.5}')["p"], 0.5)


class IndexConcurrencyTest(unittest.TestCase):
    def test_concurrent_update_does_not_lose_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_a = RunStore(tmp)
            store_b = RunStore(tmp)
            _, run1 = seed_run(tmp, [make_record("r1", "obs-0001",
                                                 url="https://a.example/")])
            rec1 = make_record(run1, "obs-0001", url="https://a.example/")
            store_a.update_index([rec1])
            # 另一进程在锁内先重读再合并：A 的陈旧内存态不得丢掉 B 的新基线
            _, run2 = seed_run(tmp, [make_record("r2", "obs-0001",
                                                 url="https://b.example/",
                                                 observed_at="2026-11-02T00:00:00Z")])
            rec2 = make_record(run2, "obs-0001", url="https://b.example/",
                               observed_at="2026-11-02T00:00:00Z")
            store_b.update_index([rec2])
            store_a.update_index([make_record(run1, "obs-0002",
                                              url="https://c.example/")])
            endpoints = RunStore(tmp).load_index()["endpoints"]
            for url in ("https://a.example/", "https://b.example/",
                        "https://c.example/"):
                self.assertIn(url, endpoints)

    def test_missing_index_authoritative_scan_finds_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = "https://a.example/"
            store1 = RunStore(tmp)
            run1 = store1.create_run()
            store1.write_observation(make_record(
                run1, "obs-0001", url=url,
                observed_at="2026-11-01T00:00:00Z", sha="0" * 64))
            store1.write_summary({"run_id": run1,
                                  "observation_ids": ["obs-0001"]}, "# r\n")
            store2 = RunStore(tmp)
            run2 = store2.create_run()
            store2.write_observation(make_record(
                run2, "obs-0001", url=url,
                observed_at="2026-11-05T00:00:00Z", sha="deadbeef" * 8))
            store2.write_summary({"run_id": run2,
                                  "observation_ids": ["obs-0001"]}, "# r\n")
            index_path = os.path.join(tmp, "index.json")
            if os.path.exists(index_path):
                os.remove(index_path)
            hit = RunStore(tmp).find_baseline(url)
            self.assertIsNotNone(hit)
            self.assertEqual(hit["run_id"], run2)
            self.assertEqual(hit["content_sha256"], "deadbeef" * 8)

    def test_truncated_observations_never_become_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = "https://t.example/"
            store1 = RunStore(tmp)
            run1 = store1.create_run()
            store1.write_observation(make_record(
                run1, "obs-0001", url=url,
                observed_at="2026-11-01T00:00:00Z", sha="a" * 64))
            store1.write_summary({"run_id": run1,
                                  "observation_ids": ["obs-0001"]}, "# r\n")
            store2 = RunStore(tmp)
            run2 = store2.create_run()
            store2.write_observation(make_record(
                run2, "obs-0001", url=url, outcome="REPRESENTATION_CHANGED",
                observed_at="2026-11-02T00:00:00Z", sha="b" * 64,
                body_truncated=True))
            store2.write_summary({"run_id": run2,
                                  "observation_ids": ["obs-0001"]}, "# r\n")
            hit = RunStore(tmp).rebuild_index()["endpoints"].get(url)
            self.assertEqual(hit["run_id"], run1,
                             "截断观测不得覆盖未截断基线")


class DeepVerifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_corrupt_observation_detected(self):
        store, run_id = seed_run(self.root, [
            make_record("r", "obs-0001", sha="z" * 64)])
        with open(os.path.join(store.run_dir, "observations",
                               "obs-0001.json"), "w") as fh:
            fh.write("{not json")
        problems = verify_store(self.root)
        self.assertTrue(any("corrupt" in p for p in problems))

    def test_wrong_run_id_in_observation_detected(self):
        store, run_id = seed_run(self.root, [
            make_record("other-run", "obs-0001")])
        problems = verify_store(self.root)
        self.assertTrue(any("claims run_id" in p for p in problems))

    def test_wrong_observation_id_detected(self):
        store, run_id = seed_run(self.root, [
            make_record("r", "obs-0002")])
        # summary 改为引用 obs-0001，而 obs-0001.json 内部的 id 是 obs-0002
        summary_path = os.path.join(store.run_dir, "run.json")
        with open(summary_path) as fh:
            summary = json.load(fh)
        summary["observation_ids"] = ["obs-0001"]
        with open(summary_path, "w") as fh:
            json.dump(summary, fh)
        with open(os.path.join(store.run_dir, "observations",
                               "obs-0001.json"), "w") as fh:
            json.dump(make_record("r", "obs-0002"), fh)
        problems = verify_store(self.root)
        self.assertTrue(any("has id" in p for p in problems))

    def test_unknown_outcome_token_detected(self):
        store, run_id = seed_run(self.root, [
            make_record("r", "obs-0001", outcome="ALIVE")])
        problems = verify_store(self.root)
        self.assertTrue(any("unknown outcome" in p for p in problems))

    def test_corrupted_raw_body_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            store.create_run()
            body = b"CONTENT-BYTES"
            sha = hashlib.sha256(body).hexdigest()
            rec = make_record(store.run_id, "obs-0001", sha=sha,
                              body_ref="raw/obs-0001.body")
            store.write_raw("obs-0001.body", body)
            store.write_observation(rec)
            store.write_summary({"run_id": store.run_id,
                                 "observation_ids": ["obs-0001"]}, "# r\n")
            with open(os.path.join(store.run_dir, "raw",
                                   "obs-0001.body"), "wb") as fh:
                fh.write(b"CORRUPTED-BYTES")
            problems = verify_store(tmp)
            self.assertTrue(any("raw body hash mismatch" in p
                                for p in problems))

    def test_index_content_mismatch_detected(self):
        store, run_id = seed_run(self.root, [
            make_record("r", "obs-0001", url="https://i.example/",
                        sha="a" * 64)])
        store.update_index([make_record(run_id, "obs-0001",
                                        url="https://i.example/",
                                        sha="a" * 64)])
        index_path = os.path.join(self.root, "index.json")
        with open(index_path) as fh:
            index = json.load(fh)
        index["endpoints"]["https://i.example/"]["content_sha256"] = "f" * 64
        with open(index_path, "w") as fh:
            json.dump(index, fh)
        problems = verify_store(self.root)
        self.assertTrue(any("out of sync" in p for p in problems))

    def test_index_stale_entry_survives_corrupt_observation_no_crash(self):
        store, run_id = seed_run(self.root, [
            make_record("r", "obs-0001", url="https://i.example/",
                        sha="a" * 64)])
        store.update_index([make_record(run_id, "obs-0001",
                                        url="https://i.example/",
                                        sha="a" * 64)])
        with open(os.path.join(store.run_dir, "observations",
                               "obs-0001.json"), "w") as fh:
            fh.write("{not json")
        problems = verify_store(self.root)
        self.assertTrue(any("corrupt" in p for p in problems))
        self.assertTrue(any("out of sync" in p for p in problems))

    def test_index_stale_entry_survives_deleted_observation_no_crash(self):
        store, run_id = seed_run(self.root, [
            make_record("r", "obs-0001", url="https://i.example/",
                        sha="a" * 64)])
        store.update_index([make_record(run_id, "obs-0001",
                                        url="https://i.example/",
                                        sha="a" * 64)])
        os.remove(os.path.join(store.run_dir, "observations",
                               "obs-0001.json"))
        problems = verify_store(self.root)
        self.assertTrue(any("file missing" in p for p in problems))
        self.assertTrue(any("out of sync" in p for p in problems))

    def test_clean_store_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            store.create_run()
            body = b"fine"
            rec = make_record(store.run_id, "obs-0001",
                              sha=hashlib.sha256(body).hexdigest(),
                              body_ref="raw/obs-0001.body")
            store.write_raw("obs-0001.body", body)
            store.write_observation(rec)
            store.write_summary({"run_id": store.run_id,
                                 "observation_ids": ["obs-0001"]}, "# r\n")
            problems = verify_store(tmp)
            self.assertEqual([p for p in problems if "out of sync" in p], [])


if __name__ == "__main__":
    unittest.main()
