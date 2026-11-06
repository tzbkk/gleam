"""Debian BTS source adapter tests: synthetic fixtures + fake
connections, fully offline (module-boundary patches).

Coverage: strict bug-id validation (UsageError), fixed hosts/paths and
application-built URLs/envelopes, mbox/SOAP parsing (multiparts,
charsets, quoted-printable/base64, attachments as metadata only),
challenge detection and bounded solving, cookie hygiene, the robots
gate, honest grading of budget/throttle/redirect/429/404/soap-fault,
and the full auto fallback matrix ({SOAP outcome} x {robots verdict}
-> fallback decision) with the frozen eligibility set.
"""

import base64
import hashlib
import json
import quopri
import time
import unittest
from unittest import mock

from gleam.core.result import DiagnosticItem, UsageError
from gleam.core.sources.bts import (
    AUTO_FALLBACK_ELIGIBLE,
    BTS_ALLOWED_HOSTS,
    BTS_SOURCES,
    BtsClient,
    BtsPolicy,
    BtsResult,
    bug_page_url,
    build_mbox_url,
    build_soap_get_bug_log,
    build_soap_url,
    validate_bug_id,
)
from gleam.core.sources.bts.client import redact_sensitive_headers
from gleam.core.sources.bts.errors import BtsError
from gleam.core.sources.bts.messages import (
    parse_mbox_messages,
    parse_soap_bug_log,
)


def fast_policy(**overrides):
    """Default policy without polite pacing (offline tests)."""
    return BtsPolicy(min_interval_s=0.0, **overrides)


# ---- synthetic mbox fixture: QP+UTF-8 body with URLs, koi8-r
# multipart + base64 attachment, html-only message ----

UTF8_QP_BODY = quopri.encodestring(
    "upstream moved to https://git.example.org/pkgone.git\n"
    "mirror: https://mirror.example.net/pkgone/\n".encode("utf-8"))

ATTACHMENT_BYTES = b"diff --git a/x b/x\n--- harmless textual patch ---\n"

KOI8_TEXT = "Text in KOI8-R encoding".encode("koi8-r")

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


def soap_envelope(subject="orphan request", n_messages=2):
    """Synthetic get_bug_log response; message i carries a distinct
    subject/body/URL so per-message digests are distinguishable."""
    items = []
    for i in range(1, n_messages + 1):
        header = (
            f"Received: (at submit) by bugs.debian.org; 2 Nov 2026\n"
            f"From sender{i}@example.org Mon Nov 02 1{i}:00:00 2026\n"
            f"Message-ID: <msg-{i}@example.org>\n"
            f"Date: Mon, 2 Nov 2026 1{i}:00:00 +0000\n"
            f"From: Sender {i} <sender{i}@example.org>\n"
            f"Subject: {subject} #{i}\n"
            f"Content-Type: text/plain; charset=utf-8\n")
        body = (f"body {i} with https://upstream{i}.example.org/new-home\n")
        items.append(soap_item(header, body, 4 + i))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soap:Envelope soap:encodingStyle='
        '"http://schemas.xmlsoap.org/soap/encoding/" '
        'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:soapenc="http://schemas.xmlsoap.org/soap/encoding/" '
        'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<soap:Body><get_bug_logResponse xmlns="urn:Debbugs/SOAP">'
        '<soapenc:Array soapenc:arrayType="xsd:anyType['
        f'{n_messages}]" xsi:type="soapenc:Array">'
        + "".join(items) +
        "</soapenc:Array></get_bug_logResponse></soap:Body>"
        "</soap:Envelope>")


SOAP_ENVELOPE = soap_envelope()

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

REAL_ROBOTS = (
    "User-Agent: Googlebot\n"
    "Allow: /cgi-bin/bugreport.cgi?bug=\n"
    "Disallow: /*/\n"
    "User-agent: *\n"
    "Disallow: /\n").encode("utf-8")


# ---- offline: validation and construction ----

