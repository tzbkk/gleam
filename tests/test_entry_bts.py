"""bts entry shell -- shapes, honest statuses, usage errors, zero storage.

No network anywhere: the fetch path is exercised through
``patch("gleam.entries.bts.BtsClient")``; the invalid-input cases fail
in the binder/required checks before any client could be constructed.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from gleam.core.result import DiagnosticItem, GOLDEN_FIELD_ORDER, exit_code_for
from gleam.core.sources.bts import BtsResult
from gleam.entries.bts import KIND_BUG_RESULT, main, run
from tests._helpers import capture_run

BTS_WHERE = "gleam.core.sources.bts"

BTS_DATA_FIELDS = {
    "bug_id",
    "source",
    "messages",
    "message_count",
    "truncated",
    "more_available",
    "provenance",
}


def fake_message(index):
    """One structured BTS message (the message shape)."""
    return {
        "index": index,
        "msg_num": index,
        "message_id": f"<bug-999999-{index}@bugs.debian.org>",
        "date": "Mon, 01 Jan 2024 00:00:00 UTC",
        "date_iso": "2024-01-01T00:00:00+00:00",
        "from": f"Reporter {index} <reporter{index}@example.test>",
        "subject": f"crash report part {index}",
        "subject_decoded": f"crash report part {index}",
        "body_text": f"body of message {index}",
        "body_chars": 18,
        "body_truncated": False,
        "html_only": False,
        "charsets": ["utf-8"],
        "attachments": [],
        "parse_defects": [],
        "urls": ["https://example.test/repro"],
        "source_url": "https://bugs.debian.org/cgi-bin/soap.cgi",
        "raw": {
            "offset": index,
            "bytes": 64,
            "sha256": "a" * 64,
            "boundary": "soap-header-body",
        },
        "provenance_note": "fixture message",
    }


def fake_bts_result(**overrides):
    """A minimal successful adapter result (soap, 2 messages)."""
    fields = dict(
        status="ok",
        bug_id=999999,
        source="soap",
        messages=[fake_message(1), fake_message(2)],
        message_count=2,
        truncated=False,
        more_available=False,
        provenance=["bts:bug=999999#msg=1", "bts:bug=999999#msg=2"],
    )
    fields.update(overrides)
    return BtsResult(**fields)


def run_main(argv):
    """Run main(argv) capturing stdout/stderr; returns (code, out, err)."""
    return capture_run(main, argv)


class BugResultPath(unittest.TestCase):
    """Fetch path over a mocked ``gleam.entries.bts.BtsClient``."""

    ARGV = ["--bug-id", "999999"]

    def _patched_client(self, result):
        patcher = patch("gleam.entries.bts.BtsClient")
        client_cls = patcher.start()
        client_cls.return_value.fetch.return_value = result
        self.addCleanup(patcher.stop)
        return client_cls

    def test_ok_result_json_shape(self):
        client_cls = self._patched_client(fake_bts_result())
        code, out, err = run_main([*self.ARGV, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertNotIn("\n", out.strip())  # single-line JSON
        payload = json.loads(out)
        self.assertEqual(list(payload), list(GOLDEN_FIELD_ORDER))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["kind"], KIND_BUG_RESULT)
        for field in BTS_DATA_FIELDS:
            self.assertIn(field, payload["data"])
        self.assertEqual(payload["data"]["bug_id"], 999999)
        self.assertEqual(payload["data"]["source"], "soap")
        self.assertEqual(payload["data"]["message_count"], 2)
        self.assertEqual(
            payload["data"]["provenance"],
            ["bts:bug=999999#msg=1", "bts:bug=999999#msg=2"],
        )
        self.assertFalse(payload["data"]["truncated"])
        self.assertFalse(payload["data"]["more_available"])
        # client wiring: policy from the closed table defaults
        client_cls.assert_called_once()
        kwargs = client_cls.call_args.kwargs
        self.assertIn("policy", kwargs)
        client_cls.return_value.fetch.assert_called_once_with(999999, "auto")

    def test_explicit_source_is_forwarded(self):
        client_cls = self._patched_client(fake_bts_result())
        code, out, _ = run_main([*self.ARGV, "--source", "mbox", "--json"])
        self.assertEqual(code, 0)
        client_cls.return_value.fetch.assert_called_once_with(999999, "mbox")

    def test_source_from_env_file_is_forwarded(self):
        client_cls = self._patched_client(fake_bts_result())
        with tempfile.TemporaryDirectory() as tmp:
            env_path = os.path.join(tmp, "bts.env")
            with open(env_path, "w", encoding="utf-8") as handle:
                handle.write("GLEAM_BTS_SOURCE=mbox\n")
            code, out, err = run_main(
                [*self.ARGV, "--env-file", env_path, "--json"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        client_cls.return_value.fetch.assert_called_once_with(999999, "mbox")

    def test_truncation_maps_to_partial(self):
        self._patched_client(
            fake_bts_result(truncated=True, more_available=True)
        )
        code, out, _ = run_main([*self.ARGV, "--json"])
        payload = json.loads(out)
        self.assertEqual(payload["status"], "partial")
        self.assertTrue(payload["data"]["truncated"])
        self.assertTrue(payload["data"]["more_available"])
        self.assertEqual(code, exit_code_for("partial"))

    def test_adapter_failure_maps_to_failed_with_lifted_errors(self):
        result = fake_bts_result(
            status="failed",
            messages=[],
            message_count=0,
            provenance=[],
            errors=[
                DiagnosticItem(
                    code="not-found",
                    message="HTTP 404 from BTS",
                    where=BTS_WHERE,
                )
            ],
        )
        self._patched_client(result)
        code, out, _ = run_main([*self.ARGV, "--json"])
        payload = json.loads(out)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["errors"][0]["code"], "not-found")
        self.assertEqual(payload["errors"][0]["where"], BTS_WHERE)
        self.assertEqual(code, exit_code_for("failed"))

    def test_empty_discussion_is_honestly_ok(self):
        self._patched_client(
            fake_bts_result(
                messages=[], message_count=0, provenance=[]
            )
        )
        code, out, _ = run_main([*self.ARGV, "--json"])
        payload = json.loads(out)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["data"]["message_count"], 0)

    def test_function_api_run_returns_the_same_result(self):
        client_cls = self._patched_client(fake_bts_result())
        result = run(bug_id=999999)
        self.assertEqual(result.kind, KIND_BUG_RESULT)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["message_count"], 2)
        client_cls.return_value.fetch.assert_called_once_with(999999, "auto")

    def test_human_render_is_nonempty_and_lists_messages(self):
        self._patched_client(fake_bts_result())
        code, out, _ = run_main(self.ARGV)
        self.assertEqual(code, 0)
        self.assertTrue(out.strip())
        self.assertIn("bts #999999 — 2 messages", out)
        self.assertIn("crash report part 1", out)
        self.assertIn("[1/2]", out)
        self.assertNotIn("bts:bug=", out)  # provenance stays in --json


class UsageErrors(unittest.TestCase):
    """Usage/config failures: exit 2, stderr message, stdout untouched."""

    def test_invalid_bug_ids_no_result_on_stdout(self):
        # Real binder path (no patch): every value violates the frozen
        # bug-id contract and dies before any client could be built.
        for bad in ("abc", "0", "007", "10000000", "-5", "9999999x"):
            with self.subTest(bug_id=bad):
                code, out, err = run_main(["--bug-id", bad, "--json"])
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertTrue(err.strip())

    def test_missing_bug_id(self):
        code, out, err = run_main(["--json"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.strip())

    def test_invalid_source_choice(self):
        code, out, err = run_main(
            ["--bug-id", "999999", "--source", "web", "--json"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.strip())

    def test_unknown_flag(self):
        code, out, err = run_main(["--bug", "999999", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.strip())


class ZeroStorage(unittest.TestCase):
    """The fetch path may create nothing under the working dir."""

    def test_no_files_created_under_the_working_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            before = sorted(os.listdir(tmp))
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with patch("gleam.entries.bts.BtsClient") as client_cls:
                    client_cls.return_value.fetch.return_value = (
                        fake_bts_result()
                    )
                    run_main(["--bug-id", "999999", "--json"])
            finally:
                os.chdir(cwd)
            self.assertEqual(sorted(os.listdir(tmp)), before)
            self.assertEqual(before, [])


if __name__ == "__main__":
    unittest.main()
