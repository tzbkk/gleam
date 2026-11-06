"""Debian BTS 讨论抓取（bug_fetch）测试：合成夹具 + 假连接，全部离线。

覆盖：严格数字 bug_id 校验、URL/信封由应用侧构造、mbox/SOAP 解析（多部分/
字符集/quoted-printable/base64/附件仅元数据）、挑战检测与有界解答、cookie
脱敏、robots 门槛、预算/限速/重定向/429/404/soap-fault 的诚实分级、
investigate 工具集成与共享预算。
"""

import base64
import json
import quopri
import time
import unittest
from unittest import mock

from gleam import bts
from gleam.agent import InvestigationContext, TOOL_SPECS
from gleam.cli import EXIT_OK, EXIT_USAGE, build_parser, cmd_bug_fetch
from gleam.net import FetchOutcome
from gleam.store import RunStore

import tempfile


def make_policy(**overrides):
    policy = dict(bts.DEFAULT_POLICY)
    policy.update(overrides)
    return policy


# ---- 合成 mbox 夹具：QP+UTF-8 正文含 URL、koi8-r 多部分 + base64 附件、HTML-only ----

UTF8_QP_BODY = quopri.encodestring(
    "上游迁移到 https://git.example.org/pkgone.git 了\n"
    "镜像: https://mirror.example.net/pkgone/\n".encode("utf-8"))

ATTACHMENT_BYTES = b"diff --git a/x b/x\n--- harmless textual patch ---\n"

KOI8_TEXT = "Текст в KOI8-R кодировке".encode("koi8-r")

MBOX_FIXTURE = (
    b"From submitter@example.org Mon Nov 02 10:00:00 2026\n"
    b"Message-ID: <msg-one@example.org>\n"
    b"Date: Mon, 2 Nov 2026 10:00:00 +0000\n"
    b"From: =?utf-8?q?Submitter?= <submitter@example.org>\n"
    b"Subject: =?utf-8?q?package_orphaned=E5=8C=85?=\n"
    b"MIME-Version: 1.0\n"
    b"Content-Type: text/plain; charset=utf-8\n"
    b"Content-Transfer-Encoding: quoted-printable\n"
    b"\n" + UTF8_QP_BODY + b"\n"
    b"From maintainer@example.org Mon Nov 02 11:00:00 2026\n"
    b"Message-ID: <msg-two@example.org>\n"
    b"Date: Mon, 2 Nov 2026 11:00:00 +0000\n"
    b"From: Maintainer <maintainer@example.org>\n"
    b"Subject: patch attached\n"
    b"MIME-Version: 1.0\n"
    b"Content-Type: multipart/mixed; boundary=\"BOUND\"\n"
    b"\n"
    b"--BOUND\n"
    b"Content-Type: text/plain; charset=koi8-r\n"
    b"Content-Transfer-Encoding: 8bit\n"
    b"\n" + KOI8_TEXT + b"\n"
    b"--BOUND\n"
    b"Content-Type: text/x-diff; name=\"fix.patch\"\n"
    b"Content-Transfer-Encoding: base64\n"
    b"Content-Disposition: attachment; filename=\"fix.patch\"\n"
    b"\n" + base64.encodebytes(ATTACHMENT_BYTES) +
    b"--BOUND--\n"
    b"From bot@example.org Mon Nov 02 12:00:00 2026\n"
    b"Message-ID: <msg-three@example.org>\n"
    b"Date: Mon, 2 Nov 2026 12:00:00 +0000\n"
    b"From: Bot <bot@example.org>\n"
    b"Subject: html only\n"
    b"MIME-Version: 1.0\n"
    b"Content-Type: text/html; charset=utf-8\n"
    b"\n"
    b"<html><body><p>see https://tracker.example.org/pkgone</p>"
    b"</body></html>\n"
)


def soap_item(header: str, body_text: str, msg_num: int) -> str:
    def esc(s):
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;"))
    return (f"<item><header xsi:type=\"xsd:string\">{esc(header)}"
            f"</header><body xsi:type=\"xsd:string\">{esc(body_text)}"
            f"</body><msg_num xsi:type=\"xsd:int\">{msg_num}</msg_num>"
            f"<attachments soapenc:arrayType=\"xsd:anyType[0]\" "
            f"xsi:type=\"soapenc:Array\"/></item>")


SOAP_ENVELOPE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<soap:Envelope soap:encodingStyle='
    '"http://schemas.xmlsoap.org/soap/encoding/" '
    'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
    'xmlns:soapenc="http://schemas.xmlsoap.org/soap/encoding/" '
    'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
    '<soap:Body><get_bug_logResponse xmlns="urn:Debbugs/SOAP">'
    '<soapenc:Array soapenc:arrayType="xsd:anyType[2]" '
    'xsi:type="soapenc:Array">'
    + soap_item("Received: (at submit) by bugs.debian.org; 2 Nov 2026\n"
                "From submitter@example.org Mon Nov 02 10:00:00 2026\n"
                "Message-ID: <msg-one@example.org>\n"
                "Date: Mon, 2 Nov 2026 10:00:00 +0000\n"
                "From: Submitter <submitter@example.org>\n"
                "Subject: orphan request\n"
                "Content-Type: text/plain; charset=utf-8\n",
                "body one with https://upstream.example.org/new-home\n",
                5)
    + soap_item("Received: (at control) by bugs.debian.org; 2 Nov 2026\n"
                "From control@example.org Mon Nov 02 11:00:00 2026\n"
                "Message-ID: <msg-two@example.org>\n"
                "From: Control <control@example.org>\n"
                "Subject: retitle\n",
                "body two plain", 7)
    + "</soapenc:Array></get_bug_logResponse></soap:Body></soap:Envelope>")


SOAP_FAULT = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
    '<soap:Body><soap:Fault><faultcode>soap:Server</faultcode>'
    '<faultstring>Bug#999999999 does not exist</faultstring>'
    '</soap:Fault></soap:Body></soap:Envelope>')


ANUBIS_CHALLENGE_PAGE = (
    b"<!DOCTYPE html><html><head><title>Making sure you're not a bot"
    b"</title></head><body>"
    b'<script id="anubis_challenge_data" type="application/json">'
    + json.dumps({"challenge": "abc123", "difficulty": 1}).encode()
    + b"</script>"
    b"<p>proof-of-work challenge</p></body></html>")

VARNISH_CHALLENGE_PAGE = (
    b"<!DOCTYPE html>\n"
    b"<!-- Adapted from haphash for Varnish PoW -->\n"
    b'<title>I Challenge Thee</title>\n'
    b"<script>\n"
    b'  const match = document.cookie.match(/pow_challenge=([^;]+)/);\n'
    b'  document.cookie = "pow_nonce=" + tries + "; path=/; max-age=3000";\n'
    b"</script>\n")


# ---- 离线：校验与构造 -------------------------------------------------------

