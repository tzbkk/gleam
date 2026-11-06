"""抽取层测试：trafilatura/回退标注、清理前出处捕获、README 解码。

trafilatura 真实抽取用例仅在已安装时运行（skipUnless）；缺失环境通过
mock HAVE_TRAFILATURA=False 覆盖回退标注语义。全部离线。
"""

import base64
import unittest
from unittest import mock

from gleam import extract

HTML = b"""<html><head><title>librcd page</title></head><body>
<nav>Menu Home About</nav>
<main><h1>librcd</h1>
<p>librcd is a character conversion library used by RusXMMS.</p>
<p>Author: RusXMMS project team, maintained since 2001.</p>
<a href="/downloads.html">downloads</a>
<a href="https://mirror.example.org/librcd">mirror</a>
<a href="mailto:maintainer@example.org">contact maintainer</a>
<a href="https://mirror.example.org/librcd">mirror again</a>
</main>
<footer>(c) 2001-2020 RusXMMS contributors; all rights reserved</footer>
</body></html>"""


class ProvenanceCaptureTest(unittest.TestCase):
    """boilerplate 清理前的独立出处捕获（trafilatura 可用与否都要成立）。"""

    def test_anchors_mailto_footer_copyright_captured(self):
        rich = extract.extract_rich(HTML, "text/html; charset=utf-8",
                                    "https://up.example.org/librcd/")
        anchors = [(a["text"], a["url"]) for a in rich["anchors"]]
        # href="/downloads.html" 是根绝对路径：按 URL 语义解析到站点根
        self.assertIn(("downloads", "https://up.example.org/downloads.html"),
                      anchors)
        self.assertIn(("mirror", "https://mirror.example.org/librcd"),
                      anchors)
        self.assertIn("maintainer@example.org", rich["mailtos"])
        joined = " ".join(rich["footer_lines"])
        self.assertIn("RusXMMS contributors", joined)
        self.assertTrue(any("2001" in line or "(c)" in line.lower()
                            for line in rich["copyright_lines"]))
        # 链接去重保序（同 URL 两次只留一次）
        self.assertEqual(
            rich["links"],
            ["https://up.example.org/downloads.html",
             "https://mirror.example.org/librcd"])

    def test_relative_anchor_resolved_against_base(self):
        rich = extract.extract_rich(
            b"<html><body><a href='../deep/x.html'>x</a></body></html>",
            "text/html", "https://a.example/p/q/")
        self.assertEqual(rich["anchors"][0]["url"],
                         "https://a.example/p/deep/x.html")

    def test_credential_and_nonweb_anchors_dropped(self):
        rich = extract.extract_rich(
            b"<html><body><a href='http://u:p@evil.example/'>e</a>"
            b"<a href='ftp://files.example/x'>f</a></body></html>",
            "text/html", "https://a.example/")
        self.assertEqual(rich["links"], [])
        self.assertEqual([a["url"] for a in rich["anchors"]
                          if a["url"]], [])


class FallbackLabelingTest(unittest.TestCase):
    def test_missing_trafilatura_labeled_fallback(self):
        with mock.patch.object(extract, "HAVE_TRAFILATURA", False), \
                mock.patch.object(extract, "trafilatura", None):
            rich = extract.extract_rich(HTML, "text/html",
                                        "https://up.example.org/")
        self.assertEqual(rich["extractor"], "fallback-stdlib")
        self.assertTrue(rich["text"])
        self.assertTrue(any("trafilatura not installed" in note
                            for note in rich["notes"]))
        # 出处信号在回退路径同样保留
        self.assertIn("maintainer@example.org", rich["mailtos"])

    def test_trafilatura_empty_output_labeled_fallback_empty(self):
        with mock.patch.object(extract, "HAVE_TRAFILATURA", True), \
                mock.patch.object(
                    extract, "trafilatura",
                    mock.Mock(extract_metadata=mock.Mock(
                        return_value=None),
                        extract=mock.Mock(return_value=None))):
            rich = extract.extract_rich(
                b"<html><body><p>visible text stays</p></body></html>",
                "text/html", "https://a.example/")
        self.assertEqual(rich["extractor"], "fallback-empty-stdlib")
        self.assertIn("visible text stays", rich["text"])

    def test_trafilatura_exception_labeled_fallback(self):
        boom = mock.Mock(extract_metadata=mock.Mock(
            side_effect=RuntimeError("meta boom")),
            extract=mock.Mock(side_effect=RuntimeError("boom")))
        with mock.patch.object(extract, "HAVE_TRAFILATURA", True), \
                mock.patch.object(extract, "trafilatura", boom):
            rich = extract.extract_rich(
                b"<html><body><p>still readable</p></body></html>",
                "text/html", "https://a.example/")
        self.assertEqual(rich["extractor"], "fallback-empty-stdlib")
        self.assertIn("still readable", rich["text"])