class ValidationTest(unittest.TestCase):
    def test_strict_numeric_only(self):
        self.assertEqual(validate_bug_id(620025), 620025)
        self.assertEqual(validate_bug_id("620025"), 620025)
        for bad in (0, -1, 10000000, "0620025", "620025a", " 620025",
                    "6200 25", "", None, True, False, 620025.0, b"620025",
                    "../etc", "620025&mbox=yes", "1;evil"):
            with self.assertRaises(UsageError, msg=repr(bad)):
                validate_bug_id(bad)

    def test_fixed_hosts_and_paths_not_configurable(self):
        # constants only; no builder accepts a host or path
        self.assertEqual(BTS_ALLOWED_HOSTS, frozenset({"bugs.debian.org"}))
        self.assertEqual(build_mbox_url(620025),
                         "https://bugs.debian.org/cgi-bin/bugreport.cgi"
                         "?bug=620025&mbox=yes")
        self.assertEqual(build_soap_url(),
                         "https://bugs.debian.org/cgi-bin/soap.cgi")
        self.assertEqual(bug_page_url(620025),
                         "https://bugs.debian.org/620025")

    def test_soap_envelope_contains_only_number(self):
        envelope = build_soap_get_bug_log(620025).decode()
        self.assertIn("get_bug_log", envelope)
        self.assertIn(">620025<", envelope)
        body = envelope.split("Body")[1].split("Body")[0]
        self.assertNotIn("&", body.replace("&lt;", ""))

    def test_fallback_eligibility_set(self):
        # frozen narrowing: exactly these four kinds may fall back
        self.assertEqual(AUTO_FALLBACK_ELIGIBLE,
                         {"transport-error", "timeout", "soap-fault",
                          "parse-error"})

# ---- offline: mbox / SOAP parsing ----

class MboxParseTest(unittest.TestCase):
    def policy(self):
        return {"max_messages": 200, "max_msg_body_chars": 20000,
                "max_urls_per_message": 20}

    def test_three_messages_charsets_attachments_urls(self):
        msgs, meta = parse_mbox_messages(
            MBOX_FIXTURE, source_url="https://bugs.debian.org/x",
            policy=self.policy())
        self.assertEqual(len(msgs), 3)
        self.assertEqual(meta["source_format"], "mbox")
        first, second, third = msgs
        self.assertEqual(first["message_id"], "<msg-one@example.org>")
        self.assertIn("https://git.example.org/pkgone.git", first["urls"])
        self.assertIn("https://mirror.example.net/pkgone/", first["urls"])
        self.assertIn("upstream moved to", first["body_text"])
        self.assertEqual(first["charsets"], ["utf-8"])
        self.assertIn("package orphaned",
                      first["subject_decoded"] or "")
        self.assertEqual(second["charsets"], ["koi8-r"])
        self.assertIn("Text in KOI8-R encoding", second["body_text"])
        self.assertTrue(third["html_only"])
        self.assertIn("https://tracker.example.org/pkgone", third["urls"])
        offsets = [m["raw"]["offset"] for m in msgs]
        self.assertEqual(offsets, sorted(offsets))
        self.assertTrue(all(m["raw"]["boundary"] == "mbox-from-line"
                            for m in msgs))

    def test_attachments_metadata_only_with_sha256(self):
        msgs, _ = parse_mbox_messages(
            MBOX_FIXTURE, source_url="u", policy=self.policy())
        attach = msgs[1]["attachments"]
        self.assertEqual(len(attach), 1)
        self.assertEqual(attach[0]["filename"], "fix.patch")
        self.assertEqual(attach[0]["bytes"], len(ATTACHMENT_BYTES))
        self.assertEqual(attach[0]["sha256"],
                         hashlib.sha256(ATTACHMENT_BYTES).hexdigest())
        self.assertIn("not separately extracted", attach[0]["note"])
        # decoded attachment content appears nowhere in the records
        self.assertNotIn("harmless textual patch", json.dumps(msgs))

    def test_per_message_sha256_over_raw_bytes(self):
        msgs, _ = parse_mbox_messages(
            MBOX_FIXTURE, source_url="u", policy=self.policy())
        for m in msgs:
            raw = MBOX_FIXTURE[m["raw"]["offset"]:
                               m["raw"]["offset"] + m["raw"]["bytes"]]
            self.assertEqual(m["raw"]["sha256"],
                             hashlib.sha256(raw).hexdigest())
            self.assertEqual(m["raw"]["bytes"], len(raw))

    def test_message_cap_and_body_truncation(self):
        msgs, meta = parse_mbox_messages(
            MBOX_FIXTURE, source_url="u",
            policy={"max_messages": 2, "max_msg_body_chars": 20000,
                    "max_urls_per_message": 20})
        self.assertEqual(len(msgs), 2)
        self.assertEqual(meta["messages_total"], 3)
        self.assertTrue(meta["notes"])
        msgs, _ = parse_mbox_messages(
            MBOX_FIXTURE, source_url="u",
            policy={"max_messages": 200, "max_msg_body_chars": 10,
                    "max_urls_per_message": 20})
        self.assertTrue(msgs[0]["body_truncated"])
        self.assertIn("truncated at 10", msgs[0]["body_text"])

    def test_no_separator_blob(self):
        blob = b"Subject: x\n\nnot an mbox\n"
        msgs, meta = parse_mbox_messages(
            blob, source_url="u", policy=self.policy())
        self.assertEqual(len(msgs), 1)
        self.assertIn("blob", meta["notes"][0])

    def test_deeply_nested_mime_is_parse_error_not_crash(self):
        depth = 2000
        body = b"deepest plain text\n"
        for i in range(depth - 1, -1, -1):
            body = ((f"--B{i}\n"
                     f'Content-Type: multipart/mixed; boundary="B{i + 1}"'
                     f"\n\n").encode()
                    + body + f"\n--B{i}--\n".encode())
        raw = (b"Subject: deep\nMIME-Version: 1.0\n"
               b'Content-Type: multipart/mixed; boundary="B0"\n\n' + body)
        with self.assertRaises(BtsError) as ctx:
            parse_mbox_messages(raw, source_url="u", policy=self.policy())
        self.assertEqual(ctx.exception.kind, "parse-error")
        self.assertIn("RecursionError", ctx.exception.message)