class ValidationTest(unittest.TestCase):
    def test_strict_numeric_only(self):
        self.assertEqual(bts.validate_bug_id(620025), 620025)
        self.assertEqual(bts.validate_bug_id("620025"), 620025)
        for bad in (0, -1, 10000000, "0620025", "620025a", " 620025",
                    "6200 25", "", None, True, False, 620025.0, b"620025",
                    "../etc", "620025&mbox=yes", "1;evil"):
            with self.assertRaises(bts.BtsError, msg=repr(bad)):
                bts.validate_bug_id(bad)

    def test_mbox_url_app_constructed(self):
        self.assertEqual(
            bts.build_mbox_url(620025),
            "https://bugs.debian.org/cgi-bin/bugreport.cgi?bug=620025"
            "&mbox=yes")

    def test_host_allowlist(self):
        for bad in ("https://evil.example", "https://bugs.debian.org.evil.io",
                    "http://127.0.0.1:8888", "https://bugs.debian.org/prefix"):
            with self.assertRaises(bts.BtsError):
                bts.build_mbox_url(1, bad)

    def test_soap_envelope_contains_only_number(self):
        envelope = bts.build_soap_get_bug_log(620025).decode()
        self.assertIn("get_bug_log", envelope)
        self.assertIn(">620025<", envelope)
        self.assertNotIn("&", envelope.split("Body")[1].split("Body")[0]
                         .replace("&lt;", ""))


# ---- 离线：mbox / SOAP 解析 --------------------------------------------------

class MboxParseTest(unittest.TestCase):
    def test_three_messages_charsets_attachments_urls(self):
        msgs, meta = bts.parse_mbox_messages(
            MBOX_FIXTURE, source_url="https://bugs.debian.org/x",
            policy=make_policy())
        self.assertEqual(len(msgs), 3)
        self.assertEqual(meta["source_format"], "mbox")
        first, second, third = msgs
        self.assertEqual(first["message_id"], "<msg-one@example.org>")
        self.assertIn("https://git.example.org/pkgone.git", first["urls"])
        self.assertIn("https://mirror.example.net/pkgone/", first["urls"])
        self.assertIn("上游迁移到", first["body_text"])
        self.assertEqual(first["charsets"], ["utf-8"])
        self.assertIn("package orphaned包",
                      first["subject_decoded"] or "")
        self.assertEqual(second["charsets"], ["koi8-r"])
        self.assertIn("Текст в KOI8-R кодировке", second["body_text"])
        attach = second["attachments"]
        self.assertEqual(len(attach), 1)
        self.assertEqual(attach[0]["filename"], "fix.patch")
        self.assertEqual(attach[0]["bytes"], len(ATTACHMENT_BYTES))
        self.assertNotIn("harmless textual patch", json.dumps(msgs))
        self.assertTrue(third["html_only"])
        self.assertIn("https://tracker.example.org/pkgone", third["urls"])
        offsets = [m["raw"]["offset"] for m in msgs]
        self.assertEqual(offsets, sorted(offsets))
        self.assertTrue(all(m["raw"]["boundary"] == "mbox-from-line"
                            for m in msgs))

    def test_message_cap_and_truncation(self):
        msgs, meta = bts.parse_mbox_messages(
            MBOX_FIXTURE, source_url="u", policy=make_policy(max_messages=2))
        self.assertEqual(len(msgs), 2)
        self.assertTrue(meta["notes"])
        msgs, _ = bts.parse_mbox_messages(
            MBOX_FIXTURE, source_url="u",
            policy=make_policy(max_msg_body_chars=10))
        self.assertTrue(msgs[0]["body_truncated"])
        self.assertIn("truncated at 10", msgs[0]["body_text"])

    def test_no_separator_blob(self):
        blob = b"Subject: x\n\nnot an mbox\n"
        msgs, meta = bts.parse_mbox_messages(
            blob, source_url="u", policy=make_policy())
        self.assertEqual(len(msgs), 1)
        self.assertIn("blob", meta["notes"][0])

    def test_html_script_style_stripped_at_call_site(self):
        # 回归：html-only 消息经真实调用路径（structure_message →
        # _strip_html_bounded）正确剥掉 script/style 块
        raw = (b"From a@b.c Mon Nov 02 10:00:00 2026\n"
               b"Content-Type: text/html; charset=utf-8\n\n"
               b"<html><head><style>body { color: red }</style>"
               b'<script type="text/javascript">var e = "<p>evil</p>";'
               b"</script></head><body><p>see https://keep.example/p"
               b"</p></body></html>\n")
        msg = bts.structure_message(raw, index=0, offset=0, source_url="u",
                                    policy=make_policy())
        self.assertTrue(msg["html_only"])
        self.assertIn("see", msg["body_text"])
        self.assertIn("https://keep.example/p", msg["urls"])
        self.assertNotIn("color: red", msg["body_text"])
        self.assertNotIn("evil", msg["body_text"])

    def test_deeply_nested_mime_is_parse_error_not_crash(self):
        # 回归：~2000 层嵌套 multipart 曾让 stdlib MIME 解析/walk 递归以
        # RecursionError 裸崩；现在解析入口转成诚实的 parse-error
        depth = 2000
        body = b"deepest plain text\n"
        for i in range(depth - 1, -1, -1):
            body = ((f"--B{i}\n"
                     f'Content-Type: multipart/mixed; boundary="B{i + 1}"'
                     f"\n\n").encode()
                    + body + f"\n--B{i}--\n".encode())
        raw = (b"Subject: deep\nMIME-Version: 1.0\n"
               b'Content-Type: multipart/mixed; boundary="B0"\n\n' + body)
        with self.assertRaises(bts.BtsError) as ctx:
            bts.parse_mbox_messages(raw, source_url="u",
                                    policy=make_policy())
        self.assertEqual(ctx.exception.kind, "parse-error")
        self.assertIn("RecursionError", ctx.exception.message)


class StripHtmlBoundedTest(unittest.TestCase):
    def test_script_style_blocks_stripped(self):
        html = ('<html><head><style>body { color: red }</style>'
                '<script type="text/javascript">var x = "<p>evil</p>";'
                '</script></head><body><p>visible &amp; fine '
                'https://a.example/x</p></body></html>')
        out = bts._strip_html_bounded(html, cap=500)
        self.assertIn("visible & fine", out)
        self.assertIn("https://a.example/x", out)
        self.assertNotIn("color: red", out)
        self.assertNotIn("evil", out)
        self.assertNotIn("<", out)

    def test_pathological_unclosed_script_fast_and_bounded(self):
        # 回归：调用方传入未预截断正文（可达 max_bytes），大量未闭合
        # <script 曾在全量输入上触发 O(k·n) 回溯；现在函数内先截 4*cap
        out = bts._strip_html_bounded("<script" * 20000, cap=200)
        self.assertLessEqual(len(out), 200)
        # 经 structure_message 的真实调用路径（html-only 消息）同样有界
        raw = (b"From a@b.c Mon Nov 02 10:00:00 2026\n"
               b"Content-Type: text/html; charset=utf-8\n\n"
               + b"<script" * 20000)
        msg = bts.structure_message(raw, index=0, offset=0, source_url="u",
                                    policy=make_policy(
                                        max_msg_body_chars=500))
        self.assertTrue(msg["html_only"])
        self.assertLessEqual(len(msg["body_text"]), 500 + 100)


