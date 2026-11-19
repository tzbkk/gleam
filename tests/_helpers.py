"""Shared test helpers.

The leading underscore keeps ``unittest discover`` from importing this
module as a test file.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout


def capture_run(entry_main, argv):
    """Run one entry ``main()`` capturing stdout/stderr; returns
    ``(code, out, err)`` (the established test_entry_meta.py pattern)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = entry_main(argv)
    return code, out.getvalue(), err.getvalue()