class SoapParseTest(unittest.TestCase):
    def policy(self):
        return {"max_messages": 200, "max_msg_body_chars": 20000,
                "max_urls_per_message": 20}

    def test_items_with_msg_num_urls_and_headers(self):
        msgs, meta = parse_soap_bug_log(
            SOAP_ENVELOPE.encode(), source_url="https://s",
            policy=self.policy())
        self.assertEqual(len(msgs), 2)
        self.assertEqual(meta["source_format"], "soap-get-bug-log")
        self.assertEqual([m["msg_num"] for m in msgs], [5, 6])
        self.assertEqual(msgs[0]["message_id"], "<msg-1@example.org>")
        self.assertIn("https://upstream1.example.org/new-home",
                      msgs[0]["urls"])
        self.assertEqual(msgs[1]["body_text"],
                         "body 2 with "
                         "https://upstream2.example.org/new-home\n")
        # stray mbox From_ lines from the SOAP header are stripped
        self.assertNotIn("From sender1@example.org Mon",
                         msgs[0]["body_text"])

    def test_entities_decoded_exactly_once(self):
        # a body that ARRIVES carrying literal &amp;-style text must
        # survive exactly one XML decode verbatim
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
        msgs, _ = parse_soap_bug_log(envelope.encode(), source_url="s",
                                     policy=self.policy())
        self.assertIn(literal, msgs[0]["body_text"])

    def test_fault_raises_with_bounded_message(self):
        huge_fault = SOAP_FAULT.replace(
            "Bug#999999999 does not exist", "F" * 10000)
        with self.assertRaises(BtsError) as ctx:
            parse_soap_bug_log(huge_fault.encode(), source_url="s",
                               policy=self.policy())
        self.assertEqual(ctx.exception.kind, "soap-fault")
        self.assertIn("F" * 200, ctx.exception.message)
        self.assertNotIn("F" * 201, ctx.exception.message)

    def test_attachments_array_disclosed_not_parsed(self):
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
        msgs, meta = parse_soap_bug_log(envelope.encode(), source_url="s",
                                        policy=self.policy())
        self.assertEqual(len(msgs), 1)  # nested attachment item is not a message
        self.assertEqual(msgs[0]["msg_num"], 9)
        self.assertTrue(any("does NOT extract" in note
                            for note in meta["notes"]))
        self.assertNotIn("evil.bin", json.dumps(msgs))

    def test_malformed_xml_raises_parse_error(self):
        with self.assertRaises(BtsError) as ctx:
            parse_soap_bug_log(b"<not-xml", source_url="s",
                               policy=self.policy())
        self.assertEqual(ctx.exception.kind, "parse-error")


# ---- offline: challenge detection and solving ----

