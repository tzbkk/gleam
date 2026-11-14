#!/usr/bin/env python
"""Regenerate the golden JSON files in this directory.

Run from the repository root:

    uv run python tests/goldens/regenerate.py

Every file is produced by the REAL entry ``run(...)`` calls defined in
``tests.test_golden`` (mocked externals, zero network) and then
serialized with the golden serializer (``Result.to_json`` plus one
trailing newline). Goldens are pinned evidence: after regenerating,
review the diff deliberately, then commit. Never hand-edit them.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from tests.test_golden import write_goldens  # noqa: E402

if __name__ == "__main__":
    for name in write_goldens():
        print(f"wrote {name}")
