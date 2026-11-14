"""Cross-platform byte determinism for stdout/stderr (IS-3).

Covers the ``gleam.__main__`` startup reconfigure contract (UTF-8,
LF-only, guarded for streams without ``reconfigure``) and the
end-to-end guarantee that ``--json`` output piped to a file contains
zero CR bytes.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from gleam.__main__ import _reconfigure_streams


class _RecordingStream:

    def __init__(self):
        self.calls = []

    def reconfigure(self, **kwargs):
        self.calls.append(kwargs)


class ReconfigureTest(unittest.TestCase):
    """The startup reconfigure pins encoding and newline on both streams."""

    def test_reconfigure_pins_utf8_and_lf_on_stdout_and_stderr(self):
        out, err = _RecordingStream(), _RecordingStream()
        with patch.object(sys, "stdout", out), \
                patch.object(sys, "stderr", err):
            _reconfigure_streams()
        self.assertEqual(
            out.calls, [{"encoding": "utf-8", "newline": "\n"}])
        self.assertEqual(
            err.calls, [{"encoding": "utf-8", "newline": "\n"}])

    def test_streams_without_reconfigure_are_skipped(self):
        class Bare:
            pass

        with patch.object(sys, "stdout", Bare()), \
                patch.object(sys, "stderr", Bare()):
            _reconfigure_streams()  # must not raise


class PipedJsonTest(unittest.TestCase):
    """``python -m gleam udd --list-datasets --json`` piped to a file."""

    def test_piped_json_contains_no_cr_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "piped.json")
            with open(out_path, "wb") as out:
                proc = subprocess.run(
                    [sys.executable, "-m", "gleam", "udd",
                     "--list-datasets", "--json"],
                    stdout=out, stderr=subprocess.PIPE, cwd=tmp,
                    timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(out_path, "rb") as fh:
                raw = fh.read()
        self.assertNotIn(b"\r", raw)
        envelope = json.loads(raw.decode("utf-8"))
        self.assertEqual(envelope["status"], "ok")
        self.assertEqual(envelope["kind"], "list-datasets")


if __name__ == "__main__":
    unittest.main()
