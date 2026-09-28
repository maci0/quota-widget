from __future__ import annotations

import base64
import contextlib
import datetime as dt
import io
import json
import os
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import fetch_quota
import print_smoke

# Fixed clock for every test that cares about expiry or replay.
PINNED_NOW_MS = 1_777_000_000_000

HttpFake = Callable[..., tuple[int, object, object]]

_RATE_LIMIT: dict[str, object] = {"error": {"type": "rate_limit_error"}}
_NEW_TOKENS: dict[str, object] = {
    "access_token": "new-access",
    "refresh_token": "new-refresh",
    "expires_in": 28800,
}


class CodexWindowTest(unittest.TestCase):
    def test_parses_weekly_utilization(self) -> None:
        window = fetch_quota._codex_window(
            {"used_percent": 79, "limit_window_seconds": 604800}, "primary_window"
        )

        self.assertIsNotNone(window)
        assert window is not None
        self.assertEqual(window["label"], "Weekly")
        self.assertEqual(window["util"], 79)

    def test_accepts_fractional_string_percentage(self) -> None:
        window = fetch_quota._codex_window(
            {"used_percent": "78.4", "limit_window_seconds": 604800},
            "primary_window",
        )

        assert window is not None
        self.assertAlmostEqual(window["util"], 78.4)

    def test_clamps_percentage_to_valid_range(self) -> None:
        over = fetch_quota._codex_window({"used_percent": 120}, "primary_window")
        under = fetch_quota._codex_window({"used_percent": -5}, "primary_window")

        assert over is not None
        assert under is not None
        self.assertEqual(over["util"], 100)
        self.assertEqual(under["util"], 0)

    def test_rejects_missing_or_malformed_percentage(self) -> None:
        self.assertIsNone(fetch_quota._codex_window({}, "primary_window"))
        self.assertIsNone(
            fetch_quota._codex_window({"used_percent": "unknown"}, "primary_window")
        )


class CodexResetCreditsTest(unittest.TestCase):
    def test_reported_null_balance_is_shown_as_zero(self) -> None:
        resets = fetch_quota._codex_reset_credits({"rate_limit_reset_credits": None})

        self.assertEqual(resets, {"reported": True, "available": 0, "applicable": 0})

    def test_preserves_reported_counts(self) -> None:
        resets = fetch_quota._codex_reset_credits(
            {
                "rate_limit_reset_credits": {
                    "available_count": 3,
                    "applicable_available_count": 1,
                }
            }
        )

        self.assertEqual(resets, {"reported": True, "available": 3, "applicable": 1})

    def test_unsupported_field_stays_hidden(self) -> None:
        resets = fetch_quota._codex_reset_credits({})

        self.assertEqual(
            resets, {"reported": False, "available": None, "applicable": None}
        )


class RetryAfterTest(unittest.TestCase):
    def test_parses_delta_seconds(self) -> None:
        self.assertEqual(fetch_quota.parse_retry_after("2"), 2.0)
        self.assertEqual(fetch_quota.parse_retry_after("0"), 0.0)
        self.assertIsNone(fetch_quota.parse_retry_after(None))
        self.assertIsNone(fetch_quota.parse_retry_after("nope"))

    def test_parses_http_date_against_the_pinned_clock(self) -> None:
        when = fetch_quota.now_utc() + dt.timedelta(seconds=8)
        header = when.strftime("%a, %d %b %Y %H:%M:%S GMT")
        got = fetch_quota.parse_retry_after(header)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertGreater(got, 5)
        self.assertLess(got, 12)

    def test_http_date_in_the_past_is_zero(self) -> None:
        when = fetch_quota.now_utc() - dt.timedelta(seconds=30)
        header = when.strftime("%a, %d %b %Y %H:%M:%S GMT")
        self.assertEqual(fetch_quota.parse_retry_after(header), 0.0)


class ClockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(os.environ.pop, fetch_quota.NOW_MS_ENV, None)
        os.environ[fetch_quota.NOW_MS_ENV] = str(PINNED_NOW_MS)

    def test_env_pins_the_clock(self) -> None:
        self.assertEqual(fetch_quota.now_ms(), PINNED_NOW_MS)
        self.assertEqual(
            fetch_quota.now_utc(),
            dt.datetime.fromtimestamp(PINNED_NOW_MS / 1000, dt.UTC),
        )

    def test_malformed_override_fails_loud(self) -> None:
        os.environ[fetch_quota.NOW_MS_ENV] = "yesterday"
        with self.assertRaises(ValueError) as ctx:
            fetch_quota.now_ms()
        self.assertIn(fetch_quota.NOW_MS_ENV, str(ctx.exception))

    def test_cache_expires_exactly_at_the_max_age(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.environ["QUOTA_WIDGET_CACHE"] = tmp.name
        self.addCleanup(lambda: os.environ.pop("QUOTA_WIDGET_CACHE", None))
        fetch_quota._write_provider_cache("grok", {"ok": True, "plan": "Grok"})

        os.environ[fetch_quota.NOW_MS_ENV] = str(
            PINNED_NOW_MS + fetch_quota.CACHE_MAX_AGE_S * 1000
        )
        self.assertIsNotNone(fetch_quota._read_provider_cache("grok"))
        os.environ[fetch_quota.NOW_MS_ENV] = str(
            PINNED_NOW_MS + (fetch_quota.CACHE_MAX_AGE_S + 1) * 1000
        )
        self.assertIsNone(fetch_quota._read_provider_cache("grok"))


def _fake_jwt(sub: str) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{payload}.sig"


def _http_returning(status: int, body: object, hdrs: object = None) -> HttpFake:
    def fake_http(
        url: str,
        headers: dict[str, str],
        *,
        timeout: float = 12.0,
        data: bytes | None = None,
        method: str | None = None,
    ) -> tuple[int, object, object]:
        return status, body, hdrs

    return fake_http


class ClaudeRateLimitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["QUOTA_WIDGET_CACHE"] = self.tmp.name
        self.addCleanup(lambda: os.environ.pop("QUOTA_WIDGET_CACHE", None))
        cred = Path(self.tmp.name) / "cred.json"
        cred.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "test-token",
                        "subscriptionType": "pro",
                        "rateLimitTier": "default_claude_pro",
                    }
                }
            )
        )
        self._orig_cred = fetch_quota.CLAUDE_CRED
        fetch_quota.CLAUDE_CRED = cred
        self.addCleanup(self._restore_cred)

    def _restore_cred(self) -> None:
        fetch_quota.CLAUDE_CRED = self._orig_cred

    def test_uses_claude_code_user_agent(self) -> None:
        seen: list[str] = []

        def fake_http(
            url: str,
            headers: dict[str, str],
            *,
            timeout: float = 12.0,
            data: bytes | None = None,
            method: str | None = None,
        ) -> tuple[int, object, object]:
            seen.append(headers.get("User-Agent", ""))
            return 200, {"five_hour": {"utilization": 4}}, None

        with patch.object(fetch_quota, "fetch_http", fake_http):
            out = fetch_quota.fetch_claude()
        self.assertTrue(out["ok"])
        self.assertTrue(seen[0].startswith("claude-code/"))
        self.assertNotEqual(seen[0], fetch_quota.USER_AGENT)

    def test_429_returns_cached_payload(self) -> None:
        fetch_quota._write_provider_cache(
            "claude",
            {
                "ok": True,
                "plan": "Pro",
                "session": {"util": 12, "resets_ms": 1},
                "weekly": [],
            },
        )

        with patch.object(
            fetch_quota,
            "fetch_http",
            _http_returning(429, _RATE_LIMIT, {"Retry-After": "0"}),
        ):
            out = fetch_quota.fetch_claude()
        self.assertTrue(out["ok"])
        self.assertTrue(out["stale"])
        self.assertEqual(out["session"]["util"], 12)

    def test_429_without_cache_is_error(self) -> None:
        with patch.object(
            fetch_quota,
            "fetch_http",
            _http_returning(429, _RATE_LIMIT, {"Retry-After": "0"}),
        ):
            out = fetch_quota.fetch_claude()
        self.assertEqual(out, {"ok": False, "error": "http-429"})

    def test_short_retry_after_retries_once(self) -> None:
        sleeps: list[float] = []
        n = {"i": 0}

        def fake_http(
            url: str,
            headers: dict[str, str],
            *,
            timeout: float = 12.0,
            data: bytes | None = None,
            method: str | None = None,
        ) -> tuple[int, object, object]:
            n["i"] += 1
            if n["i"] == 1:
                return 429, {}, {"Retry-After": "1"}
            return 200, {"five_hour": {"utilization": 3, "resets_at": None}}, None

        with (
            patch.object(fetch_quota, "sleep", sleeps.append),
            patch.object(fetch_quota, "fetch_http", fake_http),
        ):
            out = fetch_quota.fetch_claude()
        self.assertEqual(sleeps, [1.0])
        self.assertTrue(out["ok"])
        self.assertEqual(out["session"]["util"], 3)
        self.assertFalse(out.get("stale"))

    def test_refreshes_expired_oauth_before_usage_call(self) -> None:
        cred_path = fetch_quota.CLAUDE_CRED
        payload = json.loads(cred_path.read_text())
        payload["claudeAiOauth"]["refreshToken"] = "old-refresh"
        payload["claudeAiOauth"]["expiresAt"] = 1
        cred_path.write_text(json.dumps(payload))

        def fake_json(
            url: str,
            headers: dict[str, str],
            *,
            timeout: float = 12.0,
            data: bytes | None = None,
            method: str | None = None,
        ) -> tuple[int, object]:
            self.assertIn("/oauth/token", url)
            return 200, _NEW_TOKENS

        seen_auth: list[str] = []

        def fake_http(
            url: str,
            headers: dict[str, str],
            *,
            timeout: float = 12.0,
            data: bytes | None = None,
            method: str | None = None,
        ) -> tuple[int, object, object]:
            seen_auth.append(headers.get("Authorization", ""))
            return 200, {"five_hour": {"utilization": 9}}, None

        with (
            patch.object(fetch_quota, "fetch_json", fake_json),
            patch.object(fetch_quota, "fetch_http", fake_http),
        ):
            out = fetch_quota.fetch_claude()
        self.assertTrue(out["ok"])
        self.assertEqual(out["session"]["util"], 9)
        self.assertEqual(seen_auth, ["Bearer new-access"])
        saved = json.loads(cred_path.read_text())
        self.assertEqual(saved["claudeAiOauth"]["accessToken"], "new-access")
        self.assertEqual(saved["claudeAiOauth"]["refreshToken"], "new-refresh")

    def test_expired_token_with_429_refresh_is_rate_limited_not_signed_out(
        self,
    ) -> None:
        cred_path = fetch_quota.CLAUDE_CRED
        payload = json.loads(cred_path.read_text())
        payload["claudeAiOauth"]["refreshToken"] = "old-refresh"
        payload["claudeAiOauth"]["expiresAt"] = 1
        cred_path.write_text(json.dumps(payload))

        def fake_json(
            url: str,
            headers: dict[str, str],
            *,
            timeout: float = 12.0,
            data: bytes | None = None,
            method: str | None = None,
        ) -> tuple[int, object]:
            return 429, _RATE_LIMIT

        def fake_http(
            url: str,
            headers: dict[str, str],
            *,
            timeout: float = 12.0,
            data: bytes | None = None,
            method: str | None = None,
        ) -> tuple[int, object, object]:
            return 401, {"error": {"type": "authentication_error"}}, None

        with (
            patch.object(fetch_quota, "fetch_json", fake_json),
            patch.object(fetch_quota, "fetch_http", fake_http),
        ):
            out = fetch_quota.fetch_claude()
        self.assertEqual(out, {"ok": False, "error": "http-429"})

    def test_spend_used_as_number_does_not_crash(self) -> None:
        body = {"five_hour": {"utilization": 4}, "spend": {"used": 12}}
        with patch.object(fetch_quota, "fetch_http", _http_returning(200, body)):
            out = fetch_quota.fetch_claude()
        self.assertTrue(out["ok"])
        self.assertIsNone(out["spend"]["used_minor"])