class SoapParseTest(unittest.TestCase):
    def test_two_items_with_escapes(self):
        msgs, meta = bts.parse_soap_bug_log(
            SOAP_ENVELOPE.encode(), source_url="https://s",
            policy=make_policy())
        self.assertEqual(len(msgs), 2)
        self.assertEqual([m["msg_num"] for m in msgs], [5, 7])
        self.assertEqual(msgs[0]["message_id"], "<msg-one@example.org>")
        self.assertIn("https://upstream.example.org/new-home",
                      msgs[0]["urls"])
        self.assertEqual(msgs[1]["body_text"], "body two plain")
        self.assertNotIn("From submitter@example.org Mon",
                         msgs[0]["body_text"])

    def test_fault_raises(self):
        with self.assertRaises(bts.BtsError) as ctx:
            bts.parse_soap_bug_log(SOAP_FAULT.encode(), source_url="s",
                                   policy=make_policy())
        self.assertEqual(ctx.exception.kind, "soap-fault")

    def test_huge_faultstring_message_bounded(self):
        # 回归：faultstring 来自服务端（可达 max_bytes），曾整段进入
        # BtsError.message；现在截到 200 字符
        huge_fault = SOAP_FAULT.replace(
            "Bug#999999999 does not exist", "F" * 10000)
        with self.assertRaises(bts.BtsError) as ctx:
            bts.parse_soap_bug_log(huge_fault.encode(), source_url="s",
                                   policy=make_policy())
        self.assertEqual(ctx.exception.kind, "soap-fault")
        self.assertIn("F" * 200, ctx.exception.message)
        self.assertNotIn("F" * 201, ctx.exception.message)
        self.assertLessEqual(
            len(ctx.exception.message), len("SOAP Fault from BTS: ") + 200)

    def test_soap_entities_decoded_exactly_once(self):
        # 回归（Oracle 复审）：正文里字面的 &amp;/&lt;/&gt; 文本经
        # XML 规范解码一次后必须原样保留，绝不允许二次手工解码损坏
        literal = "literal &amp; and &lt;tag&gt; and 5 &lt; 6 text"
        envelope = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/'
            'envelope/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
            'xmlns:soapenc="http://schemas.xmlsoap.org/soap/encoding/">'
            '<soap:Body><get_bug_logResponse xmlns="urn:Debbugs/SOAP">'
            + soap_item("From a@b.c Mon Nov 02 10:00:00 2026\n"
                        "Message-ID: <ent@example.org>\n"
                        "Subject: entities\n", literal, 1)
            + "</get_bug_logResponse></soap:Body></soap:Envelope>")
        msgs, _ = bts.parse_soap_bug_log(envelope.encode(), source_url="s",
                                         policy=make_policy())
        self.assertIn(literal, msgs[0]["body_text"])

    def test_soap_attachments_array_disclosed_not_parsed(self):
        # Oracle 复审：非空 attachments 数组 → 披露"未提取"，其嵌套 item
        # 绝不能被当成消息
        envelope = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/'
            'envelope/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            '<soap:Body><get_bug_logResponse xmlns="urn:Debbugs/SOAP">'
            '<soapenc:Array xmlns:soapenc="http://schemas.xmlsoap.org/soap/'
            'encoding/" soapenc:arrayType="xsd:anyType[1]">'
            '<item><header xsi:type="xsd:string">From a@b.c Mon Nov 02 '
            '10:00:00 2026\nMessage-ID: &lt;with-att@example.org&gt;'
            '</header><body xsi:type="xsd:string">see attachment'
            '</body><msg_num xsi:type="xsd:int">9</msg_num>'
            '<attachments soapenc:arrayType="xsd:anyType[1]" '
            'xsi:type="soapenc:Array"><item><filename>evil.bin'
            '</filename></item></attachments></item>'
            "</soapenc:Array></get_bug_logResponse></soap:Body>"
            "</soap:Envelope>")
        msgs, meta = bts.parse_soap_bug_log(envelope.encode(), source_url="s",
                                            policy=make_policy())
        self.assertEqual(len(msgs), 1)  # 嵌套 attachment item 不算消息
        self.assertEqual(msgs[0]["msg_num"], 9)
        self.assertTrue(any("does NOT extract" in note
                            for note in meta["notes"]))
        self.assertNotIn("evil.bin", json.dumps(msgs))

    def test_malformed_xml_raises_parse_error(self):
        with self.assertRaises(bts.BtsError) as ctx:
            bts.parse_soap_bug_log(b"<not-xml", source_url="s",
                                   policy=make_policy())
        self.assertEqual(ctx.exception.kind, "parse-error")


# ---- 离线：挑战检测与解答 -----------------------------------------------------

class ChallengeTest(unittest.TestCase):
    def test_detect_anubis_page(self):
        challenge = bts.detect_challenge(
            200, {"content-type": ["text/html"]}, ANUBIS_CHALLENGE_PAGE)
        self.assertIsNotNone(challenge)
        self.assertEqual(challenge["type"], "pow-challenge")

    def test_detect_varnish_redirect_and_page(self):
        redirect = bts.detect_challenge(
            302, {"location": ["/.internal/challenge.html?original=%2Fx"]},
            b"")
        self.assertIsNotNone(redirect)
        self.assertEqual(redirect["type"], "varnish-pow")
        page = bts.detect_challenge(
            200, {"content-type": ["text/html"]}, VARNISH_CHALLENGE_PAGE)
        self.assertIsNotNone(page)
        self.assertEqual(page["type"], "varnish-pow")

    def test_normal_redirect_is_not_challenge(self):
        self.assertIsNone(bts.detect_challenge(
            302, {"location": ["https://bugs.debian.org/cgi-bin/"]}, b""))

    def test_normal_mbox_not_challenge(self):
        self.assertIsNone(bts.detect_challenge(
            200, {"content-type": ["application/mbox"]}, MBOX_FIXTURE))
        self.assertIsNone(bts.detect_challenge(
            200, {"content-type": ["text/xml"]}, SOAP_ENVELOPE.encode()))

    def test_extract_anubis_params(self):
        params = bts.extract_anubis_challenge(ANUBIS_CHALLENGE_PAGE)
        self.assertEqual(params["challenge"], "abc123")
        self.assertEqual(params["difficulty"], 1)
        self.assertIsNone(bts.extract_anubis_challenge(MBOX_FIXTURE))

    def test_solve_varnish_pow_matches_js_spec(self):
        solved = bts.solve_varnish_pow("abc", max_seconds=10.0)
        self.assertIsNotNone(solved)
        tries, digest, elapsed = solved
        import hashlib as _h
        expect = _h.sha256(f"abc;{tries}".encode()).hexdigest()
        self.assertEqual(digest, expect)
        self.assertTrue(digest.startswith("00"))
        self.assertLess(tries, 100000)
        self.assertIsNone(bts.solve_varnish_pow("abc", max_seconds=0.0))

    def test_hashcash_bounded(self):
        solved = bts.solve_hashcash("abc", 1, max_seconds=5.0)
        self.assertIsNotNone(solved)
        nonce, digest, elapsed = solved
        self.assertTrue(digest.startswith("0"))
        self.assertLess(elapsed, 5.0)
        self.assertIsNone(bts.solve_hashcash("abc", 40, max_seconds=0.05))

    def test_redact_cookies(self):
        redacted = bts.redact_sensitive_headers({
            "set-cookie": ["session=SECRET; Path=/"],
            "content-type": ["text/html"]})
        self.assertNotIn("SECRET", json.dumps(redacted))
        self.assertEqual(redacted["content-type"], ["text/html"])


