"""Where the tests read the tree from, resolved once for the whole suite.

`package/metadata.json` is the project marker, so the root is found by walking
up to it rather than by counting parents off `__file__`.
"""

from __future__ import annotations

from pathlib import Path

PACKAGE_METADATA = Path("package") / "metadata.json"


def project_root() -> Path:
    start = Path(__file__).resolve().parent
    for path in (start, *start.parents):
        if (path / PACKAGE_METADATA).is_file():
            return path
    raise AssertionError(f"{PACKAGE_METADATA} not found above {start}")