class ChallengeTest(unittest.TestCase):
    def test_detect_varnish_redirect_and_page(self):
        from gleam.core.sources.bts.challenge import detect_challenge
        redirect = detect_challenge(
            302, {"location": ["/.internal/challenge.html?original=%2Fx"]},
            b"")
        self.assertIsNotNone(redirect)
        self.assertEqual(redirect["type"], "varnish-pow")
        page = detect_challenge(
            200, {"content-type": ["text/html"]}, VARNISH_CHALLENGE_PAGE)
        self.assertIsNotNone(page)
        self.assertEqual(page["type"], "varnish-pow")

    def test_detect_anubis_and_negative_cases(self):
        from gleam.core.sources.bts.challenge import detect_challenge
        anubis = detect_challenge(
            200, {"content-type": ["text/html"]}, ANUBIS_CHALLENGE_PAGE)
        self.assertIsNotNone(anubis)
        self.assertEqual(anubis["type"], "pow-challenge")
        # normal traffic is never graded as a challenge
        self.assertIsNone(detect_challenge(
            302, {"location": ["https://bugs.debian.org/cgi-bin/"]}, b""))
        self.assertIsNone(detect_challenge(
            200, {"content-type": ["application/mbox"]}, MBOX_FIXTURE))
        self.assertIsNone(detect_challenge(
            200, {"content-type": ["text/xml"]}, SOAP_ENVELOPE.encode()))

    def test_solve_varnish_pow_matches_js_spec_and_is_bounded(self):
        from gleam.core.sources.bts.challenge import solve_varnish_pow
        solved = solve_varnish_pow("abc", max_seconds=10.0)
        self.assertIsNotNone(solved)
        tries, digest, elapsed = solved
        self.assertEqual(digest,
                         hashlib.sha256(f"abc;{tries}".encode()).hexdigest())
        self.assertTrue(digest.startswith("00"))
        self.assertLess(tries, 100000)
        # zero CPU budget: honest None, never an unbounded burn
        self.assertIsNone(solve_varnish_pow("abc", max_seconds=0.0))

    def test_solve_hashcash_bounded(self):
        from gleam.core.sources.bts.challenge import solve_hashcash
        solved = solve_hashcash("abc", 1, max_seconds=5.0)
        self.assertIsNotNone(solved)
        nonce, digest, elapsed = solved
        self.assertTrue(digest.startswith("0"))
        self.assertLess(elapsed, 5.0)
        self.assertIsNone(solve_hashcash("abc", 40, max_seconds=0.05))

    def test_extract_anubis_params(self):
        from gleam.core.sources.bts.challenge import extract_anubis_challenge
        params = extract_anubis_challenge(ANUBIS_CHALLENGE_PAGE)
        self.assertEqual(params["challenge"], "abc123")
        self.assertEqual(params["difficulty"], 1)
        self.assertIsNone(extract_anubis_challenge(MBOX_FIXTURE))

    def test_redact_cookies(self):
        redacted = redact_sensitive_headers({
            "set-cookie": ["session=SECRET; Path=/"],
            "content-type": ["text/html"]})
        self.assertNotIn("SECRET", json.dumps(redacted))
        self.assertEqual(redacted["content-type"], ["text/html"])


# ---- fake connections: client behavior ----

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
        mock.patch("gleam.core.sources.bts.client._public_address",
                   lambda host: "203.0.113.9"),
        mock.patch("gleam.core.sources.bts.client._open_connection",
                   lambda scheme, host, port, pinned_ip, timeout:
                   FakeConnection(scheme, host, port, pinned_ip, timeout)),
    ]
    for patch in testcase.patches:
        patch.start()
        testcase.addCleanup(patch.stop)


def mbox_calls():
    return [c for c in FakeConnection.instances
            if any("bugreport.cgi" in p for _, p, _ in c.requests)]


def bts_route(pages=None, soap_status=200,
              soap_body=SOAP_ENVELOPE.encode()):
    """Typical BTS route: robots + soap + optional path overrides."""

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


def robots_absent_route(soap=None):
    """robots 404 (= unrestricted, mbox fallback permitted) plus a
    customizable soap response."""

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