# ---- 假连接：客户端行为 -------------------------------------------------------

class FakeResponse:
    def __init__(self, status, headers=None, body=b""):
        self.status = status
        self._headers = headers or {}
        self._body = body
        self._pos = 0

    def getheaders(self):
        flat = []
        for key, value in self._headers.items():
            values = value if isinstance(value, list) else [value]
            flat.extend((key, item) for item in values)
        return flat

    def read(self, n=-1):
        if self._pos >= len(self._body):
            return b""
        chunk = self._body[self._pos:self._pos + n] if n and n > 0 \
            else self._body[self._pos:]
        self._pos += len(chunk)
        return chunk


class FakeConnection:
    instances = []

    def __init__(self, scheme, host, port, pinned_ip, timeout):
        self.host = host
        self.route = FakeConnection.route
        self.sock = None
        self.requests = []
        self.request_bodies = []
        FakeConnection.instances.append(self)

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, headers))
        self.request_bodies.append(body)

    def getresponse(self):
        method, path, headers = self.requests[-1]
        result = self.route(self.host, path, headers or {})
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        pass


def patch_transport(testcase, route):
    FakeConnection.instances = []
    FakeConnection.route = staticmethod(route)
    testcase.patches = [
        mock.patch("gleam.bts._public_address", lambda host: "203.0.113.9"),
        mock.patch("gleam.bts._open_connection",
                   lambda scheme, host, port, pinned_ip, timeout:
                   FakeConnection(scheme, host, port, pinned_ip, timeout)),
    ]
    for patch in testcase.patches:
        patch.start()
        testcase.addCleanup(patch.stop)


REAL_ROBOTS = (
    "User-Agent: Googlebot\n"
    "Allow: /cgi-bin/bugreport.cgi?bug=\n"
    "Disallow: /*/\n"
    "User-agent: *\n"
    "Disallow: /\n").encode("utf-8")


def bts_route(pages=None, soap_status=200, soap_body=SOAP_ENVELOPE.encode()):
    """构造典型 BTS 路由：robots + soap + 可选覆盖页（按路径前缀）。"""

    pages = pages or {}

    def route(host, path, headers):
        if path == "/robots.txt":
            return FakeResponse(200, {"content-type": ["text/plain"]},
                                REAL_ROBOTS)
        if path.startswith("/cgi-bin/soap.cgi"):
            return FakeResponse(
                soap_status,
                {"content-type": ["text/xml; charset=utf-8"]}, soap_body)
        for prefix, response in pages.items():
            if path.startswith(prefix):
                return response
        return FakeResponse(404, {"content-type": ["text/html"]}, b"")
    return route


NO_THROTTLE = {"min_interval_s": 0}