class CursorParseTest(unittest.TestCase):
    def test_plan_labels(self) -> None:
        self.assertEqual(fetch_quota.cursor_plan_label("pro"), "Pro")
        self.assertEqual(fetch_quota.cursor_plan_label("pro_plus"), "Pro+")
        self.assertEqual(fetch_quota.cursor_plan_label("ultra"), "Ultra")
        self.assertEqual(fetch_quota.cursor_plan_label(None), "Cursor")

    def test_workos_user_id_strips_provider_prefix(self) -> None:
        self.assertEqual(fetch_quota._workos_user_id("auth0|user_01ABC"), "user_01ABC")
        self.assertEqual(fetch_quota._workos_user_id("github|user_01ABC"), "user_01ABC")
        self.assertEqual(fetch_quota._workos_user_id("user_01ABC"), "user_01ABC")

    def test_jwt_sub_from_token(self) -> None:
        token = _fake_jwt("auth0|user_01XYZ")
        self.assertEqual(fetch_quota._jwt_sub(token), "user_01XYZ")

    def test_parse_included_and_on_demand(self) -> None:
        parsed = fetch_quota.parse_cursor_summary(
            {
                "billingCycleStart": "2026-04-02T14:11:55.000Z",
                "billingCycleEnd": "2026-05-02T14:11:55.000Z",
                "membershipType": "pro",
                "limitType": "user",
                "isUnlimited": False,
                "individualUsage": {
                    "plan": {
                        "enabled": True,
                        "used": 200,
                        "limit": 500,
                        "remaining": 300,
                        "autoPercentUsed": 10,
                        "apiPercentUsed": 40,
                        "totalPercentUsed": 40,
                    },
                    "onDemand": {
                        "enabled": True,
                        "used": 2309,
                        "limit": 10000,
                        "remaining": 7691,
                    },
                },
            }
        )
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["plan"], "Pro")
        labels = [p["label"] for p in parsed["periods"]]
        self.assertEqual(labels[:2], ["Included", "Auto + Composer"])
        self.assertEqual(parsed["periods"][0]["util"], 40)
        self.assertEqual(parsed["periods"][0]["used"], 200)
        self.assertEqual(parsed["periods"][0]["limit"], 500)
        on_demand = parsed["periods"][-1]
        self.assertEqual(on_demand["label"], "On-demand")
        self.assertEqual(on_demand["unit"], "cents")
        self.assertEqual(on_demand["util"], 23.1)

    def test_unlimited_has_no_included_bar(self) -> None:
        parsed = fetch_quota.parse_cursor_summary(
            {
                "membershipType": "ultra",
                "isUnlimited": True,
                "individualUsage": {"plan": {"enabled": True, "used": 0, "limit": 0}},
            }
        )
        self.assertTrue(parsed["unlimited"])
        self.assertEqual(parsed["plan"], "Ultra")
        self.assertEqual(parsed["periods"], [])

    def test_enterprise_overall_and_team_on_demand(self) -> None:
        parsed = fetch_quota.parse_cursor_summary(
            {
                "billingCycleEnd": "2026-05-02T14:11:55.000Z",
                "membershipType": "enterprise",
                "limitType": "team",
                "isUnlimited": False,
                "individualUsage": {
                    "overall": {
                        "enabled": True,
                        "used": 195813,
                        "limit": 10000000,
                        "remaining": 9804187,
                    }
                },
                "teamUsage": {
                    "onDemand": {
                        "enabled": True,
                        "used": 14255,
                        "limit": 50000000,
                        "remaining": 49985745,
                    }
                },
            }
        )
        self.assertEqual(parsed["plan"], "Enterprise")
        self.assertEqual(len(parsed["periods"]), 2)
        included, on_demand = parsed["periods"]
        self.assertEqual(included["label"], "Included")
        self.assertEqual(included["unit"], "cents")
        self.assertAlmostEqual(included["util"], 2.0)
        self.assertEqual(on_demand["label"], "On-demand")
        self.assertEqual(on_demand["unit"], "cents")
        self.assertAlmostEqual(on_demand["util"], 0.0)

    def test_fetch_cursor_uses_local_auth_json(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.environ["QUOTA_WIDGET_CACHE"] = tmp.name
        os.environ["CURSOR_AUTH_JSON"] = str(Path(tmp.name) / "auth.json")
        self.addCleanup(lambda: os.environ.pop("QUOTA_WIDGET_CACHE", None))
        self.addCleanup(lambda: os.environ.pop("CURSOR_AUTH_JSON", None))
        Path(os.environ["CURSOR_AUTH_JSON"]).write_text(
            json.dumps({"accessToken": _fake_jwt("auth0|user_01TEST")})
        )

        def fake_json(
            url: str,
            headers: dict[str, str],
            *,
            timeout: float = 12.0,
            data: bytes | None = None,
            method: str | None = None,
        ) -> tuple[int, object]:
            self.assertIn("WorkosCursorSessionToken=", headers.get("Cookie", ""))
            self.assertEqual(url, fetch_quota.CURSOR_SUMMARY_URL)
            return 200, {
                "membershipType": "pro_plus",
                "billingCycleEnd": "2026-05-02T14:11:55.000Z",
                "isUnlimited": False,
                "individualUsage": {
                    "plan": {
                        "enabled": True,
                        "used": 10,
                        "limit": 100,
                        "totalPercentUsed": 10,
                    }
                },
            }

        with patch.object(fetch_quota, "fetch_json", fake_json):
            out = fetch_quota.fetch_cursor()
        self.assertTrue(out["ok"])
        self.assertEqual(out["plan"], "Pro+")
        self.assertEqual(out["periods"][0]["util"], 10)


class IsoToMsTest(unittest.TestCase):
    def test_parses_zulu(self) -> None:
        ms = fetch_quota.iso_to_ms("2026-05-02T14:11:55.000Z")
        self.assertIsNotNone(ms)
        assert ms is not None
        self.assertGreater(ms, 1_700_000_000_000)

    def test_rejects_missing_or_garbage(self) -> None:
        self.assertIsNone(fetch_quota.iso_to_ms(None))
        self.assertIsNone(fetch_quota.iso_to_ms("nope"))


class PlanLabelTest(unittest.TestCase):
    def test_maps_credential_fields(self) -> None:
        self.assertEqual(fetch_quota.plan_label("max", "max_20x"), "Max (20x)")
        self.assertEqual(fetch_quota.plan_label("pro", "default_claude_pro"), "Pro")
        self.assertEqual(fetch_quota.plan_label(None, None), "Claude")


class GrokPeriodTest(unittest.TestCase):
    def test_unified_credits_zero_when_percent_omitted(self) -> None:
        parsed = fetch_quota._parse_grok_period(
            {"isUnifiedBillingUser": True, "currentPeriod": {"type": "WEEKLY"}}
        )
        self.assertEqual(parsed["label"], "Weekly")
        self.assertEqual(parsed["util"], 0.0)

    def test_legacy_monthly_cents(self) -> None:
        parsed = fetch_quota._parse_grok_period({"used": 250, "monthlyLimit": 1000})
        self.assertEqual(parsed["label"], "Monthly")
        self.assertEqual(parsed["util"], 25.0)

    def test_non_dict_current_period(self) -> None:
        parsed = fetch_quota._parse_grok_period(
            {"creditUsagePercent": 12.5, "currentPeriod": "weekly"}
        )
        self.assertEqual(parsed["util"], 12.5)


class JwtGuardTest(unittest.TestCase):
    def test_array_payload_is_ignored(self) -> None:
        payload = base64.urlsafe_b64encode(b"[1,2]").rstrip(b"=").decode()
        token = f"eyJhbGciOiJub25lIn0.{payload}.sig"
        self.assertIsNone(fetch_quota._jwt_exp_ms(token))
        self.assertIsNone(fetch_quota._jwt_claim(token, "sub"))


class ProviderCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["QUOTA_WIDGET_CACHE"] = self.tmp.name
        self.addCleanup(lambda: os.environ.pop("QUOTA_WIDGET_CACHE", None))

    def test_round_trip_and_stale_flag(self) -> None:
        fetch_quota._write_provider_cache("grok", {"ok": True, "plan": "Grok"})
        got = fetch_quota._read_provider_cache("grok")
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got["plan"], "Grok")
        stale = fetch_quota._stale_cache("grok")
        self.assertIsNotNone(stale)
        assert stale is not None
        self.assertTrue(stale["stale"])

    def test_does_not_write_failed_payloads(self) -> None:
        fetch_quota._write_provider_cache("grok", {"ok": False, "error": "net"})
        self.assertIsNone(fetch_quota._read_provider_cache("grok"))


class DurableWriteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "auth.json"

    def test_content_and_directory_are_flushed(self) -> None:
        flushed: list[int] = []
        real_fsync = os.fsync

        def spy(fd: int) -> None:
            flushed.append(fd)
            real_fsync(fd)

        with patch("os.fsync", spy):
            fetch_quota._atomic_write_json(self.path, {"a": 1})

        self.assertEqual(json.loads(self.path.read_text()), {"a": 1})
        self.assertGreaterEqual(len(flushed), 2)  # file, then directory

    def test_failed_write_keeps_previous_file_and_leaves_no_temp(self) -> None:
        fetch_quota._atomic_write_json(self.path, {"tokens": "first"})

        with patch.object(json, "dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                fetch_quota._atomic_write_json(self.path, {"tokens": "second"})

        self.assertEqual(json.loads(self.path.read_text()), {"tokens": "first"})
        self.assertEqual(list(self.path.parent.glob(".*.tmp")), [])

    def test_merge_write_keeps_fields_another_process_added(self) -> None:
        fetch_quota._atomic_write_json(self.path, {"tokens": "old", "other": 1})

        def put_tokens(store: dict[str, object]) -> tuple[str, object]:
            store["tokens"] = "new"
            return "tokens", "new"

        fetch_quota._merge_write_json(self.path, put_tokens)

        self.assertEqual(
            json.loads(self.path.read_text()), {"tokens": "new", "other": 1}
        )

    def test_merge_write_retries_when_a_concurrent_writer_wins(self) -> None:
        fetch_quota._atomic_write_json(self.path, {"tokens": "old"})
        writes: list[int] = []
        real_write = fetch_quota._atomic_write_json

        def racing_write(target: Path, obj: object) -> None:
            real_write(target, obj)
            writes.append(1)
            if len(writes) == 1:
                # The vendor CLI refreshed at the same moment.
                real_write(target, {"tokens": "cli"})

        def put_tokens(store: dict[str, object]) -> tuple[str, object]:
            store["tokens"] = "widget"
            return "tokens", "widget"

        with patch.object(fetch_quota, "_atomic_write_json", racing_write):
            fetch_quota._merge_write_json(self.path, put_tokens)

        self.assertEqual(len(writes), 2)
        self.assertEqual(json.loads(self.path.read_text())["tokens"], "widget")

    def test_merge_write_falls_back_to_the_callers_store(self) -> None:
        fetch_quota._atomic_write_json(self.path, {"tokens": "torn"})

        def put_tokens(store: dict[str, object]) -> tuple[str, object]:
            store["tokens"] = "new"
            return "tokens", "new"

        def unreadable(self: Path) -> str:
            raise PermissionError("denied")

        with patch.object(Path, "read_text", unreadable):
            fetch_quota._merge_write_json(
                self.path, put_tokens, {"tokens": "old", "other": 2}
            )

        self.assertEqual(
            json.loads(self.path.read_text()), {"tokens": "new", "other": 2}
        )


class HttpRetryableTest(unittest.TestCase):
    def test_retryable_status_codes(self) -> None:
        self.assertTrue(fetch_quota._http_retryable(429))
        self.assertTrue(fetch_quota._http_retryable(503))
        self.assertTrue(fetch_quota._http_retryable(500))
        self.assertFalse(fetch_quota._http_retryable(401))
        self.assertFalse(fetch_quota._http_retryable(0))


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


class ReplayTest(unittest.TestCase):
    """One pinned clock value plus one fixed HTTP script must reproduce the
    poll byte-for-byte, cache writes included."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        os.environ["QUOTA_WIDGET_CACHE"] = str(self.root)
        self.addCleanup(lambda: os.environ.pop("QUOTA_WIDGET_CACHE", None))
        os.environ[fetch_quota.NOW_MS_ENV] = str(PINNED_NOW_MS)
        self.addCleanup(lambda: os.environ.pop(fetch_quota.NOW_MS_ENV, None))

        self._rebind("CLAUDE_CRED", {"claudeAiOauth": {"accessToken": _fake_jwt("s")}})
        self._rebind("CODEX_AUTH", {"tokens": {"access_token": _fake_jwt("s")}})
        self._rebind(
            "GROK_AUTH",
            {"x": {"key": "tok", "oidc_client_id": "c", "refresh_token": "r"}},
        )
        self._rebind(
            "CURSOR_AUTH_JSON", {"accessToken": _fake_jwt("auth0|user_01TEST")}
        )

    def _rebind(self, name: str, obj: object) -> None:
        auth = self.root / "auth"
        auth.mkdir(exist_ok=True)
        path = auth / f"{name.lower()}.json"
        path.write_text(json.dumps(obj))
        original = getattr(fetch_quota, name)
        setattr(fetch_quota, name, path)
        self.addCleanup(setattr, fetch_quota, name, original)

    def _fake_http(
        self,
        url: str,
        headers: dict[str, str],
        *,
        timeout: float = 12.0,
        data: bytes | None = None,
        method: str | None = None,
    ) -> tuple[int, object, object]:
        if "anthropic" in url:
            return 200, {"limits": [{"kind": "weekly_all", "percent": 12}]}, None
        if "cursor.com" in url:
            return 200, {"membershipType": "pro", "individualUsage": {}}, None
        return 200, {"rate_limit": {"primary_window": {"used_percent": 5}}}, None

    def _fake_json(
        self,
        url: str,
        headers: dict[str, str],
        *,
        timeout: float = 12.0,
        data: bytes | None = None,
        method: str | None = None,
    ) -> tuple[int, object]:
        if "grok.com" not in url:
            return 200, None
        cfg = (
            {"isUnifiedBillingUser": True, "currentPeriod": {"type": "WEEKLY"}}
            if "format=credits" in url
            else {"used": 250, "monthlyLimit": 1000}
        )
        return 200, {"config": cfg}

    def _poll(self) -> str:
        out = io.StringIO()
        with (
            patch.object(fetch_quota, "fetch_http", self._fake_http),
            patch.object(fetch_quota, "fetch_json", self._fake_json),
            contextlib.redirect_stdout(out),
            self.assertRaises(SystemExit),
        ):
            fetch_quota.main()
        return out.getvalue()

    def test_same_inputs_replay_byte_for_byte(self) -> None:
        first = self._poll()
        second = self._poll()
        self.assertEqual(first, second)
        self.assertEqual(json.loads(first)["fetched_ms"], PINNED_NOW_MS)

    def test_replay_does_not_depend_on_a_running_cache(self) -> None:
        first = self._poll()
        for cached in self.root.glob("*.json"):
            if cached.name.startswith(("claude", "cursor", "grok", "codex")):
                cached.unlink()
        self.assertEqual(self._poll(), first)


if __name__ == "__main__":
    unittest.main()