class BtsClientTest(unittest.TestCase):
    def test_soap_success_typed_result(self):
        patch_transport(self, bts_route())
        client = BtsClient(policy=fast_policy())
        r = client.fetch(620025)
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.source, "soap")
        self.assertEqual(r.message_count, 2)
        self.assertEqual(client.request_count, 2)  # robots + soap
        # per-message sha256 provenance over distinct raw bytes
        digests = {m["raw"]["sha256"] for m in r.messages}
        self.assertEqual(len(digests), 2)
        for m in r.messages:
            self.assertRegex(m["raw"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(r.content_sha256,
                         hashlib.sha256(SOAP_ENVELOPE.encode()).hexdigest())
        # parallel provenance array
        self.assertEqual(r.provenance,
                         ["bts:bug=620025#msg=1", "bts:bug=620025#msg=2"])
        self.assertFalse(r.truncated)
        self.assertEqual(r.source_selection,
                         {"requested": "auto", "mode": "auto",
                          "tried": ["soap"], "fallback_used": False})
        self.assertEqual(r.attempts, [{"source": "soap", "error": None}])
        soap_calls = [c for c in FakeConnection.instances
                      if any(p.startswith("/cgi-bin/soap.cgi")
                             for _, p, _ in c.requests)]
        self.assertEqual(len(soap_calls), 1)
        method, path, headers = soap_calls[0].requests[0]
        self.assertEqual(method, "POST")
        self.assertIn(">620025<", soap_calls[0].request_bodies[0].decode())
        self.assertEqual(headers.get("SOAPAction"),
                         '"urn:Debbugs/SOAP#get_bug_log"')
        self.assertFalse(r.challenge["encountered"])
        self.assertEqual(mbox_calls(), [])

    def test_usage_errors_raise_before_any_network(self):
        patch_transport(self, bts_route())
        client = BtsClient(policy=fast_policy())
        with self.assertRaises(UsageError):
            client.fetch("0062bad")
        with self.assertRaises(UsageError):
            client.fetch(620025, source="web")
        self.assertEqual(FakeConnection.instances, [])
        self.assertEqual(client.request_count, 0)

    def test_throttle_interval_respected(self):
        patch_transport(self, bts_route())
        sleeps = []
        client = BtsClient(
            policy=BtsPolicy(min_interval_s=5.0),
            clock=lambda: 100.0, sleep=sleeps.append)
        client.fetch(620025)
        self.assertEqual(sleeps, [5.0])

    def test_request_budget_failed_result(self):
        patch_transport(self, bts_route())
        client = BtsClient(policy=fast_policy(max_requests=1))
        r = client.fetch(620025)
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.errors[0].code, "budget-exhausted")
        self.assertEqual(mbox_calls(), [])

    def test_rate_limited_429_failed_no_fallback(self):
        soap = FakeResponse(429, {"retry-after": ["60"]}, b"")
        patch_transport(self, robots_absent_route(soap=soap))
        client = BtsClient(policy=fast_policy())
        r = client.fetch(620025)
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.errors[0].code, "rate-limited")
        self.assertIn("retry-after=60", r.errors[0].message)
        self.assertEqual(mbox_calls(), [])

    def test_404_failed_no_fallback(self):
        soap = FakeResponse(404, {}, b"")
        patch_transport(self, robots_absent_route(soap=soap))
        client = BtsClient(policy=fast_policy())
        r = client.fetch(620025)
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.errors[0].code, "not-found")
        self.assertEqual(mbox_calls(), [])

    def test_403_http_4xx_failed_no_fallback(self):
        # pin: http-4xx is not in the eligibility set -- no fallback
        soap = FakeResponse(403, {}, b"")
        patch_transport(self, robots_absent_route(soap=soap))
        client = BtsClient(policy=fast_policy())
        r = client.fetch(620025)
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.errors[0].code, "http-4xx")
        self.assertEqual(mbox_calls(), [])

    def test_redirect_failed_no_fallback(self):
        # pin: redirect is not in the eligibility set -- no fallback
        soap = FakeResponse(302, {"location": ["https://bugs.debian.org/x"]},
                            b"")
        patch_transport(self, robots_absent_route(soap=soap))
        client = BtsClient(policy=fast_policy())
        r = client.fetch(620025)
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.errors[0].code, "redirect")
        self.assertIn("not followed", r.errors[0].message)
        self.assertEqual(mbox_calls(), [])

    def test_soap_fault_500_maps_not_found(self):
        soap = FakeResponse(500,
                            {"content-type": ["text/xml; charset=utf-8"]},
                            SOAP_FAULT.encode())
        patch_transport(self, robots_absent_route(soap=soap))
        client = BtsClient(policy=fast_policy())
        r = client.fetch(9999999)
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.errors[0].code, "not-found")

    def test_read_timeout_partial_body_failed(self):
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
        client = BtsClient(policy=fast_policy(timeout_s=0.01))
        r = client.fetch(620025, source="soap")
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.errors[0].code, "timeout")
        self.assertIn("partial body", r.errors[0].message)
        self.assertIn("discarded", r.errors[0].message)

    def test_cookie_with_crlf_dropped_next_request_fine(self):
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
        client = BtsClient(policy=fast_policy())
        r = client.fetch(620025)
        self.assertEqual(r.status, "ok")
        self.assertEqual(seen, ["good=ok"])

    def test_host_allowlist_enforced_per_request(self):
        client = BtsClient(policy=fast_policy())
        with self.assertRaises(BtsError) as ctx:
            client._request("https://evil.example/x")
        self.assertEqual(ctx.exception.kind, "unsafe-destination")

    def test_varnish_pow_flow_solved_and_cookies_private(self):
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
        client = BtsClient(policy=fast_policy())
        r = client.fetch(620025)
        self.assertEqual(r.status, "ok")
        self.assertTrue(r.challenge["encountered"])
        self.assertTrue(r.challenge["solved"])
        self.assertEqual(r.message_count, 2)
        trace_json = json.dumps(r.challenge["trace"])
        self.assertNotIn("CHALLENGEVAL", trace_json)
        self.assertIn("pow_challenge", state["seen_cookie"])
        # robots + soap (302 carrying pow_challenge) + retry after solve
        self.assertEqual(client.request_count, 3)

    def test_varnish_pow_no_cookie_is_challenge_unknown(self):
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
        client = BtsClient(policy=fast_policy())
        r = client.fetch(620025)
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.errors[0].code, "challenge-unknown")
        # challenge-unknown never falls back even though robots
        # is absent (= permitted) on this route
        self.assertEqual(mbox_calls(), [])

    def test_unrecognized_challenge_is_challenge_unknown(self):
        weird = (b"<!DOCTYPE html><html><head><title>challenge</title>"
                 b"</head><body><p>proof-of-work required</p>"
                 b"<form><input name=nonce></form></body></html>")

        def route(host, path, headers):
            if path == "/robots.txt":
                return FakeResponse(404, {}, b"")
            return FakeResponse(200, {"content-type": ["text/html"]},
                                weird)
        patch_transport(self, route)
        client = BtsClient(policy=fast_policy())
        r = client.fetch(620025)
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.errors[0].code, "challenge-unknown")