class BtsClientTest(unittest.TestCase):
    def soap_headers(self, body):
        return {"Content-Type": "text/xml; charset=utf-8",
                "Content-Length": str(len(body)),
                "SOAPAction": '"urn:Debbugs/SOAP#get_bug_log"'}

    def test_soap_success(self):
        patch_transport(self, bts_route())
        client = bts.BtsClient(policy=dict(NO_THROTTLE))
        result = client.fetch_bug(620025)
        self.assertEqual(result["messages_count"], 2)
        self.assertEqual(result["status"], 200)
        self.assertEqual(client.request_count, 2)  # robots + soap
        soap_calls = [c for c in FakeConnection.instances
                      if any(p.startswith("/cgi-bin/soap.cgi")
                             for _, p, _ in c.requests)]
        self.assertEqual(len(soap_calls), 1)
        method, path, headers = soap_calls[0].requests[0]
        self.assertEqual(method, "POST")
        self.assertIn(">620025<", soap_calls[0].request_bodies[0].decode())
        self.assertEqual(headers.get("SOAPAction"),
                         '"urn:Debbugs/SOAP#get_bug_log"')
        self.assertFalse(result["challenge"]["encountered"])

    def test_mbox_refused_by_robots(self):
        patch_transport(self, bts_route(pages={
            "/cgi-bin/bugreport.cgi": FakeResponse(
                200, {"content-type": ["application/mbox"]}, MBOX_FIXTURE)}))
        client = bts.BtsClient(policy=dict(NO_THROTTLE))
        with self.assertRaises(bts.BtsError) as ctx:
            client.fetch_bug(620025, source="mbox")
        self.assertEqual(ctx.exception.kind, "robots-disallow")
        mbox_calls = [c for c in FakeConnection.instances
                      if any("bugreport.cgi" in p for _, p, _ in c.requests)]
        self.assertEqual(mbox_calls, [])  # 拒绝发生在请求之前

    def test_mbox_allowed_when_robots_absent(self):
        def route(host, path, headers):
            if path == "/robots.txt":
                return FakeResponse(404, {}, b"")
            if path.startswith("/cgi-bin/bugreport.cgi"):
                return FakeResponse(200,
                                    {"content-type": ["application/mbox"]},
                                    MBOX_FIXTURE)
            return FakeResponse(404, {}, b"")
        patch_transport(self, route)
        client = bts.BtsClient(policy=dict(NO_THROTTLE))
        result = client.fetch_bug(620025, source="mbox")
        self.assertEqual(result["messages_count"], 3)

    def test_rate_limited_429_honest(self):
        patch_transport(self, bts_route(soap_status=429, soap_body=b""))
        client = bts.BtsClient(policy=dict(NO_THROTTLE))
        with self.assertRaises(bts.BtsError) as ctx:
            client.fetch_bug(620025)
        self.assertEqual(ctx.exception.kind, "rate-limited")

    def test_404_not_found(self):
        patch_transport(self, bts_route(soap_status=404, soap_body=b""))
        client = bts.BtsClient(policy=dict(NO_THROTTLE))
        with self.assertRaises(bts.BtsError) as ctx:
            client.fetch_bug(620025)
        self.assertEqual(ctx.exception.kind, "not-found")

    def test_soap_fault_nonexistent_bug_maps_not_found(self):
        patch_transport(self, bts_route(soap_status=500,
                                        soap_body=SOAP_FAULT.encode()))
        client = bts.BtsClient(policy=dict(NO_THROTTLE))
        with self.assertRaises(bts.BtsError) as ctx:
            client.fetch_bug(9999999)
        self.assertEqual(ctx.exception.kind, "not-found")

    def test_huge_faultstring_bounded_on_http_500(self):
        # 回归：HTTP 500 伴随的巨大 faultstring 曾整段进入错误消息；
        # 现在分类用全文、消息只带前 200 字符
        huge_fault = SOAP_FAULT.replace(
            "Bug#999999999 does not exist", "F" * 10000)
        patch_transport(self, bts_route(soap_status=500,
                                        soap_body=huge_fault.encode()))
        client = bts.BtsClient(policy=dict(NO_THROTTLE))
        with self.assertRaises(bts.BtsError) as ctx:
            client.fetch_bug(620025, source="soap")
        self.assertEqual(ctx.exception.kind, "soap-fault")
        self.assertIn("F" * 200, ctx.exception.message)
        self.assertNotIn("F" * 201, ctx.exception.message)
        self.assertLessEqual(
            len(ctx.exception.message), len("HTTP 500; SOAP Fault: ") + 200)

    def test_read_timeout_partial_body_raises_timeout(self):
        # 回归：读体超时曾带部分 body 与 error 字段返回而被静默当作
        # 200+截断解析；现在按 error.kind 如实上抛、部分 body 不解析
        class SlowBodyResponse(FakeResponse):
            def read(self, n=-1):
                time.sleep(0.05)
                return super().read(n)

        def route(host, path, headers):
            if path == "/robots.txt":
                return FakeResponse(404, {}, b"")
            return SlowBodyResponse(
                200, {"content-type": ["text/xml; charset=utf-8"]},
                SOAP_ENVELOPE.encode())
        patch_transport(self, route)
        client = bts.BtsClient(policy=dict(NO_THROTTLE, timeout_s=0.01))
        with self.assertRaises(bts.BtsError) as ctx:
            client.fetch_bug(620025, source="soap")
        self.assertEqual(ctx.exception.kind, "timeout")
        self.assertIn("partial body", ctx.exception.message)
        self.assertIn("discarded", ctx.exception.message)

    def test_cookie_with_crlf_dropped_next_request_fine(self):
        # 回归：含 CRLF 的 cookie 值曾直接拼进 Cookie 头（http.client
        # ValueError 被误分级 transport-error）；现在不安全 cookie 整条
        # 丢弃，安全 cookie 照常携带
        seen = []

        def route(host, path, headers):
            if path == "/robots.txt":
                return FakeResponse(
                    200,
                    {"content-type": ["text/plain"],
                     "set-cookie": ["evil=va\r\nlue; Path=/", "good=ok"]},
                    REAL_ROBOTS)
            seen.append(headers.get("Cookie"))
            return FakeResponse(
                200, {"content-type": ["text/xml; charset=utf-8"]},
                SOAP_ENVELOPE.encode())
        patch_transport(self, route)
        client = bts.BtsClient(policy=dict(NO_THROTTLE))
        result = client.fetch_bug(620025)
        self.assertEqual(result["messages_count"], 2)
        self.assertEqual(seen, ["good=ok"])
        self.assertEqual(client._cookies, {"good": "ok"})

    def test_redirect_not_followed(self):
        patch_transport(self, bts_route(soap_status=302, soap_body=b""))
        client = bts.BtsClient(policy=dict(NO_THROTTLE))
        with self.assertRaises(bts.BtsError) as ctx:
            client.fetch_bug(620025)
        self.assertEqual(ctx.exception.kind, "redirect")

    def test_request_budget(self):
        patch_transport(self, bts_route())
        client = bts.BtsClient(policy=dict(NO_THROTTLE, max_requests=1))
        with self.assertRaises(bts.BtsError) as ctx:
            client.fetch_bug(620025)
        self.assertEqual(ctx.exception.kind, "budget-exhausted")

    def test_throttle_interval_respected(self):
        patch_transport(self, bts_route())
        sleeps = []
        client = bts.BtsClient(
            policy={"min_interval_s": 5.0},
            clock=lambda: 100.0, sleep=sleeps.append)
        client.fetch_bug(620025)
        self.assertEqual(sleeps, [5.0])

    def test_host_allowlist_blocks_url_construction(self):
        with self.assertRaises(bts.BtsError):
            bts.BtsClient(base_url="https://evil.example")

    def test_varnish_pow_flow_solved_and_cookies_private(self):
        # 与 2026-11-02 实测抓取的挑战流程逐字对应：302 →
        # /.internal/challenge.html（pow_challenge cookie）→ 解 →
        # pow_nonce cookie → 重试原 URL 成功
        state = {"phase": 0, "seen_cookie": None}

        def route(host, path, headers):
            if path == "/robots.txt":
                return FakeResponse(404, {}, b"")
            if state["phase"] == 0:
                state["phase"] = 1
                return FakeResponse(
                    302,
                    {"location": ["/.internal/challenge.html?original="
                                  "%2fcgi-bin%2fsoap.cgi"],
                     "set-cookie": ["pow_challenge=CHALLENGEVAL"]},
                    b"")
            if path.startswith("/.internal/challenge.html"):
                return FakeResponse(
                    200,
                    {"set-cookie": ["pow_challenge=CHALLENGEVAL"],
                     "content-type": ["text/html"]},
                    VARNISH_CHALLENGE_PAGE)
            assert "pow_nonce" in headers.get("Cookie", "")
            state["seen_cookie"] = headers.get("Cookie")
            return FakeResponse(
                200, {"content-type": ["text/xml; charset=utf-8"]},
                SOAP_ENVELOPE.encode())
        patch_transport(self, route)
        client = bts.BtsClient(policy=dict(NO_THROTTLE))
        result = client.fetch_bug(620025)
        self.assertTrue(result["challenge"]["encountered"])
        self.assertTrue(result["challenge"]["solved"])
        self.assertEqual(result["messages_count"], 2)
        trace_json = json.dumps(result["challenge"]["trace"])
        self.assertNotIn("CHALLENGEVAL", trace_json)
        self.assertNotIn("SECRETCOOKIE", trace_json)
        self.assertIn("pow_challenge", state["seen_cookie"])
        # robots + soap(302 携带 pow_challenge cookie) + 解后重试
        self.assertEqual(client.request_count, 3)

    def test_varnish_pow_challenge_page_fetch_for_cookie(self):
        # 302 未带 cookie：按文档流程取挑战页一次补 pow_challenge
        state = {"phase": 0}

        def route(host, path, headers):
            if path == "/robots.txt":
                return FakeResponse(404, {}, b"")
            if state["phase"] == 0 and path.startswith("/cgi-bin/soap.cgi"):
                state["phase"] = 1
                return FakeResponse(
                    302,
                    {"location": ["/.internal/challenge.html?original=x"]},
                    b"")
            if path.startswith("/.internal/challenge.html"):
                return FakeResponse(
                    200, {"set-cookie": ["pow_challenge=LATECOOKIE"],
                          "content-type": ["text/html"]},
                    VARNISH_CHALLENGE_PAGE)
            return FakeResponse(
                200, {"content-type": ["text/xml; charset=utf-8"]},
                SOAP_ENVELOPE.encode())
        patch_transport(self, route)
        client = bts.BtsClient(policy=dict(NO_THROTTLE))
        result = client.fetch_bug(620025)
        self.assertTrue(result["challenge"]["solved"])
        self.assertEqual(result["messages_count"], 2)

    def test_varnish_pow_no_cookie_honest_failure(self):
        def route(host, path, headers):
            if path == "/robots.txt":
                return FakeResponse(404, {}, b"")
            if path.startswith("/.internal/challenge.html"):
                return FakeResponse(
                    200, {"content-type": ["text/html"]},
                    VARNISH_CHALLENGE_PAGE)
            return FakeResponse(
                302,
                {"location": ["/.internal/challenge.html?original=x"]},
                b"")
        patch_transport(self, route)
        client = bts.BtsClient(policy=dict(NO_THROTTLE))
        with self.assertRaises(bts.BtsError) as ctx:
            client.fetch_bug(620025)
        self.assertEqual(ctx.exception.kind, "challenge-unknown")

    def test_anubis_flow_solved_and_cookie_private(self):
        state = {"phase": 0}

        def route(host, path, headers):
            if path == "/robots.txt":
                return FakeResponse(404, {}, b"")
            if path.startswith("/.within.website"):
                state["phase"] = 1
                return FakeResponse(
                    200, {"set-cookie": ["anubis-auth=SECRETCOOKIE"]},
                    b'{"status":"ok"}')
            if state["phase"] == 0:
                return FakeResponse(200, {"content-type": ["text/html"]},
                                    ANUBIS_CHALLENGE_PAGE)
            assert "SECRETCOOKIE" in headers.get("Cookie", "")
            return FakeResponse(
                200, {"content-type": ["text/xml; charset=utf-8"]},
                SOAP_ENVELOPE.encode())
        patch_transport(self, route)
        client = bts.BtsClient(policy=dict(NO_THROTTLE, max_pow_seconds=10))
        result = client.fetch_bug(620025)
        self.assertTrue(result["challenge"]["encountered"])
        self.assertTrue(result["challenge"]["solved"])
        self.assertEqual(result["messages_count"], 2)
        self.assertEqual(client.request_count, 4)  # soap+pass+retry（robots 404 计 1）
        self.assertNotIn("SECRETCOOKIE",
                         json.dumps(result["challenge"]["trace"]))
        self.assertNotIn("SECRETCOOKIE",
                         json.dumps(result["headers_redacted"]))

    def test_unrecognized_challenge_is_challenge_unknown(self):
        weird = (b"<!DOCTYPE html><html><head><title>challenge</title>"
                 b"</head><body><p>proof-of-work required</p>"
                 b"<form><input name=nonce></form></body></html>")

        def route(host, path, headers):
            if path == "/robots.txt":
                return FakeResponse(404, {}, b"")
            return FakeResponse(200, {"content-type": ["text/html"]}, weird)
        patch_transport(self, route)
        client = bts.BtsClient(policy=dict(NO_THROTTLE))
        with self.assertRaises(bts.BtsError) as ctx:
            client.fetch_bug(620025)
        self.assertEqual(ctx.exception.kind, "challenge-unknown")


