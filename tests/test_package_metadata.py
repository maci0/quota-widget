from __future__ import annotations

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_version_matches_metadata() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    metadata = json.loads((ROOT / "package" / "metadata.json").read_text())
    assert metadata["KPlugin"]["Version"] == pyproject["project"]["version"]
