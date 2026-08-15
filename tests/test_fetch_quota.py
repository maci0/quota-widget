import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).parents[1] / "package" / "contents" / "code" / "fetch_quota.py"
)
SPEC = importlib.util.spec_from_file_location("fetch_quota", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
fetch_quota = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch_quota)


class CodexWindowTest(unittest.TestCase):
    def test_parses_weekly_utilization(self) -> None:
        window = fetch_quota._codex_window(
            {"used_percent": 79, "limit_window_seconds": 604800}, "primary_window"
        )

        self.assertIsNotNone(window)
        self.assertEqual(window["label"], "Weekly")
        self.assertEqual(window["util"], 79)

    def test_accepts_fractional_string_percentage(self) -> None:
        window = fetch_quota._codex_window(
            {"used_percent": "78.4", "limit_window_seconds": 604800},
            "primary_window",
        )

        self.assertAlmostEqual(window["util"], 78.4)

    def test_clamps_percentage_to_valid_range(self) -> None:
        over = fetch_quota._codex_window({"used_percent": 120}, "primary_window")
        under = fetch_quota._codex_window({"used_percent": -5}, "primary_window")

        self.assertEqual(over["util"], 100)
        self.assertEqual(under["util"], 0)

    def test_rejects_missing_or_malformed_percentage(self) -> None:
        self.assertIsNone(fetch_quota._codex_window({}, "primary_window"))
        self.assertIsNone(
            fetch_quota._codex_window({"used_percent": "unknown"}, "primary_window")
        )


class CodexResetCreditsTest(unittest.TestCase):
    def test_reported_null_balance_is_shown_as_zero(self) -> None:
        resets = fetch_quota._codex_reset_credits(
            {"rate_limit_reset_credits": None}
        )

        self.assertEqual(
            resets, {"reported": True, "available": 0, "applicable": 0}
        )

    def test_preserves_reported_counts(self) -> None:
        resets = fetch_quota._codex_reset_credits(
            {
                "rate_limit_reset_credits": {
                    "available_count": 3,
                    "applicable_available_count": 1,
                }
            }
        )

        self.assertEqual(
            resets, {"reported": True, "available": 3, "applicable": 1}
        )

    def test_unsupported_field_stays_hidden(self) -> None:
        resets = fetch_quota._codex_reset_credits({})

        self.assertEqual(
            resets, {"reported": False, "available": None, "applicable": None}
        )


if __name__ == "__main__":
    unittest.main()