# ---- auto 来源：回退矩阵 -------------------------------------------------------

ABSENT_ROBOTS_ROUTE = bts_route(pages={
    "/cgi-bin/bugreport.cgi": FakeResponse(
        200, {"content-type": ["application/mbox"]}, MBOX_FIXTURE)})


def robots_absent_route(soap=None):
    """robots 404（=无限制，mbox 可回退）+ 可定制的 soap 响应。"""

    def route(host, path, headers):
        if path == "/robots.txt":
            return FakeResponse(404, {}, b"")
        if path.startswith("/cgi-bin/soap.cgi") and soap is not None:
            return soap
        if path.startswith("/cgi-bin/bugreport.cgi"):
            return FakeResponse(200, {"content-type": ["application/mbox"]},
                                MBOX_FIXTURE)
        return FakeResponse(404, {}, b"")
    return route


class AutoSourceTest(unittest.TestCase):
    def test_auto_soap_success_no_fallback(self):
        patch_transport(self, bts_route())
        client = bts.BtsClient(policy=dict(NO_THROTTLE))
        result = client.fetch_bug(620025, source="auto")
        self.assertEqual(result["source"], "soap")
        self.assertFalse(result["source_selection"]["fallback_used"])
        self.assertEqual(result["source_selection"]["tried"], ["soap"])
        self.assertEqual(result["attempts"],
                         [{"source": "soap", "error": None}])
        self.assertEqual(client.request_count, 2)  # robots + soap，无 mbox
        mbox_calls = [c for c in FakeConnection.instances
                      if any("bugreport.cgi" in p for _, p, _ in c.requests)]
        self.assertEqual(mbox_calls, [])

    def test_auto_fallback_on_transport_error_when_robots_permits(self):
        def route(host, path, headers):
            if path == "/robots.txt":
                return FakeResponse(404, {}, b"")
            if path.startswith("/cgi-bin/soap.cgi"):
                raise ConnectionResetError("reset by peer")
            if path.startswith("/cgi-bin/bugreport.cgi"):
                return FakeResponse(
                    200, {"content-type": ["application/mbox"]},
                    MBOX_FIXTURE)
            return FakeResponse(404, {}, b"")
        patch_transport(self, route)
        client = bts.BtsClient(policy=dict(NO_THROTTLE))
        result = client.fetch_bug(620025, source="auto")
        self.assertEqual(result["source"], "mbox")
        selection = result["source_selection"]
        self.assertTrue(selection["fallback_used"])
        self.assertEqual(selection["tried"], ["soap", "mbox"])
        self.assertEqual(selection["fallback_reason"], "transport-error")
        self.assertEqual(result["attempts"][0]["source"], "soap")
        self.assertEqual(result["attempts"][0]["error"]["kind"],
                         "transport-error")
        self.assertEqual(result["attempts"][1],
                         {"source": "mbox", "error": None})
        self.assertEqual(result["messages_count"], 3)
        self.assertEqual(client.request_count, 3)  # robots + soap + mbox

    def test_auto_parse_error_falls_back(self):
        soap = FakeResponse(200, {"content-type": ["text/xml"]},
                            b"<not-xml")
        patch_transport(self, robots_absent_route(soap=soap))
        client = bts.BtsClient(policy=dict(NO_THROTTLE))
        result = client.fetch_bug(620025, source="auto")
        self.assertEqual(result["source"], "mbox")
        self.assertEqual(result["source_selection"]["fallback_reason"],
                         "parse-error")

    def test_auto_no_fallback_on_rate_limit(self):
        soap = FakeResponse(429, {}, b"")
        patch_transport(self, robots_absent_route(soap=soap))
        client = bts.BtsClient(policy=dict(NO_THROTTLE))
        with self.assertRaises(bts.BtsError) as ctx:
            client.fetch_bug(620025, source="auto")
        self.assertEqual(ctx.exception.kind, "rate-limited")
        mbox_calls = [c for c in FakeConnection.instances
                      if any("bugreport.cgi" in p for _, p, _ in c.requests)]
        self.assertEqual(mbox_calls, [])

    def test_auto_no_fallback_on_not_found(self):
        soap = FakeResponse(404, {}, b"")
        patch_transport(self, robots_absent_route(soap=soap))
        client = bts.BtsClient(policy=dict(NO_THROTTLE))
        with self.assertRaises(bts.BtsError) as ctx:
            client.fetch_bug(620025, source="auto")
        self.assertEqual(ctx.exception.kind, "not-found")
        mbox_calls = [c for c in FakeConnection.instances
                      if any("bugreport.cgi" in p for _, p, _ in c.requests)]
        self.assertEqual(mbox_calls, [])

    def test_auto_no_fallback_on_budget_exhausted(self):
        # robots 一发就把 max_requests=1 的预算耗尽：SOAP 尝试本身以
        # budget-exhausted 失败 → 不属回退资格，原始错误上抛、无 mbox
        def route(host, path, headers):
            if path == "/robots.txt":
                return FakeResponse(404, {}, b"")
            return FakeResponse(404, {}, b"")
        patch_transport(self, route)
        client = bts.BtsClient(policy=dict(NO_THROTTLE, max_requests=1))
        with self.assertRaises(bts.BtsError) as ctx:
            client.fetch_bug(620025, source="auto")
        self.assertEqual(ctx.exception.kind, "budget-exhausted")
        self.assertEqual(client.request_count, 1)
        mbox_calls = [c for c in FakeConnection.instances
                      if any("bugreport.cgi" in p for _, p, _ in c.requests)]
        self.assertEqual(mbox_calls, [])

    def test_auto_fallback_budget_exhausted_at_mbox_reports_both(self):
        # SOAP 以资格内错误失败后，mbox 尝试撞预算 → 总失败带双来源轨迹
        def route(host, path, headers):
            if path == "/robots.txt":
                return FakeResponse(404, {}, b"")
            if path.startswith("/cgi-bin/soap.cgi"):
                raise ConnectionResetError("reset")
            return FakeResponse(404, {}, b"")
        patch_transport(self, route)
        client = bts.BtsClient(policy=dict(NO_THROTTLE, max_requests=2))
        with self.assertRaises(bts.BtsError) as ctx:
            client.fetch_bug(620025, source="auto")
        self.assertEqual(ctx.exception.kind, "budget-exhausted")
        kinds = [(a["source"], a["error"]["kind"])
                 for a in ctx.exception.attempts]
        self.assertEqual(kinds, [("soap", "transport-error"),
                                 ("mbox", "budget-exhausted")])

    def test_auto_no_fallback_when_robots_disallows_mbox(self):
        def route(host, path, headers):
            if path == "/robots.txt":
                return FakeResponse(200, {"content-type": ["text/plain"]},
                                    REAL_ROBOTS)
            if path.startswith("/cgi-bin/soap.cgi"):
                raise ConnectionResetError("reset")
            return FakeResponse(404, {}, b"")
        patch_transport(self, route)
        client = bts.BtsClient(policy=dict(NO_THROTTLE))
        with self.assertRaises(bts.BtsError) as ctx:
            client.fetch_bug(620025, source="auto")
        self.assertEqual(ctx.exception.kind, "transport-error")
        self.assertIn("robots", ctx.exception.message)
        self.assertIn("never bypassed", ctx.exception.message)
        self.assertEqual(ctx.exception.attempts,
                         [{"source": "soap",
                           "error": {"kind": "transport-error",
                                     "message": ctx.exception.attempts[0]
                                     ["error"]["message"]}}])
        mbox_calls = [c for c in FakeConnection.instances
                      if any("bugreport.cgi" in p for _, p, _ in c.requests)]
        self.assertEqual(mbox_calls, [])

    def test_auto_no_fallback_when_robots_unknown(self):
        def route(host, path, headers):
            if path == "/robots.txt":
                return FakeResponse(403, {}, b"")  # robots 状态未知
            if path.startswith("/cgi-bin/soap.cgi"):
                raise ConnectionResetError("reset")
            if path.startswith("/cgi-bin/bugreport.cgi"):
                return FakeResponse(
                    200, {"content-type": ["application/mbox"]},
                    MBOX_FIXTURE)
            return FakeResponse(404, {}, b"")
        patch_transport(self, route)
        client = bts.BtsClient(policy=dict(NO_THROTTLE))
        with self.assertRaises(bts.BtsError) as ctx:
            client.fetch_bug(620025, source="auto")
        self.assertEqual(ctx.exception.kind, "transport-error")
        self.assertIn("auto fallback to mbox not attempted",
                      ctx.exception.message)
        # allow_robots_disallowed_mbox 在 auto 模式被忽略（不接受未知态）
        patch_transport(self, route)
        client = bts.BtsClient(policy=dict(NO_THROTTLE))
        with self.assertRaises(bts.BtsError) as ctx2:
            client.fetch_bug(620025, source="auto",
                             allow_robots_disallowed_mbox=True)
        self.assertEqual(ctx2.exception.kind, "transport-error")

    def test_auto_both_fail_reports_last_error_with_attempts(self):
        soap = FakeResponse(200, {"content-type": ["text/xml"]},
                            b"<not-xml")

        def route(host, path, headers):
            if path == "/robots.txt":
                return FakeResponse(404, {}, b"")
            if path.startswith("/cgi-bin/soap.cgi"):
                return soap
            if path.startswith("/cgi-bin/bugreport.cgi"):
                return FakeResponse(404, {"content-type": ["text/html"]},
                                    b"")
            return FakeResponse(404, {}, b"")
        patch_transport(self, route)
        client = bts.BtsClient(policy=dict(NO_THROTTLE))
        with self.assertRaises(bts.BtsError) as ctx:
            client.fetch_bug(620025, source="auto")
        self.assertEqual(ctx.exception.kind, "not-found")  # 最后失败来源
        self.assertIn("[auto] both sources failed", ctx.exception.message)
        kinds = [(a["source"], a["error"]["kind"])
                 for a in ctx.exception.attempts]
        self.assertEqual(kinds, [("soap", "parse-error"),
                                 ("mbox", "not-found")])

    def test_explicit_source_no_fallback_semantics(self):
        soap = FakeResponse(429, {}, b"")
        patch_transport(self, robots_absent_route(soap=soap))
        client = bts.BtsClient(policy=dict(NO_THROTTLE))
        with self.assertRaises(bts.BtsError) as ctx:
            client.fetch_bug(620025, source="soap")
        self.assertEqual(ctx.exception.kind, "rate-limited")
        self.assertIsNone(ctx.exception.attempts)  # 显式模式不构造 auto 轨迹
        mbox_calls = [c for c in FakeConnection.instances
                      if any("bugreport.cgi" in p for _, p, _ in c.requests)]
        self.assertEqual(mbox_calls, [])
        # 显式成功结果同样带 attempts/source_selection（uniform provenance）
        patch_transport(self, bts_route())
        result = client.fetch_bug(620025, source="soap")
        self.assertEqual(result["source_selection"]["mode"], "explicit")
        self.assertEqual(result["attempts"],
                         [{"source": "soap", "error": None}])

    def test_invalid_source_rejected(self):
        client = bts.BtsClient(policy=dict(NO_THROTTLE))
        for bad in ("web", "", None, 5):
            with self.assertRaises(bts.BtsError) as ctx:
                client.fetch_bug(620025, source=bad)
            self.assertEqual(ctx.exception.kind, "argument-invalid")


