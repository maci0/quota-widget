"""Release metadata: the version has one source of truth."""

from __future__ import annotations

import json
import re
import tomllib
import unittest
from pathlib import Path

import fetch_quota


def project_root() -> Path:
    start = Path(__file__).resolve().parent
    for path in [start, *start.parents]:
        if (path / "package" / "metadata.json").is_file():
            return path
    raise AssertionError("package/metadata.json not found")


class VersionTest(unittest.TestCase):
    """package/metadata.json is the version Plasma shows; nothing may drift
    from it, or a release ships a widget that reports the wrong version."""

    def setUp(self) -> None:
        self.root = project_root()
        metadata = json.loads(
            (self.root / "package" / "metadata.json").read_text(encoding="utf-8")
        )
        self.plugin_version: str = metadata["KPlugin"]["Version"]
        pyproject = tomllib.loads(
            (self.root / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.project_version: str = pyproject["project"]["version"]

    def test_versions_match(self) -> None:
        self.assertEqual(self.project_version, self.plugin_version)

    def test_version_is_semver(self) -> None:
        self.assertRegex(self.plugin_version, r"^\d+\.\d+\.\d+$")

    def test_user_agent_follows_the_package_version(self) -> None:
        self.assertEqual(fetch_quota.USER_AGENT, f"quota-widget/{self.plugin_version}")

    def test_changelog_documents_the_shipped_version(self) -> None:
        changelog = (self.root / "CHANGELOG.md").read_text(encoding="utf-8")
        released = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", changelog, re.MULTILINE)
        self.assertIn(self.plugin_version, released)


if __name__ == "__main__":
    unittest.main()
