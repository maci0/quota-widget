"""install.sh: the paths that remove or refuse a directory.

The install path itself polls four providers, so it is not run here. The
decisions around it are: a manifest whose Id cannot name a directory, and a
destination holding something this widget did not put there.
"""

from __future__ import annotations

import json
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


if __name__ == "__main__":
    unittest.main()