# ---- CLI ----

class BugFetchCliTest(unittest.TestCase):
    def run_cli(self, argv, route):
        store_dir = tempfile.mkdtemp()
        patch_transport(self, route)
        parser = build_parser()
        args = parser.parse_args(["bug-fetch", "--bug", "620025",
                                  "--store", store_dir,
                                  "--bts-min-interval", "0"] + argv)
        code = cmd_bug_fetch(args)
        return code, store_dir

    def test_success_writes_store_and_check_passes(self):
        code, store_dir = self.run_cli([], bts_route())
        self.assertEqual(code, EXIT_OK)
        store = RunStore(store_dir)
        run_id = store.list_runs()[0]
        with open(f"{store.runs_root}/{run_id}/run.json") as fh:
            summary = json.load(fh)
        self.assertEqual(summary["kind"], "bug-fetch")
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["counts"]["messages"], 2)
        self.assertFalse(summary["challenge"]["encountered"])
        from gleam.store import verify_store
        self.assertEqual(verify_store(store_dir), [])

    def test_usage_error_non_numeric_bug(self):
        parser = build_parser()
        args = parser.parse_args(["bug-fetch", "--bug", "0062002x"])
        self.assertEqual(cmd_bug_fetch(args), EXIT_USAGE)

    def test_failure_exit_1_with_artifacts(self):
        code, store_dir = self.run_cli(
            [], bts_route(soap_status=404, soap_body=b""))
        self.assertEqual(code, 1)
        store = RunStore(store_dir)
        run_id = store.list_runs()[0]
        with open(f"{store.runs_root}/{run_id}/run.json") as fh:
            summary = json.load(fh)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["status_detail"]["kind"], "not-found")


