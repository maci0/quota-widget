"""Release metadata: the version has one source of truth."""

from __future__ import annotations

import json
import re
import tomllib
import unittest

import fetch_quota
from project_paths import project_root


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


class DevDependencyRangeTest(unittest.TestCase):
    """Every gate tool is floored at a reviewed release and capped below its
    next major. `uv add` writes an uncapped range, and a re-lock that widens
    one silently swaps the linter, formatter, or type checker the gate was
    verified against."""

    def setUp(self) -> None:
        pyproject = tomllib.loads(
            (project_root() / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.requirements: list[str] = pyproject["project"]["optional-dependencies"][
            "dev"
        ]

    def test_every_requirement_is_floored_and_capped(self) -> None:
        for requirement in self.requirements:
            name, _, specifier = requirement.partition(">=")
            with self.subTest(requirement=requirement):
                self.assertNotEqual(name, requirement, f"{requirement} has no floor")
                self.assertRegex(
                    specifier,
                    r"^[\d.]+,<\d+$",
                    f"{requirement} must cap below its next major",
                )

    def test_floor_admits_the_locked_release(self) -> None:
        lock = (project_root() / "uv.lock").read_text(encoding="utf-8")
        locked = dict(re.findall(r'^name = "([^"]+)"\nversion = "([^"]+)"', lock, re.M))
        for requirement in self.requirements:
            name, _, specifier = requirement.partition(">=")
            floor = specifier.split(",")[0]
            version = locked[name]
            with self.subTest(requirement=requirement):
                self.assertEqual(
                    version.split(".")[: len(floor.split("."))],
                    floor.split("."),
                    f"{name} {version} is below the declared floor {floor}",
                )


if __name__ == "__main__":
    unittest.main()
