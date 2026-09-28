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


def _semver(version: str) -> tuple[int, int, int]:
    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)


class UnreleasedTest(unittest.TestCase):
    """CONTRIBUTING.md: a change to the fetcher JSON or to a `main.xml` key that
    an older installed widget cannot read is a major. Nothing about the bump is
    mechanical, so the changelog names the next version above its breaking
    entries and this checks the two against each other."""

    def setUp(self) -> None:
        self.root = project_root()
        changelog = (self.root / "CHANGELOG.md").read_text(encoding="utf-8")
        start = changelog.index("## [Unreleased]")
        end = changelog.index("\n## [", start + 1)
        self.unreleased: str = changelog[start:end]
        metadata = json.loads(
            (self.root / "package" / "metadata.json").read_text(encoding="utf-8")
        )
        self.shipped: tuple[int, int, int] = _semver(metadata["KPlugin"]["Version"])

    def _breaking_bullets(self) -> list[str] | None:
        if "### Breaking" not in self.unreleased:
            return None
        section = self.unreleased.split("### Breaking", 1)[1]
        section = re.split(r"^### ", section, maxsplit=1, flags=re.MULTILINE)[0]
        return re.findall(r"^- (.+?)(?=\n- |\Z)", section, re.MULTILINE | re.DOTALL)

    def test_breaking_entries_declare_a_next_major(self) -> None:
        if self._breaking_bullets() is None:
            self.skipTest("no breaking change pending")
        match = re.search(
            r"^Next release: (\d+\.\d+\.\d+)\.", self.unreleased, re.MULTILINE
        )
        assert match is not None, (
            "the Unreleased section holds breaking changes but names no next "
            "version; add a 'Next release: X.Y.Z.' line above them"
        )
        nxt = _semver(match.group(1))
        self.assertGreater(
            nxt, self.shipped, f"next release {match.group(1)} is not ahead"
        )
        self.assertGreater(
            nxt[0], self.shipped[0], "a breaking change needs a major bump"
        )

    def test_breaking_entries_say_what_they_replace(self) -> None:
        bullets = self._breaking_bullets()
        if bullets is None:
            self.skipTest("no breaking change pending")
        self.assertTrue(bullets, "a Breaking heading with no entry under it")
        for bullet in bullets or []:
            with self.subTest(bullet=bullet[:40]):
                self.assertIn(
                    "Before", bullet, "a breaking entry must state the old way"
                )
                self.assertIn("now", bullet, "a breaking entry must state the new one")


class DocumentedContractTest(unittest.TestCase):
    """The tables the release notes promise are in step with the code.

    The Unreleased section tells a user that every knob is listed in the
    README and that the widget settings are documented with their defaults.
    Both are hand-written tables beside code that changes, so nothing but a
    test keeps them true.
    """

    def setUp(self) -> None:
        self.root = project_root()
        self.readme = (self.root / "README.md").read_text(encoding="utf-8")

    def _table_rows(self, header: str) -> list[list[str]]:
        """The cells of every data row of the markdown table headed `header`."""
        _, separator, table = self.readme.partition(f"| {header} |")
        assert separator, f"README has no table headed {header}"
        rows: list[list[str]] = []
        for line in table.splitlines()[1:]:
            if not line:
                continue  # the blank line that closes the table
            if not line.startswith("|"):
                break
            cells = [cell.strip().strip("`") for cell in line.split("|")[1:-1]]
            if set(cells[0]) <= set("-: "):
                continue  # the separator row under the header
            rows.append(cells)
        return rows

    def test_readme_lists_every_knob_and_no_others(self) -> None:
        documented = {
            cells[0]
            for cells in self._table_rows("Variable")
            if cells[0].startswith("QUOTA_")
        }
        self.assertEqual(documented, set(fetch_quota.KNOWN_ENV))

    def test_readme_settings_table_matches_main_xml(self) -> None:
        config = self.root / "package" / "contents" / "config" / "main.xml"
        main_xml = config.read_text(encoding="utf-8")
        entries: dict[str, dict[str, str]] = {}
        for block in re.findall(r"<entry\b.*?</entry>", main_xml, re.DOTALL):
            found = re.search(r'name="([^"]+)"', block)
            assert found is not None, block
            bounds = dict(re.findall(r"<(default|min|max)>([^<]+)</\1>", block))
            entries[found.group(1)] = bounds

        rows = self._table_rows("Setting")
        self.assertEqual({cells[0] for cells in rows}, set(entries))
        for name, default, span, _meaning in rows:
            with self.subTest(setting=name):
                self.assertEqual(default, entries[name].get("default"))
                low, high = entries[name].get("min"), entries[name].get("max")
                if low is not None and high is not None:
                    self.assertEqual(span, f"{low} to {high}")


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
        locked = dict(
            re.findall(r'^name = "([^"]+)"\nversion = "([^"]+)"', lock, re.MULTILINE)
        )
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