# ---- investigate 工具集成 ------------------------------------------------------

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


class StubBtsClient:
    """BtsClient 替身：返回合成成功结果，绝不触网。"""

    def __init__(self, policy=None, budget_check=None, base_url=None,
                 clock=None, sleep=None):
        self.policy = dict(bts.DEFAULT_POLICY)
        self.budget_check = budget_check
        self.request_count = 0
        self.challenge_trace = []
        self.robots_fact = None

    def fetch_bug(self, bug_id, source="soap",
                  allow_robots_disallowed_mbox=False):
        self.request_count += 2
        messages, meta = bts.parse_soap_bug_log(
            SOAP_ENVELOPE.encode(), source_url="https://bugs.debian.org/"
            "cgi-bin/soap.cgi", policy=self.policy)
        return {
            "bug_id": bug_id, "source": "soap",
            "url": "https://bugs.debian.org/cgi-bin/soap.cgi",
            "bug_page_url": bts.bug_page_url(bug_id),
            "retrieved_at": "2026-11-02T00:00:00Z", "status": 200,
            "headers_redacted": {},
            "body_truncated": False, "content_bytes": 100,
            "content_sha256": "0" * 64,
            "robots": {"mode": "absent", "detail": "robots absent",
                       "url": "https://bugs.debian.org/robots.txt"},
            "challenge": {"encountered": False, "solved": False,
                          "trace": []},
            "messages": messages, "messages_count": len(messages),
            "parse_meta": meta,
            "body_bytes": SOAP_ENVELOPE.encode(),
        }


class AgentToolTest(unittest.TestCase):
    def make_context(self, bts_enabled=True, max_requests=12):
        store_dir = tempfile.mkdtemp()
        store = RunStore(store_dir)
        run_id = store.create_run()
        fetcher = StubFetcher()
        ctx = InvestigationContext(
            store=store, run_id=run_id,
            package="pkgone", udd_client=None, default_release=None,
            udd_max_calls=6, udd_max_rows=200,
            fetcher=fetcher,
            limits={"max_body_chars": 4000, "max_links": 40,
                    "max_context_chars": 60000, "max_rounds": 8,
                    "max_tool_calls": 24, "max_report_repairs": 2},
            search_enabled=False, search_client=None, github_base=None,
            bts_enabled=bts_enabled)
        return ctx

    def test_tool_spec_registered(self):
        names = {spec["function"]["name"] for spec in TOOL_SPECS}
        self.assertIn("bug_fetch", names)

    def test_success_payload_and_evidence(self):
        ctx = self.make_context()
        with mock.patch("gleam.agent.InvestigationContext._bts",
                        return_value=StubBtsClient()):
            payload = ctx.execute("bug_fetch", {"bug_id": 620025})
        self.assertNotIn("error", payload)
        self.assertEqual(payload["messages_count"], 2)
        self.assertEqual(payload["source_kind"],
                         "debian-bts-public-discussion")
        self.assertIn("https://upstream.example.org/new-home",
                      payload["messages"][0]["urls"])
        obs_id = payload["observation_id"]
        entry = ctx.evidence[obs_id]
        self.assertIn(bts.bug_page_url(620025),
                      entry["qualifying_urls_norm"])
        self.assertIn("https://upstream.example.org/new-home",
                      entry["qualifying_urls_norm"])
        self.assertTrue(ctx.tool_counts["bug_fetch"]["calls"] == 1)
        self.assertEqual(ctx.tool_counts["bug_fetch"]["errors"], 0)

    def test_invalid_bug_id_rejected_before_network(self):
        ctx = self.make_context()
        payload = ctx.execute("bug_fetch", {"bug_id": "0062bad"})
        self.assertEqual(payload["error"]["kind"], "argument-invalid")
        self.assertEqual(ctx.fetcher.request_count, 0)

    def test_disabled_honest(self):
        ctx = self.make_context(bts_enabled=False)
        payload = ctx.execute("bug_fetch", {"bug_id": 620025})
        self.assertEqual(payload["error"]["kind"], "capability-disabled")

    def test_shared_budget_exhaustion(self):
        ctx = self.make_context()
        ctx.fetcher.policy["max_requests"] = 2
        ctx.fetcher.request_count = 2
        payload = ctx.execute("bug_fetch", {"bug_id": 620025})
        self.assertEqual(payload["error"]["kind"], "budget-exhausted")

    def test_bts_error_honest_note(self):
        class FailingBts(StubBtsClient):
            def fetch_bug(self, bug_id, **kwargs):
                raise bts.BtsError("challenge", "unsolved")
        ctx = self.make_context()
        with mock.patch("gleam.agent.InvestigationContext._bts",
                        return_value=FailingBts()):
            payload = ctx.execute("bug_fetch", {"bug_id": 620025})
        self.assertEqual(payload["error"]["kind"], "challenge")
        self.assertIn("never proof", payload["note"])


if __name__ == "__main__":
    unittest.main()