# ---- auto source: the fallback matrix ----

class AutoFallbackMatrixTest(unittest.TestCase):
    def test_transport_error_plus_robots_permits_falls_back(self):
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
        client = BtsClient(policy=fast_policy())
        r = client.fetch(620025, source="auto")
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.source, "mbox")
        self.assertEqual(r.message_count, 3)
        selection = r.source_selection
        self.assertTrue(selection["fallback_used"])
        self.assertEqual(selection["tried"], ["soap", "mbox"])
        self.assertEqual(selection["fallback_reason"], "transport-error")
        self.assertEqual(r.attempts[0]["source"], "soap")
        self.assertEqual(r.attempts[0]["error"]["kind"], "transport-error")
        self.assertEqual(r.attempts[1], {"source": "mbox", "error": None})
        self.assertEqual(client.request_count, 3)  # robots + soap + mbox

    def test_parse_error_plus_robots_permits_falls_back(self):
        soap = FakeResponse(200, {"content-type": ["text/xml"]},
                            b"<not-xml")
        patch_transport(self, robots_absent_route(soap=soap))
        client = BtsClient(policy=fast_policy())
        r = client.fetch(620025, source="auto")
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.source, "mbox")
        self.assertEqual(r.source_selection["fallback_reason"],
                         "parse-error")

    def test_robots_denies_no_fallback_mbox_never_fetched(self):
        def route(host, path, headers):
            if path == "/robots.txt":
                return FakeResponse(200, {"content-type": ["text/plain"]},
                                    REAL_ROBOTS)
            if path.startswith("/cgi-bin/soap.cgi"):
                raise ConnectionResetError("reset by peer")
            return FakeResponse(404, {}, b"")
        patch_transport(self, route)
        client = BtsClient(policy=fast_policy())
        r = client.fetch(620025, source="auto")
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.errors[0].code, "transport-error")
        self.assertIn("robots", r.errors[0].message)
        self.assertIn("never bypassed", r.errors[0].message)
        self.assertEqual(
            r.attempts,
            [{"source": "soap",
              "error": {"kind": "transport-error",
                        "message": r.attempts[0]["error"]["message"]}}])
        self.assertEqual(mbox_calls(), [])  # refusal precedes any request

    def test_robots_unknown_no_fallback(self):
        def route(host, path, headers):
            if path == "/robots.txt":
                return FakeResponse(403, {}, b"")  # unknown robots state
            if path.startswith("/cgi-bin/soap.cgi"):
                raise ConnectionResetError("reset by peer")
            if path.startswith("/cgi-bin/bugreport.cgi"):
                return FakeResponse(
                    200, {"content-type": ["application/mbox"]},
                    MBOX_FIXTURE)
            return FakeResponse(404, {}, b"")
        patch_transport(self, route)
        client = BtsClient(policy=fast_policy())
        r = client.fetch(620025, source="auto")
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.errors[0].code, "transport-error")
        self.assertIn("auto fallback to mbox not attempted",
                      r.errors[0].message)
        self.assertEqual(mbox_calls(), [])

    def test_robots_body_truncated_is_unknown_no_fallback(self):
        # a robots body truncated at robots_max_bytes abstains --
        # trailing Disallow lines may be lost
        huge_robots = b"User-agent: *\n" + b"Disallow: /pad\n" * 9000

        def route(host, path, headers):
            if path == "/robots.txt":
                return FakeResponse(200,
                                    {"content-type": ["text/plain"]},
                                    huge_robots)
            if path.startswith("/cgi-bin/soap.cgi"):
                raise ConnectionResetError("reset by peer")
            return FakeResponse(404, {}, b"")
        patch_transport(self, route)
        client = BtsClient(policy=fast_policy())
        r = client.fetch(620025, source="auto")
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.errors[0].code, "transport-error")
        self.assertIn("auto fallback to mbox not attempted",
                      r.errors[0].message)
        self.assertEqual(mbox_calls(), [])

    def test_mbox_budget_exhaustion_reports_both_attempts(self):
        def route(host, path, headers):
            if path == "/robots.txt":
                return FakeResponse(404, {}, b"")
            if path.startswith("/cgi-bin/soap.cgi"):
                raise ConnectionResetError("reset")
            return FakeResponse(404, {}, b"")
        patch_transport(self, route)
        client = BtsClient(policy=fast_policy(max_requests=2))
        r = client.fetch(620025, source="auto")
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.errors[0].code, "budget-exhausted")
        kinds = [(a["source"], a["error"]["kind"]) for a in r.attempts]
        self.assertEqual(kinds, [("soap", "transport-error"),
                                 ("mbox", "budget-exhausted")])

    def test_both_fail_reports_last_error_with_attempts(self):
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
        client = BtsClient(policy=fast_policy())
        r = client.fetch(620025, source="auto")
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.errors[0].code, "not-found")  # last failing source
        self.assertIn("[auto] both sources failed", r.errors[0].message)
        kinds = [(a["source"], a["error"]["kind"]) for a in r.attempts]
        self.assertEqual(kinds, [("soap", "parse-error"),
                                 ("mbox", "not-found")])


