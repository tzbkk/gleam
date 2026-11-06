import unittest

from gleam.report import _md_escape, build_summary, render_markdown


def make_record(**overrides):
    rec = {
        "observation_id": "obs-0001",
        "package": "pkgx",
        "endpoint": {"url": "https://x.example/a", "label": "homepage-candidate",
                     "kind": "web", "origin": "pkgx:udd:sources:homepage"},
        "observed_at": "2026-11-01T00:00:00Z",
        "outcome": "OK",
        "reason": None,
        "status_code": 200,
        "final_url": "https://x.example/a",
        "redirect_chain": [],
        "content_sha256": "a" * 64,
        "content_bytes": 3,
        "body_truncated": False,
        "effective_hash_from_baseline": False,
        "error": None,
        "robots": None,
        "baseline": None,
        "comparison": "first-observation",
    }
    rec.update(overrides)
    return rec


LIVE_PROVENANCE = {
    "kind": "udd-live",
    "dsn_display": "postgresql://udd-mirror.debian.net/udd",
    "release": "sid",
    "qa_filter": ("maintainer='Debian QA Group <packages@qa.debian.org>' "
                  "AND release='sid'"),
    "template_versions": {"facts": "udd-fetch-templates-v2",
                          "cohort": "udd-cohort-templates-v1"},
    "selection": {"mode": "named-fast-path", "cohort_queried": False},
    "packages": {
        "pkgx": {
            "kind": "udd-live",
            "fetched_at": "2026-11-03T00:00:00Z",
            "response_sha256": "b" * 64,
            "template_version": "udd-fetch-templates-v2",
            "row_counts": {"source": 1, "upstream": 1, "metadata": 2,
                           "packaging": 1},
        },
    },
    "notes": [],
}


class MarkdownEscapingTest(unittest.TestCase):
    def render(self, records):
        summary = build_summary(
            run_id="r", started_at="t0", finished_at="t1",
            mode={"offline": False, "no_model": True}, parameters={},
            input_provenance=LIVE_PROVENANCE,
            packages=[], records=records, counts={"by_outcome": {}},
            model_calls=[])
        return render_markdown(summary, records, [])

    def test_pipe_in_url_and_label_escaped(self):
        md = self.render([make_record(
            package="pkgx",
            endpoint={"url": "https://x.example/a|injected|col",
                      "label": "homepage-candidate|\nevil",
                      "kind": "web", "origin": "o"})])
        row = [line for line in md.splitlines()
               if line.startswith("| `obs-0001`")][0]
        # 未转义的原始管道不得出现在表格行
        self.assertNotIn("a|injected|col", row)
        self.assertIn("a\\|injected\\|col", row)
        self.assertNotIn("\n", row)

    def test_long_cell_truncation_disclosed_with_ellipsis(self):
        # 长单元格截断必须以可见省略号披露；管道转义先于截断
        long_url = "https://x.example/|pipe/" + "a" * 300
        md = self.render([make_record(
            endpoint={"url": long_url, "label": "homepage-candidate",
                      "kind": "web", "origin": "o"})])
        row = [line for line in md.splitlines()
               if line.startswith("| `obs-0001`")][0]
        self.assertIn("…", row)
        self.assertIn("x.example/\\|pipe/", row)
        self.assertNotIn("x.example/|pipe", row)


class MdEscapeTest(unittest.TestCase):
    def render(self, records):
        summary = build_summary(
            run_id="r", started_at="t0", finished_at="t1",
            mode={"offline": False, "no_model": True}, parameters={},
            input_provenance=LIVE_PROVENANCE,
            packages=[], records=records, counts={"by_outcome": {}},
            model_calls=[])
        return render_markdown(summary, records, [])

    def test_short_cells_unchanged_no_ellipsis(self):
        self.assertEqual(_md_escape("plain cell"), "plain cell")
        self.assertEqual(_md_escape(None), "")
        self.assertEqual(_md_escape(""), "")

    def test_boundary_160_not_truncated(self):
        self.assertEqual(_md_escape("x" * 160), "x" * 160)
        self.assertNotIn("…", _md_escape("x" * 160))

    def test_long_cell_truncated_with_visible_ellipsis(self):
        cell = _md_escape("x" * 300)
        self.assertTrue(cell.endswith("…"))
        self.assertEqual(len(cell), 161)  # 160 字符 + 省略号

    def test_newlines_flattened_before_truncation(self):
        cell = _md_escape("line\n" * 100)
        self.assertNotIn("\n", cell)
        self.assertTrue(cell.endswith("…"))

    def test_http_error_row_rendered(self):
        md = self.render([make_record(outcome="HTTP_ERROR", status_code=404,
                                      comparison=None)])
        self.assertIn("HTTP_ERROR", md)

    def test_packaging_provenance_wording_not_categorical(self):
        md = self.render([])
        self.assertIn("packaging provenance", md)
        self.assertIn("可能共址", md)
        self.assertNotIn("不是上游。", md)

    def test_live_udd_provenance_rendered_no_snapshot_claim(self):
        md = self.render([])
        self.assertIn("udd-live", md)
        self.assertIn("postgresql://udd-mirror.debian.net/udd", md)
        self.assertIn("fetched_at 2026-11-03T00:00:00Z", md)
        self.assertIn("named-fast-path", md)
        # 报告不得再声称快照输入
        self.assertNotIn("输入快照", md)
        self.assertNotIn("pulled_at", md)
        # 归档 raw 与 run raw 的区分必须出现
        self.assertIn("归档快照，不是本次运行的输入", md)

    def test_cached_offline_provenance_disclosed(self):
        prov = {
            **LIVE_PROVENANCE,
            "kind": "udd-cache",
            "cache": {"root": "/tmp/facts", "mode": "read-only (offline)"},
            "packages": {
                "pkgx": {
                    **LIVE_PROVENANCE["packages"]["pkgx"],
                    "kind": "udd-cache",
                    "cache": {"root": "/tmp/facts",
                              "written_at": "2026-11-03T01:00:00Z",
                              "original_fetched_at": "2026-11-03T00:00:00Z"},
                },
            },
        }
        summary = build_summary(
            run_id="r", started_at="t0", finished_at="t1",
            mode={"offline": True, "no_model": True}, parameters={},
            input_provenance=prov,
            packages=[], records=[], counts={"by_outcome": {}},
            model_calls=[])
        md = render_markdown(summary, [], [])
        self.assertIn("offline：读取显式 --facts-cache", md)
        self.assertIn("离线缓存（原始 fetched_at 2026-11-03T00:00:00Z", md)


if __name__ == "__main__":
    unittest.main()
