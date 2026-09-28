"""The smoke summarizer: a bad dump must name its file, and the exit code
must say whether the fetcher ran at all."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import print_smoke


class PrintSmokeTest(unittest.TestCase):
    def test_missing_dump_names_the_command_to_run(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        missing = Path(tmp.name) / "smoke.json"

        with self.assertRaises(SystemExit) as caught:
            print_smoke._load(missing)
        self.assertIn(str(missing), str(caught.exception))
        self.assertIn("fetch_quota.py", str(caught.exception))

    def test_invalid_json_names_the_file(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dump = Path(tmp.name) / "smoke.json"
        dump.write_text("not json")

        with self.assertRaises(SystemExit) as caught:
            print_smoke._load(dump)
        self.assertIn("not valid JSON", str(caught.exception))

    def test_reads_provider_payload(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dump = Path(tmp.name) / "smoke.json"
        dump.write_text(json.dumps({"claude": {"ok": True, "plan": "Pro"}}))

        self.assertEqual(
            print_smoke._load(dump), {"claude": {"ok": True, "plan": "Pro"}}
        )

    def test_help_exits_zero(self) -> None:
        for flag in ("--help", "-h"):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                with self.assertRaises(SystemExit) as caught:
                    print_smoke.main([flag])
            self.assertEqual(caught.exception.code, 0)
            self.assertIn("usage: print_smoke.py", stdout.getvalue())

    def test_config_error_goes_to_stderr(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dump = Path(tmp.name) / "smoke.json"
        dump.write_text(json.dumps({"ok": False, "config_error": "bad cache path"}))
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                print_smoke.main([str(dump)])
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("bad cache path", stderr.getvalue())

    def test_extra_argument_is_a_usage_error(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                print_smoke.main(["a", "b"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("usage: print_smoke.py", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
