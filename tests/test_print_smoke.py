"""The smoke summarizer: a bad dump must name its file, and the exit code
must say whether the fetcher ran at all. The summary itself is one line per
provider on stdout, so the wording of those lines is pinned here too."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import print_smoke


def write_dump(case: unittest.TestCase, payload: object) -> Path:
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    path = Path(tmp.name) / "smoke.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


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
        self.assertIn("try 'print_smoke.py --help'", stderr.getvalue())


class PrintSmokeSummaryTest(unittest.TestCase):
    """One line per provider, in panel order, from a dump the fetcher wrote."""

    def _lines(self, payload: object) -> list[str]:
        dump = write_dump(self, payload)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            print_smoke.main([str(dump)])
        return stdout.getvalue().splitlines()

    def _line(self, payload: object, provider: str) -> str:
        # Every provider always gets a line, so one card's wording is read out
        # of the summary rather than asserted as the whole of it.
        lines = self._lines(payload)
        self.assertEqual(len(lines), 4, lines)
        return next(line for line in lines if line.startswith(f"  {provider}:"))

    def test_a_fresh_reading_names_the_plan_and_the_session(self) -> None:
        self.assertEqual(
            self._line(
                {
                    "claude": {
                        "ok": True,
                        "plan": "Pro",
                        "session": {"util": 12},
                    }
                },
                "claude",
            ),
            "  claude: ok Pro session=12%",
        )

    def test_a_stale_claude_reading_is_marked_stale(self) -> None:
        self.assertEqual(
            self._line(
                {
                    "claude": {
                        "ok": True,
                        "plan": "Max",
                        "session": {"util": 88},
                        "stale": True,
                    }
                },
                "claude",
            ),
            "  claude: ok Max session=88% stale",
        )

    def test_a_signed_out_provider_reports_its_error(self) -> None:
        self.assertEqual(
            self._line({"claude": {"ok": False, "error": "http-401"}}, "claude"),
            "  claude: http-401",
        )

    def test_a_provider_that_answered_nothing_says_empty(self) -> None:
        self.assertEqual(self._line({"grok": {"ok": False}}, "grok"), "  grok: empty")

    def test_every_provider_gets_a_line_in_panel_order(self) -> None:
        self.assertEqual(
            self._lines(
                {
                    "claude": {"ok": True, "plan": "Pro"},
                    "cursor": {
                        "ok": True,
                        "plan": "Pro+",
                        "periods": [
                            {"label": "Included", "util": 30},
                            {"label": "On demand", "util": 0},
                        ],
                    },
                    "grok": {"ok": True, "periods": [{"label": "Credits", "util": 7}]},
                    "codex": {"ok": True, "plan": "Plus", "windows": [{}, {}]},
                }
            ),
            [
                "  claude: ok Pro",
                "  cursor: ok Pro+ Included=30% On demand=0%",
                "  codex: ok Plus windows=2",
                "  grok: ok Credits=7%",
            ],
        )

    def test_a_failed_provider_counts_no_windows(self) -> None:
        # The window count describes a reading; a card that has none shows the
        # error, so printing "windows=2" next to it reads as a live quota.
        self.assertEqual(
            self._line(
                {"codex": {"ok": False, "error": "net", "windows": [{}, {}]}},
                "codex",
            ),
            "  codex: net",
        )

    def test_a_non_ascii_label_reaches_an_ascii_terminal(self) -> None:
        # Plan labels and error text come off the vendor's wire in the
        # vendor's text. Under a C locale the stream encoder is ASCII, and one
        # such label would raise instead of printing the summary.
        dump = write_dump(
            self, {"claude": {"ok": False, "error": "kontoüberschreitung"}}
        )
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
        with patch.object(sys, "stdout", stream):
            print_smoke.main([str(dump)])
        stream.detach()
        lines = raw.getvalue().decode("utf-8").splitlines()
        self.assertEqual(len(lines), 4, lines)
        self.assertEqual(lines[0], "  claude: kontoüberschreitung")


if __name__ == "__main__":
    unittest.main()