class ExplicitSourceTest(unittest.TestCase):
    def test_mbox_refused_when_robots_disallows(self):
        patch_transport(self, bts_route(pages={
            "/cgi-bin/bugreport.cgi": FakeResponse(
                200, {"content-type": ["application/mbox"]},
                MBOX_FIXTURE)}))
        client = BtsClient(policy=fast_policy())
        r = client.fetch(620025, source="mbox")
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.errors[0].code, "robots-disallow")
        self.assertEqual(mbox_calls(), [])  # refusal precedes any request

    def test_mbox_refused_when_robots_unknown(self):
        def route(host, path, headers):
            if path == "/robots.txt":
                return FakeResponse(500, {}, b"")
            return FakeResponse(404, {}, b"")
        patch_transport(self, route)
        client = BtsClient(policy=fast_policy())
        r = client.fetch(620025, source="mbox")
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.errors[0].code, "robots-disallow")
        self.assertEqual(mbox_calls(), [])

    def test_mbox_succeeds_when_robots_absent(self):
        def route(host, path, headers):
            if path == "/robots.txt":
                return FakeResponse(404, {}, b"")
            if path.startswith("/cgi-bin/bugreport.cgi"):
                return FakeResponse(
                    200, {"content-type": ["application/mbox"]},
                    MBOX_FIXTURE)
            return FakeResponse(404, {}, b"")
        patch_transport(self, route)
        client = BtsClient(policy=fast_policy())
        r = client.fetch(620025, source="mbox")
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.message_count, 3)
        self.assertEqual(r.source_selection["mode"], "explicit")
        self.assertEqual(r.attempts, [{"source": "mbox", "error": None}])

    def test_explicit_failure_has_no_fallback(self):
        soap = FakeResponse(429, {}, b"")
        patch_transport(self, robots_absent_route(soap=soap))
        client = BtsClient(policy=fast_policy())
        r = client.fetch(620025, source="soap")
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.errors[0].code, "rate-limited")
        self.assertEqual(r.attempts[0]["error"]["kind"], "rate-limited")
        self.assertEqual(mbox_calls(), [])


