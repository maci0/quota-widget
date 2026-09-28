from __future__ import annotations

import base64
import datetime as dt
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import fetch_quota


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

    def test_parses_http_date(self) -> None:
        when = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=8)
        header = when.strftime("%a, %d %b %Y %H:%M:%S GMT")
        got = fetch_quota.parse_retry_after(header)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertGreater(got, 5)
        self.assertLess(got, 12)


def _fake_jwt(sub: str) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{payload}.sig"


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

        def fake_http(
            url: str,
            headers: dict[str, str],
            *,
            timeout: float = 12.0,
            data: bytes | None = None,
            method: str | None = None,
        ) -> tuple[int, object, object]:
            return 429, {"error": {"type": "rate_limit_error"}}, {"Retry-After": "0"}

        with patch.object(fetch_quota, "fetch_http", fake_http):
            out = fetch_quota.fetch_claude()
        self.assertTrue(out["ok"])
        self.assertTrue(out["stale"])
        self.assertEqual(out["session"]["util"], 12)

    def test_429_without_cache_is_error(self) -> None:
        def fake_http(
            url: str,
            headers: dict[str, str],
            *,
            timeout: float = 12.0,
            data: bytes | None = None,
            method: str | None = None,
        ) -> tuple[int, object, object]:
            return 429, {"error": {"type": "rate_limit_error"}}, {"Retry-After": "0"}

        with patch.object(fetch_quota, "fetch_http", fake_http):
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
            patch.object(time, "sleep", sleeps.append),
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
            return 200, {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 28800,
            }

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
            return 429, {"error": {"type": "rate_limit_error"}}

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
        def fake_http(
            url: str,
            headers: dict[str, str],
            *,
            timeout: float = 12.0,
            data: bytes | None = None,
            method: str | None = None,
        ) -> tuple[int, object, object]:
            return 200, {"five_hour": {"utilization": 4}, "spend": {"used": 12}}, None

        with patch.object(fetch_quota, "fetch_http", fake_http):
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


class HttpRetryableTest(unittest.TestCase):
    def test_retryable_status_codes(self) -> None:
        self.assertTrue(fetch_quota._http_retryable(429))
        self.assertTrue(fetch_quota._http_retryable(503))
        self.assertTrue(fetch_quota._http_retryable(500))
        self.assertFalse(fetch_quota._http_retryable(401))
        self.assertFalse(fetch_quota._http_retryable(0))


if __name__ == "__main__":
    unittest.main()
