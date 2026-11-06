"""历史归档快照读取器（gleam.snapshot.load_snapshot）测试。

load_snapshot 仅用于读取仓库根 raw/ 的 2026-10-29 历史存档（遗留分析
工具），不再是活跃 CLI 的输入（活跃路径 = live UDD，见 test_liveudd.py
与 test_cli.py 的 no-root-raw 测试）。本文件保留以锁定归档读取器的
语义（多行/重复行/逐行出处/坏名致命）。
"""

import os
import tempfile
import unittest

from gleam.snapshot import load_snapshot

from .fixtures import write_fixture


class SnapshotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.raw = write_fixture(os.path.join(cls.tmp.name, "raw"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def load(cls):
        return load_snapshot(cls.raw)

    def test_multiline_and_duplicate_rows_preserved(self):
        snap = self.load()
        pkg = snap.packages["pkgone"]
        source_rows = [f for f in pkg.rows if f.table == "sources.csv"]
        self.assertEqual(len(source_rows), 2)
        self.assertEqual(sorted(f.row_number for f in source_rows), [1, 2])
        self.assertEqual(pkg.versions, ["1.0-1", "1.1-1"])
        multiline = source_rows[0].fields["files"]
        self.assertIn("\n", multiline)
        watch = pkg.first("upstream.csv", "watch_file")
        self.assertIn("\n", watch)
        self.assertIn("version=4", watch)

    def test_upstream_metadata_long_form_grouped(self):
        snap = self.load()
        pkg = snap.packages["pkgone"]
        keys = {(f.fields["key"], f.fields["value"]) for f in pkg.rows
                if f.table == "upstream_metadata.csv"}
        self.assertIn(("Repository", "https://git.example.org/pkgone.git"), keys)
        self.assertIn(("Repository-Browse", "https://git.example.org/pkgone"), keys)

    def test_homepage_and_dep12_labelled_separately(self):
        snap = self.load()
        labels = {(c.url, c.label, c.kind, c.fetch)
                  for c in snap.packages["pkgone"].upstream_candidates()}
        self.assertIn(("https://example.org/pkgone/",
                       "homepage-candidate", "web", True), labels)
        self.assertIn(("https://git.example.org/pkgone.git",
                       "dep12-repository", "git-endpoint", False), labels)
        self.assertIn(("https://git.example.org/pkgone",
                       "dep12-repository-browse", "web", True), labels)
        self.assertIn(("https://example.org/pkgone/pkgone-1.0.tar.gz",
                       "upstream-artifact", "artifact", False), labels)

    def test_packaging_vcs_not_upstream(self):
        snap = self.load()
        pkg = snap.packages["pkgone"]
        vcs_urls = {v["url"] for v in pkg.packaging_vcs()}
        self.assertTrue(vcs_urls, "packaging VCS should be recorded")
        upstream_urls = {c.url for c in pkg.upstream_candidates()}
        self.assertFalse(vcs_urls & upstream_urls,
                         "packaging VCS must never be an upstream candidate")
        for v in pkg.packaging_vcs():
            self.assertIn("packaging provenance", v["note"])
            self.assertIn("not proof of upstream", v["note"])

    def test_pkgtwo_without_homepage_has_no_web_candidate(self):
        snap = self.load()
        cands = snap.packages["pkgtwo"].upstream_candidates()
        self.assertEqual(cands, [])

    def test_manifest_and_input_hashes_are_provenance(self):
        snap = self.load()
        prov = snap.provenance_summary()
        self.assertEqual(prov["manifest"]["pulled_at"],
                         "2026-10-29T06:44:28Z")
        self.assertEqual(len(prov["input_hashes"]), 4)
        for table, digest in prov["input_hashes"].items():
            self.assertEqual(len(digest), 64, f"{table} sha256")
        self.assertEqual(snap.row_counts["sources.csv"], 4)

    def test_select_missing_package_raises(self):
        snap = self.load()
        with self.assertRaises(KeyError):
            snap.select(["no-such-package"])

    def test_select_limit_deterministic_order(self):
        snap = self.load()
        names = [p.name for p in snap.select(limit=2)]
        self.assertEqual(names, ["pkgone", "pkgthree"])

    def test_missing_tables_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                load_snapshot(tmp)

    def test_invalid_source_name_is_fatal_not_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = write_fixture(os.path.join(tmp, "raw"))
            with open(os.path.join(raw, "sources.csv"), "a",
                      encoding="utf-8") as fh:
                fh.write("Bad_Pkg!,1.0-1,,,,,,,,,,\n")
            with self.assertRaises(ValueError) as ctx:
                load_snapshot(raw)
            self.assertIn("Bad_Pkg!", str(ctx.exception))
            self.assertIn("Debian policy", str(ctx.exception))

    def test_empty_source_name_is_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = write_fixture(os.path.join(tmp, "raw"))
            with open(os.path.join(raw, "sources.csv"), "a",
                      encoding="utf-8") as fh:
                fh.write(",1.0-1,,,,,,,,,,\n")
            with self.assertRaises(ValueError):
                load_snapshot(raw)

    def test_provenance_uses_logical_row_not_physical_line(self):
        snap = self.load()
        pkg = snap.packages["pkgone"]
        source_rows = [f for f in pkg.rows if f.table == "sources.csv"]
        # 第 1 个数据行含多行字段（占 3 个物理行），出处仍是 #row=1
        self.assertEqual(source_rows[0].provenance, "sources.csv#row=1")
        self.assertEqual(source_rows[1].provenance, "sources.csv#row=2")

    def test_select_dedups_repeated_names(self):
        snap = self.load()
        picked = snap.select(["pkgone", "pkgone", "pkgthree", "pkgone"])
        self.assertEqual([p.name for p in picked], ["pkgone", "pkgthree"])


if __name__ == "__main__":
    unittest.main()