class ResultContractTest(unittest.TestCase):
    def test_to_data_field_set(self):
        patch_transport(self, bts_route())
        client = BtsClient(policy=fast_policy())
        r = client.fetch(620025)
        self.assertIsInstance(r, BtsResult)
        self.assertEqual(set(r.to_data()),
                         {"bug_id", "source", "messages", "message_count",
                          "truncated", "more_available", "provenance"})
        self.assertEqual(r.to_data()["message_count"], 2)
        self.assertEqual(len(r.to_data()["provenance"]), 2)
        # raw bytes never leak into the public schema
        self.assertNotIn("body_bytes", r.to_data())

    def test_message_cap_truncation_maps_more_available(self):
        def route(host, path, headers):
            if path == "/robots.txt":
                return FakeResponse(404, {}, b"")
            if path.startswith("/cgi-bin/soap.cgi"):
                return FakeResponse(
                    200, {"content-type": ["text/xml; charset=utf-8"]},
                    soap_envelope(n_messages=5).encode())
            return FakeResponse(404, {}, b"")
        patch_transport(self, route)
        client = BtsClient(policy=fast_policy(max_messages=2))
        r = client.fetch(620025)
        # adapter stays ok; the ENTRY maps truncated -> partial
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.message_count, 2)
        self.assertTrue(r.truncated)
        self.assertTrue(r.more_available)
        self.assertTrue(any("message cap 2" in note for note in r.notes))
        self.assertEqual(r.parse_meta["messages_total"], 5)
        self.assertEqual(len(r.provenance), 2)

    def test_empty_message_truncation_flags_absent(self):
        envelope = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/'
            'envelope/"><soap:Body><get_bug_logResponse '
            'xmlns="urn:Debbugs/SOAP">'
            '<item><header xsi:type="xsd:string">Subject: s'
            '</header><body xsi:type="xsd:string">b</body>'
            '<msg_num xsi:type="xsd:int">1</msg_num></item>'
            '</get_bug_logResponse></soap:Body></soap:Envelope>')

        def route(host, path, headers):
            if path == "/robots.txt":
                return FakeResponse(404, {}, b"")
            return FakeResponse(
                200, {"content-type": ["text/xml; charset=utf-8"]},
                envelope.encode())
        patch_transport(self, route)
        client = BtsClient(policy=fast_policy())
        r = client.fetch(620025)
        self.assertEqual(r.status, "ok")
        self.assertFalse(r.truncated)
        self.assertFalse(r.more_available)

    def test_errors_are_diagnostic_items_with_stable_codes(self):
        soap = FakeResponse(429, {}, b"")
        patch_transport(self, robots_absent_route(soap=soap))
        client = BtsClient(policy=fast_policy())
        r = client.fetch(620025)
        self.assertEqual(len(r.errors), 1)
        err = r.errors[0]
        self.assertIsInstance(err, DiagnosticItem)
        self.assertEqual(err.code, "rate-limited")
        self.assertEqual(err.where, "gleam.core.sources.bts")
        self.assertEqual(err.details["attempts"],
                         r.attempts)

    def test_sources_vocabulary(self):
        self.assertEqual(BTS_SOURCES, ("auto", "soap", "mbox"))


if __name__ == "__main__":
    unittest.main()