@unittest.skipUnless(extract.HAVE_TRAFILATURA,
                     "trafilatura not installed in this environment")
class TrafilaturaRealTest(unittest.TestCase):
    def test_real_extraction_labels_and_metadata(self):
        rich = extract.extract_rich(HTML, "text/html; charset=utf-8",
                                    "https://up.example.org/librcd/")
        self.assertEqual(rich["extractor"], "trafilatura")
        self.assertIn("character conversion library", rich["text"])
        self.assertTrue(rich["metadata"], "metadata fields expected")
        # 清理后正文仍在；出处锚点独立保留（不因清理丢失）
        self.assertTrue(rich["anchors"])

    def test_short_author_page_not_hidden(self):
        short = (b"<html><head><title>Author page</title></head><body>"
                 b"<p>Jane Doe, librcd maintainer.</p></body></html>")
        rich = extract.extract_rich(short, "text/html", "https://a.example/")
        self.assertTrue(rich["text"].strip(),
                        "short pages must not be hidden")


class PassthroughTest(unittest.TestCase):
    def test_json_untouched(self):
        payload = b'{"results": [{"n": 1.5, "s": "caf\\u00e9"}]}'
        rich = extract.extract_rich(payload, "application/json",
                                    "https://a.example/x")
        self.assertEqual(rich["kind"], "json")
        self.assertEqual(rich["text"], payload.decode())
        self.assertEqual(rich["extractor"], "passthrough")

    def test_plain_text_untouched(self):
        payload = b"plain text line\nsecond line\n"
        rich = extract.extract_rich(payload, "text/plain; charset=utf-8",
                                    "https://a.example/x")
        self.assertEqual(rich["kind"], "text")
        self.assertEqual(rich["text"], payload.decode())

    def test_empty_body(self):
        rich = extract.extract_rich(b"", None, "https://a.example/")
        # 空 body + 无 content-type 走 HTML 分支（与旧 extract_page 语义
        # 一致）：文本为空、无链接
        self.assertEqual(rich["text"], "")
        self.assertEqual(rich["links"], [])


class DecodeReadmeTest(unittest.TestCase):
    def test_base64_roundtrip(self):
        text = "# librcd\n\nREADME body with 中文 contents.\n"
        payload = {"encoding": "base64",
                   "content": base64.b64encode(text.encode()).decode()}
        out, mode = extract.decode_readme(payload, cap=1000)
        self.assertEqual(out, text)
        self.assertEqual(mode, "decoded-from-base64")

    def test_cap_truncation_disclosed(self):
        payload = {"encoding": "base64",
                   "content": base64.b64encode(b"x" * 50).decode()}
        out, mode = extract.decode_readme(payload, cap=10)
        self.assertTrue(out.startswith("xxxxxxxxxx"))
        self.assertIn("truncated at 10", out)

    def test_missing_content(self):
        out, mode = extract.decode_readme({"encoding": "base64"}, 100)
        self.assertEqual(out, "")
        self.assertEqual(mode, "missing-content")


if __name__ == "__main__":
    unittest.main()
