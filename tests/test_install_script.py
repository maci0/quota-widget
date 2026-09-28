"""install.sh: root resolution, and the paths that remove or refuse a directory.

`--help` answers before the installer touches the plasmoid directory, the
cache, or the network, so it exercises root resolution and nothing else. The
install path itself polls four providers, so it is not run here; the decisions
around it are a manifest whose Id cannot name a directory, and a destination
holding something this widget did not put there.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from project_paths import project_root

SCRIPT = project_root() / "install.sh"


class InstallScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()

    def checkout(self, plugin_id: str | None) -> Path:
        """A checkout whose install.sh finds_root() resolves to itself."""
        root = self.base / "checkout"
        (root / "package").mkdir(parents=True, exist_ok=True)
        shutil.copy2(SCRIPT, root / "install.sh")
        metadata: dict[str, object] = {
            "KPackageStructure": "Plasma/Applet",
            "KPlugin": {"Name": "AI Quota", "Version": "0.0.0"},
        }
        if plugin_id is not None:
            plugin = metadata["KPlugin"]
            assert isinstance(plugin, dict)
            plugin["Id"] = plugin_id
        (root / "package" / "metadata.json").write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
        )
        (root / "package" / "metainfo.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<component type="desktop-application">\n'
            "  <releases>\n"
            '    <release version="1.2.3" date="2026-09-28"/>\n'
            "  </releases>\n"
            "</component>\n",
            encoding="utf-8",
        )
        return root

    def run_script(self, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is not on PATH")
        return subprocess.run(
            [bash, str(root / "install.sh"), *args],
            capture_output=True,
            text=True,
            check=False,
            env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"},
        )

    def dest(self, plugin_id: str) -> Path:
        return self.home / ".local" / "share" / "plasma" / "plasmoids" / plugin_id

    def place(self, plugin_id: str, owned: bool) -> Path:
        dest = self.dest(plugin_id)
        dest.mkdir(parents=True)
        (dest / "metadata.json").write_text(
            json.dumps(
                {"KPlugin": {"Id": plugin_id if owned else "com.example.other"}}
            ),
            encoding="utf-8",
        )
        (dest / "user-notes.txt").write_text("keep me", encoding="utf-8")
        return dest

    def test_refuses_a_manifest_with_no_id(self) -> None:
        result = self.run_script(self.checkout(None))

        self.assertEqual(result.returncode, 1)
        self.assertIn("KPlugin Id", result.stderr)

    def test_refuses_an_id_that_is_not_a_directory_name(self) -> None:
        for bad in ("../../etc", "com.maci quota-widget", "com.maci..widget", ""):
            with self.subTest(plugin_id=bad):
                result = self.run_script(self.checkout(bad))

                self.assertEqual(result.returncode, 1)
                self.assertIn("KPlugin Id", result.stderr)

    def test_install_refuses_a_directory_it_did_not_create(self) -> None:
        plugin_id = "com.example.widget"
        dest = self.place(plugin_id, owned=False)

        result = self.run_script(self.checkout(plugin_id))

        self.assertEqual(result.returncode, 1)
        self.assertIn("not this widget", result.stderr)
        self.assertTrue((dest / "user-notes.txt").is_file())

    def test_uninstall_refuses_a_directory_it_did_not_create(self) -> None:
        plugin_id = "com.example.widget"
        dest = self.place(plugin_id, owned=False)

        result = self.run_script(self.checkout(plugin_id), "--uninstall")

        self.assertEqual(result.returncode, 1)
        self.assertIn("not this widget", result.stderr)
        self.assertTrue((dest / "user-notes.txt").is_file())

    def test_uninstall_removes_a_matching_directory(self) -> None:
        plugin_id = "com.example.widget"
        dest = self.place(plugin_id, owned=True)

        result = self.run_script(self.checkout(plugin_id), "--uninstall")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(dest.exists())

    def test_uninstall_reports_nothing_to_remove(self) -> None:
        result = self.run_script(self.checkout("com.example.widget"), "--uninstall")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not installed", result.stdout)

    def test_a_second_argument_is_a_usage_error(self) -> None:
        # A mistyped or misplaced second argument stops the run, so the
        # destructive one cannot fire on a line nobody read to the end.
        plugin_id = "com.example.widget"
        dest = self.place(plugin_id, owned=True)

        for args in (("--uninstall", "typo"), ("-u", "--help"), ("--help", "extra")):
            with self.subTest(args=args):
                result = self.run_script(self.checkout(plugin_id), *args)

                self.assertEqual(result.returncode, 2)
                self.assertIn("unexpected argument", result.stderr)
        self.assertTrue(dest.is_dir())

    def test_a_usage_error_points_at_help(self) -> None:
        result = self.run_script(self.checkout("com.example.widget"), "--instal")

        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown argument", result.stderr)
        self.assertIn("try 'install.sh --help'", result.stderr)

    def test_help_lists_every_flag_and_exits_zero(self) -> None:
        result = self.run_script(self.checkout("com.example.widget"), "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        for flag in ("--uninstall", "--help", "--version"):
            self.assertIn(flag, result.stdout)

    def test_version_prints_the_released_version(self) -> None:
        for flag in ("--version", "-V"):
            with self.subTest(flag=flag):
                result = self.run_script(self.checkout("com.example.widget"), flag)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "com.example.widget 1.2.3\n")

    def test_version_fails_when_no_release_is_named(self) -> None:
        # A manifest with no release has no version to answer with, and an
        # empty line would read as one the operator mistyped.
        root = self.checkout("com.example.widget")
        (root / "package" / "metainfo.xml").write_text(
            '<component type="desktop-application"/>\n', encoding="utf-8"
        )

        result = self.run_script(root, "--version")

        self.assertEqual(result.returncode, 1)
        self.assertIn("metainfo.xml", result.stderr)


class FindRootTest(unittest.TestCase):
    """The root walk starts at the script, not at the link that names it.

    A distro package or a convenience link in ~/bin runs this script from
    outside the checkout, and a walk from the link's own directory never
    reaches package/metadata.json.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def _run(self, script: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
        # HOME is read before the argument is dispatched, and set -u would
        # abort on a missing one; the temp dir keeps a stray write harmless.
        env = dict(os.environ, HOME=str(self.tmp))
        # check=False, not an exception: a failing run is a result the caller
        # asserts on, not an error the runner should raise over the output.
        return subprocess.run(
            [str(script), "--help"],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def test_run_from_the_checkout(self) -> None:
        done = self._run(SCRIPT, project_root())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("usage: install.sh", done.stdout)

    def test_absolute_symlink_outside_the_checkout(self) -> None:
        link = self.tmp / "install.sh"
        link.symlink_to(SCRIPT)

        done = self._run(link, self.tmp)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("usage: install.sh", done.stdout)

    def test_relative_symlink_in_a_subdirectory(self) -> None:
        # A packaged link is written relative to its own directory; a relative
        # target must be resolved against the link, not against the cwd.
        nested = self.tmp / "bin"
        nested.mkdir()
        target = nested / "real-install.sh"
        target.symlink_to(os.path.relpath(SCRIPT, nested))
        link = nested / "quota-widget-install"
        link.symlink_to(Path("real-install.sh"))

        done = self._run(link, self.tmp)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("usage: install.sh", done.stdout)

    def test_symlink_chain(self) -> None:
        first = self.tmp / "install.sh"
        first.symlink_to(SCRIPT)
        second = self.tmp / "quota-widget-install"
        second.symlink_to(first)

        done = self._run(second, self.tmp)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_no_project_above_fails_loudly(self) -> None:
        # A link to a copy of the script with no checkout above it is the
        # failure the root walk is there to report, not to install around.
        detached = self.tmp / "detached"
        detached.mkdir()
        script = detached / "install.sh"
        script.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
        script.chmod(0o755)

        done = self._run(script, detached)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("package/metadata.json not found", done.stderr)


if __name__ == "__main__":
    unittest.main()
