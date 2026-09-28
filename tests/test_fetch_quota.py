from __future__ import annotations

import base64
import contextlib
import dataclasses
import datetime as dt
import email.message
import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final, Literal
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

JsonDict = dict[str, Any]

_CREDENTIAL_FIELD: Final[dict[str, str]] = {
    "CLAUDE_CRED": "claude_cred",
    "CODEX_AUTH": "codex_auth",
    "GROK_AUTH": "grok_auth",
    "CURSOR_AUTH_JSON": "cursor_auth",
}

_SANDBOX = tempfile.TemporaryDirectory()
_SANDBOX_ENV = {
    "QUOTA_WIDGET_HOME": _SANDBOX.name,
    "QUOTA_WIDGET_CACHE": str(Path(_SANDBOX.name) / "cache"),
}

# Short names a test binds a credential file to, and the env var that carries it.
_CRED_ENV = {
    "CLAUDE_CRED": "QUOTA_WIDGET_CLAUDE_CREDENTIALS",
    "CODEX_AUTH": "QUOTA_WIDGET_CODEX_AUTH",
    "GROK_AUTH": "QUOTA_WIDGET_GROK_AUTH",
    "CURSOR_AUTH_JSON": "QUOTA_WIDGET_CURSOR_AUTH",
}


@contextlib.contextmanager
def point_credential(name: str, path: Path) -> Iterator[None]:
    """Point one credential file at a temp path, then restore the config.

    The fetcher reads credential paths from its Config, so a test swaps the
    field there rather than rebinding a module-level constant.
    """
    original = fetch_quota.config()
    updates: dict[str, Any] = {_CREDENTIAL_FIELD[name]: path}
    fetch_quota._CONFIG = dataclasses.replace(original, **updates)
    try:
        yield
    finally:
        fetch_quota._CONFIG = original


def setUpModule() -> None:
    """Point every test at a sandbox home and cache, never the real ones."""
    for key, value in _SANDBOX_ENV.items():
        os.environ[key] = value
    fetch_quota.load_config()


def tearDownModule() -> None:
    for key in _SANDBOX_ENV:
        os.environ.pop(key, None)
    fetch_quota.load_config()
    _SANDBOX.cleanup()


ConfigPathField = Literal[
    "home",
    "claude_cred",
    "codex_auth",
    "grok_auth",
    "cursor_auth",
    "cursor_state_db",
    "cache_dir",
]


def point_config(case: unittest.TestCase, field: ConfigPathField, path: Path) -> None:
    """Point one path of the active config elsewhere, then restore the config."""
    cfg = fetch_quota.config()
    fetch_quota._CONFIG = fetch_quota.Config(
        home=path if field == "home" else cfg.home,
        claude_cred=path if field == "claude_cred" else cfg.claude_cred,
        codex_auth=path if field == "codex_auth" else cfg.codex_auth,
        grok_auth=path if field == "grok_auth" else cfg.grok_auth,
        cursor_auth=path if field == "cursor_auth" else cfg.cursor_auth,
        cursor_state_db=path if field == "cursor_state_db" else cfg.cursor_state_db,
        cache_dir=path if field == "cache_dir" else cfg.cache_dir,
        http_timeout_s=cfg.http_timeout_s,
        cache_max_age_s=cfg.cache_max_age_s,
    )
    case.addCleanup(fetch_quota.load_config)


@contextlib.contextmanager
def config_env(**env: str) -> Iterator[None]:
    """Apply environment overrides for one test, then restore the config."""
    saved = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    fetch_quota.load_config()
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        fetch_quota.load_config()


# Credential paths live in the Config record; tests name the file by the
# provider and point the environment variable behind it.
CREDENTIAL_ENV = {
    "CLAUDE_CRED": "QUOTA_WIDGET_CLAUDE_CREDENTIALS",
    "CODEX_AUTH": "QUOTA_WIDGET_CODEX_AUTH",
    "GROK_AUTH": "QUOTA_WIDGET_GROK_AUTH",
    "CURSOR_AUTH_JSON": "QUOTA_WIDGET_CURSOR_AUTH",
    "CURSOR_STATE_DB": "QUOTA_WIDGET_CURSOR_STATE_DB",
}


def credential_env(name: str, path: Path) -> Any:
    return config_env(**{CREDENTIAL_ENV[name]: str(path)})


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

    def test_codex_expiry_reads_the_pinned_clock(self) -> None:
        # Expires 30 s after the pinned instant: expired on the pinned clock,
        # still valid on the real one whenever the two disagree.
        exp_s = (PINNED_NOW_MS + 30_000) // 1000
        tokens = {"access_token": _jwt_with_exp(exp_s)}
        self.assertTrue(fetch_quota._codex_token_expired(tokens))

    def test_codex_expiry_skew_counts_from_the_pinned_clock(self) -> None:
        exp_s = (PINNED_NOW_MS + fetch_quota.TOKEN_SKEW_S * 1000 + 1000) // 1000
        tokens = {"access_token": _jwt_with_exp(exp_s)}
        self.assertFalse(fetch_quota._codex_token_expired(tokens))

    def test_codex_token_expiry_follows_the_pinned_clock(self) -> None:
        # Every wall-clock read goes through now_ms(), so a replayed poll makes
        # the same refresh decision on every run.
        exp_s = (PINNED_NOW_MS // 1000) + 3600
        self.assertFalse(
            fetch_quota._codex_token_expired({"access_token": _jwt_with_exp(exp_s)})
        )
        self.assertTrue(
            fetch_quota._codex_token_expired(
                {"access_token": _jwt_with_exp((PINNED_NOW_MS // 1000) - 1)}
            )
        )

    def test_cache_expires_exactly_at_the_max_age(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.environ["QUOTA_WIDGET_CACHE"] = tmp.name
        self.addCleanup(lambda: os.environ.pop("QUOTA_WIDGET_CACHE", None))
        account = fetch_quota._account_id(_fake_jwt("user_01GROK"))
        fetch_quota._write_provider_cache("grok", {"ok": True, "plan": "Grok"}, account)

        os.environ[fetch_quota.NOW_MS_ENV] = str(
            PINNED_NOW_MS + fetch_quota.DEFAULT_CACHE_MAX_AGE_S * 1000
        )
        self.assertIsNotNone(fetch_quota._read_provider_cache("grok", account))
        os.environ[fetch_quota.NOW_MS_ENV] = str(
            PINNED_NOW_MS + (fetch_quota.DEFAULT_CACHE_MAX_AGE_S + 1) * 1000
        )
        self.assertIsNone(fetch_quota._read_provider_cache("grok", account))
        self.assertFalse((Path(tmp.name) / "grok.json").exists())


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
        cred = Path(self.tmp.name) / "cred.json"
        self.access_token = _fake_jwt("user_01CLAUDE")
        cred.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": self.access_token,
                        "subscriptionType": "pro",
                        "rateLimitTier": "default_claude_pro",
                    }
                }
            )
        )
        self.env = config_env(
            QUOTA_WIDGET_CACHE=self.tmp.name,
            QUOTA_WIDGET_CLAUDE_CREDENTIALS=str(cred),
        )
        self.env.__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)

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
            fetch_quota._account_id(self.access_token),
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

    def test_429_never_serves_another_accounts_payload(self) -> None:
        fetch_quota._write_provider_cache(
            "claude",
            {
                "ok": True,
                "plan": "Max",
                "session": {"util": 88, "resets_ms": 1},
                "weekly": [],
            },
            fetch_quota._account_id(_fake_jwt("user_01OTHER")),
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
        self.assertEqual(out, {"ok": False, "error": "http-429"})

    def test_credential_without_account_id_writes_no_cache(self) -> None:
        cred_path = fetch_quota.config().claude_cred
        payload = json.loads(cred_path.read_text())
        payload["claudeAiOauth"]["accessToken"] = "opaque-token"
        cred_path.write_text(json.dumps(payload))

        def fake_http(
            url: str,
            headers: dict[str, str],
            *,
            timeout: float = 12.0,
            data: bytes | None = None,
            method: str | None = None,
        ) -> tuple[int, object, object]:
            return 200, {"five_hour": {"utilization": 5}}, None

        with patch.object(fetch_quota, "fetch_http", fake_http):
            out = fetch_quota.fetch_claude()
        self.assertTrue(out["ok"])
        self.assertFalse((Path(self.tmp.name) / "claude.json").exists())

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
        cred_path = fetch_quota.config().claude_cred
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
        cred_path = fetch_quota.config().claude_cred
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
        auth_json = Path(tmp.name) / "auth.json"
        auth_json.write_text(
            json.dumps({"accessToken": _fake_jwt("auth0|user_01TEST")})
        )
        env = config_env(
            QUOTA_WIDGET_CACHE=tmp.name, QUOTA_WIDGET_CURSOR_AUTH=str(auth_json)
        )
        env.__enter__()
        self.addCleanup(env.__exit__, None, None, None)

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


class SecondsToMsTest(unittest.TestCase):
    def test_keeps_the_millisecond_a_truncating_cast_drops(self) -> None:
        # 1777000000.001 is representable but lands a hair under when divided
        # back out; int() truncation would report the previous millisecond.
        seconds = 1777000000.001
        self.assertEqual(fetch_quota.ms_from_seconds(seconds), 1_777_000_000_001)

    def test_iso_timestamp_keeps_its_exact_millisecond(self) -> None:
        when = dt.datetime(2026, 5, 2, 14, 11, 55, tzinfo=dt.UTC)
        iso = when.isoformat().replace("+00:00", "Z")
        self.assertEqual(
            fetch_quota.iso_to_ms(iso),
            int(when.timestamp() * 1000),
        )
        self.assertEqual(
            fetch_quota.ms_from_seconds(when.timestamp()),
            fetch_quota.iso_to_ms(iso),
        )

    def test_codex_reset_keeps_its_millisecond(self) -> None:
        window = fetch_quota._codex_window(
            {
                "used_percent": 1,
                "limit_window_seconds": 3600,
                "reset_at": 1777000000.001,
            },
            "primary_window",
        )
        assert window is not None
        self.assertEqual(window["resets_ms"], 1_777_000_000_001)


class NonFiniteReadingTest(unittest.TestCase):
    """json.loads accepts NaN and 1e400, and json.dumps writes them back as
    bare NaN/Infinity, which plasmashell cannot parse. A missing reading must
    read as absent, never as a clamped 0% or a full 100%."""

    def test_codex_util_percent_rejects_non_finite(self) -> None:
        for raw in (float("nan"), float("inf"), -float("inf")):
            self.assertIsNone(
                fetch_quota._codex_window({"used_percent": raw}, "primary_window")
            )

    def test_codex_window_still_parses_whole_seconds(self) -> None:
        window = fetch_quota._codex_window(
            {"used_percent": 42, "limit_window_seconds": 604800}, "primary_window"
        )
        assert window is not None
        self.assertEqual(window["util"], 42.0)
        self.assertEqual(window["window_seconds"], 604800)

    def test_cursor_meter_rejects_non_finite_amounts(self) -> None:
        meter = fetch_quota._cursor_meter(
            {"enabled": True, "used": float("nan"), "limit": 100},
            "Included",
            "cents",
            1,
        )
        self.assertIsNotNone(meter)
        assert meter is not None
        self.assertIsNone(meter["util"])
        self.assertIsNone(meter["used"])

    def test_money_value_rounds_to_the_nearest_cent(self) -> None:
        self.assertEqual(fetch_quota._money_val(249.9999999), 250)
        self.assertEqual(fetch_quota._money_val({"val": 100.5}), 100)
        self.assertIsNone(fetch_quota._money_val(float("nan")))
        self.assertIsNone(fetch_quota._money_val(float("inf")))

    def test_grok_period_never_divides_by_a_missing_limit(self) -> None:
        self.assertIsNone(
            fetch_quota._parse_grok_period({"used": 250, "monthlyLimit": 0})["util"]
        )
        self.assertIsNone(
            fetch_quota._parse_grok_period({"used": 250, "monthlyLimit": -100})["util"]
        )

    def test_grok_period_reports_over_limit_spend(self) -> None:
        period = fetch_quota._parse_grok_period({"used": 1250, "monthlyLimit": 1000})
        self.assertEqual(period["util"], 125.0)

    def test_grok_credit_percent_rejects_non_finite(self) -> None:
        period = fetch_quota._parse_grok_period(
            {"creditUsagePercent": float("nan"), "currentPeriod": "weekly"}
        )
        self.assertIsNone(period["util"])

    def test_poll_output_stays_parseable_json(self) -> None:
        parsed = fetch_quota.parse_cursor_summary(
            {
                "membershipType": "pro",
                "individualUsage": {
                    "plan": {"enabled": True, "used": float("inf"), "limit": 100}
                },
            }
        )
        encoded = json.dumps(parsed)
        self.assertNotIn("Infinity", encoded)
        self.assertNotIn("NaN", encoded)
        self.assertEqual(json.loads(encoded), parsed)


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


class GrokNoPeriodTest(unittest.TestCase):
    """A 200 with no meter in it must not be reported as the failure."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        auth = Path(tmp.name) / "grok.json"
        auth.write_text(json.dumps({"cli::c": {"key": "tok"}}))
        self.env = config_env(
            QUOTA_WIDGET_CACHE=tmp.name, QUOTA_WIDGET_GROK_AUTH=str(auth)
        )
        self.env.__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)

    def _fetch(self, week: int, week_body: object, month: int) -> JsonDict:
        def fake_json(
            url: str,
            headers: dict[str, str],
            *,
            timeout: float = 12.0,
            data: bytes | None = None,
            method: str | None = None,
        ) -> tuple[int, object]:
            if "format=credits" in url:
                return week, week_body
            return month, {"config": {"used": 1, "monthlyLimit": 0}}

        with patch.object(fetch_quota, "fetch_json", fake_json):
            return fetch_quota.fetch_grok()

    def test_ok_call_with_no_period_reports_the_failing_status(self) -> None:
        out = self._fetch(200, {"config": {"used": 5}}, 503)
        self.assertEqual(out, {"ok": False, "error": "http-503"})

    def test_signed_out_reports_401(self) -> None:
        out = self._fetch(401, None, 200)
        self.assertEqual(out, {"ok": False, "error": "http-401"})

    def test_offline_reports_net(self) -> None:
        out = self._fetch(0, None, 0)
        self.assertEqual(out, {"ok": False, "error": "net"})


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
        self.env = config_env(QUOTA_WIDGET_CACHE=self.tmp.name)
        self.env.__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)
        self.account = "acct-1"

    def test_round_trip_and_stale_flag(self) -> None:
        fetch_quota._write_provider_cache(
            "grok", {"ok": True, "plan": "Grok"}, self.account
        )
        got = fetch_quota._read_provider_cache("grok", self.account)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got["plan"], "Grok")
        stale = fetch_quota._stale_cache("grok", self.account)
        self.assertIsNotNone(stale)
        assert stale is not None
        self.assertTrue(stale["stale"])

    def test_does_not_write_failed_payloads(self) -> None:
        fetch_quota._write_provider_cache(
            "grok", {"ok": False, "error": "net"}, self.account
        )
        self.assertIsNone(fetch_quota._read_provider_cache("grok", self.account))

    def test_other_account_cannot_read_the_entry(self) -> None:
        fetch_quota._write_provider_cache(
            "grok", {"ok": True, "plan": "Grok"}, self.account
        )
        self.assertIsNone(fetch_quota._read_provider_cache("grok", "acct-2"))
        self.assertIsNone(fetch_quota._stale_cache("grok", "acct-2"))

    def test_unidentifiable_caller_reads_nothing(self) -> None:
        fetch_quota._write_provider_cache(
            "grok", {"ok": True, "plan": "Grok"}, self.account
        )
        self.assertIsNone(fetch_quota._read_provider_cache("grok", None))

    def test_entry_older_than_the_stale_window_is_ignored(self) -> None:
        fetch_quota._write_provider_cache(
            "grok", {"ok": True, "plan": "Grok"}, self.account
        )
        path = Path(self.tmp.name) / "grok.json"
        entry = json.loads(path.read_text())
        entry["cached_ms"] = int(
            (
                dt.datetime.now(dt.UTC).timestamp()
                - fetch_quota.config().cache_max_age_s
                - 60
            )
            * 1000
        )
        path.write_text(json.dumps(entry))
        self.assertIsNone(fetch_quota._stale_cache("grok", self.account))


class AccountIdTest(unittest.TestCase):
    def test_survives_access_token_rotation(self) -> None:
        first = fetch_quota._account_id(_fake_jwt("user_01CLAUDE"))
        second = fetch_quota._account_id(_fake_jwt("user_01CLAUDE"))
        self.assertIsNotNone(first)
        self.assertEqual(first, second)

    def test_distinguishes_accounts(self) -> None:
        self.assertNotEqual(
            fetch_quota._account_id(_fake_jwt("user_01A")),
            fetch_quota._account_id(_fake_jwt("user_01B")),
        )

    def test_opaque_token_falls_back_to_provider_account_id(self) -> None:
        self.assertEqual(
            fetch_quota._account_id("opaque", "acct-9"),
            fetch_quota._digest("acct-9"),
        )

    def test_nothing_identifiable_is_none(self) -> None:
        self.assertIsNone(fetch_quota._account_id("opaque"))
        self.assertIsNone(fetch_quota._account_id(None))


class ErrorBodyTest(unittest.TestCase):
    """An HTTP error body can echo the account id or email; keep it out of the
    result, since no caller reads it."""

    def _error(self, body: bytes) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            "https://api.anthropic.com/api/oauth/usage",
            429,
            "Too Many Requests",
            email.message.Message(),
            io.BytesIO(body),
        )

    def test_error_body_is_not_returned(self) -> None:
        body = b'{"error":{"message":"user_01ABC@example.com has too many requests"}}'
        with patch.object(urllib.request, "urlopen", side_effect=self._error(body)):
            status, data, _hdrs = fetch_quota.fetch_http("https://example.test", {})

        self.assertEqual(status, 429)
        self.assertIsNone(data)

    def test_error_body_is_not_emitted(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.environ["QUOTA_WIDGET_CACHE"] = tmp.name
        self.addCleanup(lambda: os.environ.pop("QUOTA_WIDGET_CACHE", None))
        cred = Path(tmp.name) / "cred.json"
        cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok"}}))
        os.environ["QUOTA_WIDGET_CLAUDE_CREDENTIALS"] = str(cred)
        self.addCleanup(lambda: os.environ.pop("QUOTA_WIDGET_CLAUDE_CREDENTIALS", None))
        body = b"account user_01ABC@example.com not found"
        out = io.StringIO()
        with (
            credential_env("CLAUDE_CRED", cred),
            patch.object(urllib.request, "urlopen", side_effect=self._error(body)),
            contextlib.redirect_stdout(out),
            self.assertRaises(SystemExit),
        ):
            fetch_quota.main([])

        self.assertNotIn("user_01ABC", out.getvalue())
        self.assertEqual(json.loads(out.getvalue())["claude"]["error"], "http-429")

    def test_error_response_is_closed(self) -> None:
        error = self._error(b"nope")
        with patch.object(urllib.request, "urlopen", side_effect=error):
            status, body, _hdrs = fetch_quota.fetch_http("https://example.test", {})

        self.assertEqual(status, 429)
        self.assertIsNone(body)
        self.assertTrue(error.fp.closed)


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

    def test_written_file_uses_lf_on_every_platform(self) -> None:
        fetch_quota._atomic_write_json(self.path, {"a": 1})

        self.assertNotIn(b"\r", self.path.read_bytes())

    def test_failed_write_keeps_previous_file_and_leaves_no_temp(self) -> None:
        fetch_quota._atomic_write_json(self.path, {"tokens": "first"})

        with patch.object(json, "dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                fetch_quota._atomic_write_json(self.path, {"tokens": "second"})

        self.assertEqual(json.loads(self.path.read_text()), {"tokens": "first"})
        self.assertEqual(list(self.path.parent.glob(".*.tmp")), [])

    def test_unserializable_value_keeps_previous_file_and_leaves_no_temp(self) -> None:
        fetch_quota._atomic_write_json(self.path, {"tokens": "first"})

        with self.assertRaises(TypeError):
            fetch_quota._atomic_write_json(self.path, {"tokens": object()})

        self.assertEqual(json.loads(self.path.read_text()), {"tokens": "first"})
        self.assertEqual(list(self.path.parent.glob(".*.tmp")), [])

    def test_non_ascii_is_written_as_utf8(self) -> None:
        fetch_quota._atomic_write_json(self.path, {"plan": "Ünïcode"})

        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8")), {"plan": "Ünïcode"}
        )

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

        def unreadable(self: Path, **kwargs: object) -> str:
            raise PermissionError("denied")

        with patch.object(Path, "read_text", unreadable):
            fetch_quota._merge_write_json(
                self.path, put_tokens, {"tokens": "old", "other": 2}
            )

        self.assertEqual(
            json.loads(self.path.read_text()), {"tokens": "new", "other": 2}
        )


class ConfigTest(unittest.TestCase):
    """Configuration is read once, validated, and never silently repaired."""

    def setUp(self) -> None:
        self.addCleanup(fetch_quota.load_config)

    def test_defaults_derive_from_home(self) -> None:
        cfg = fetch_quota.load_config({"QUOTA_WIDGET_HOME": "/home/widget"})
        self.assertEqual(cfg.home, Path("/home/widget"))
        self.assertEqual(
            cfg.claude_cred, Path("/home/widget/.claude/.credentials.json")
        )
        self.assertEqual(cfg.codex_auth, Path("/home/widget/.codex/auth.json"))
        self.assertEqual(cfg.grok_auth, Path("/home/widget/.grok/auth.json"))
        self.assertEqual(cfg.cache_dir, Path("/home/widget/.cache/quota-widget"))
        self.assertEqual(cfg.http_timeout_s, fetch_quota.DEFAULT_HTTP_TIMEOUT_S)
        self.assertEqual(cfg.cache_max_age_s, fetch_quota.DEFAULT_CACHE_MAX_AGE_S)

    def test_xdg_cache_home_is_honored(self) -> None:
        cfg = fetch_quota.load_config(
            {
                "QUOTA_WIDGET_HOME": "/home/widget",
                "XDG_CACHE_HOME": "/xdg/cache",
            }
        )
        self.assertEqual(cfg.cache_dir, Path("/xdg/cache/quota-widget"))

    def test_relative_xdg_dirs_are_ignored(self) -> None:
        cfg = fetch_quota.load_config(
            {
                "QUOTA_WIDGET_HOME": "/home/widget",
                "XDG_CACHE_HOME": "relative/cache",
                "XDG_CONFIG_HOME": "relative/config",
            }
        )
        self.assertEqual(cfg.cache_dir, Path("/home/widget/.cache/quota-widget"))
        self.assertEqual(cfg.cursor_auth, Path("/home/widget/.config/cursor/auth.json"))
        self.assertEqual(
            cfg.cursor_state_db,
            Path("/home/widget/.config/Cursor/User/globalStorage/state.vscdb"),
        )

    def test_overrides_win_over_defaults(self) -> None:
        cfg = fetch_quota.load_config(
            {
                "QUOTA_WIDGET_HOME": "/home/widget",
                "QUOTA_WIDGET_CACHE": "/var/tmp/qw",
                "QUOTA_WIDGET_CURSOR_AUTH": "/opt/cursor/auth.json",
                "QUOTA_WIDGET_HTTP_TIMEOUT": "3.5",
                "QUOTA_WIDGET_CACHE_MAX_AGE_S": "60",
            }
        )
        self.assertEqual(cfg.cache_dir, Path("/var/tmp/qw"))
        self.assertEqual(cfg.cursor_auth, Path("/opt/cursor/auth.json"))
        self.assertEqual(cfg.http_timeout_s, 3.5)
        self.assertEqual(cfg.cache_max_age_s, 60)

    def test_empty_override_is_rejected(self) -> None:
        with self.assertRaises(fetch_quota.ConfigError) as ctx:
            fetch_quota.load_config({"QUOTA_WIDGET_CACHE": "  "})
        self.assertIn("QUOTA_WIDGET_CACHE", str(ctx.exception))

    def test_relative_path_is_rejected(self) -> None:
        with self.assertRaises(fetch_quota.ConfigError) as ctx:
            fetch_quota.load_config({"QUOTA_WIDGET_CLAUDE_CREDENTIALS": "creds.json"})
        self.assertIn("absolute", str(ctx.exception))

    def test_timeout_must_be_a_number_in_range(self) -> None:
        for bad in ("twelve", "0", "-1", "600"):
            with self.subTest(bad=bad), self.assertRaises(fetch_quota.ConfigError):
                fetch_quota.load_config({"QUOTA_WIDGET_HTTP_TIMEOUT": bad})

    def test_cache_max_age_must_be_whole_seconds_in_range(self) -> None:
        for bad in ("twelve", "0", "-1", "86401", "0.5"):
            with self.subTest(bad=bad), self.assertRaises(fetch_quota.ConfigError):
                fetch_quota.load_config({"QUOTA_WIDGET_CACHE_MAX_AGE_S": bad})

    def test_describe_exposes_paths_only(self) -> None:
        described = fetch_quota.load_config(
            {"QUOTA_WIDGET_HOME": "/home/widget"}
        ).describe()
        self.assertEqual(described["cache_dir"], "/home/widget/.cache/quota-widget")
        self.assertNotIn("token", json.dumps(described).lower())

    def test_bad_config_fails_before_any_provider_runs(self) -> None:
        stderr = io.StringIO()
        with (
            patch.dict(os.environ, {"QUOTA_WIDGET_CACHE": "relative/path"}),
            patch.object(fetch_quota, "fetch_claude") as claude,
            patch.object(fetch_quota, "fetch_cursor") as cursor,
            patch.object(fetch_quota, "fetch_grok") as grok,
            patch.object(fetch_quota, "fetch_codex") as codex,
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as ctx,
        ):
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                fetch_quota.main([])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("QUOTA_WIDGET_CACHE", stderr.getvalue())
        for provider in (claude, cursor, grok, codex):
            provider.assert_not_called()
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "config")
        self.assertEqual(payload["claude"]["error"], "config")

    def test_bad_clock_override_is_a_config_error(self) -> None:
        stderr = io.StringIO()
        with (
            patch.dict(os.environ, {fetch_quota.NOW_MS_ENV: "yesterday"}),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as ctx,
        ):
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                fetch_quota.main([])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn(fetch_quota.NOW_MS_ENV, stderr.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["error"], "config")
        self.assertIn(fetch_quota.NOW_MS_ENV, payload["config_error"])

    def test_print_config_emits_describe(self) -> None:
        with patch.dict(os.environ, {"QUOTA_WIDGET_HOME": "/home/widget"}):
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                with self.assertRaises(SystemExit):
                    fetch_quota.main(["--print-config"])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["config"]["home"], "/home/widget")

    def test_unknown_argument_exits_nonzero(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                fetch_quota.main(["--nope"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("usage: fetch_quota.py", stderr.getvalue())

    def test_help_exits_zero_and_lists_the_flags(self) -> None:
        for flag in ("--help", "-h"):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                with self.assertRaises(SystemExit) as ctx:
                    fetch_quota.main([flag])
            self.assertEqual(ctx.exception.code, 0)
            self.assertIn("--print-config", stdout.getvalue())

    def test_help_survives_a_broken_config(self) -> None:
        with (
            patch.dict(os.environ, {"QUOTA_WIDGET_CACHE": "relative/path"}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as ctx:
                fetch_quota.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)


class Utf8StateFileTest(unittest.TestCase):
    """The credential, cache, and state files are UTF-8 whatever the locale is.

    A plasmashell started without LANG gets a C locale, where open()'s default
    is ASCII: a store holding a non-ASCII account name then fails to decode,
    and the merge-write fallback rewrites the file without the parts it could
    not read, dropping the other CLI's tokens from a file it shares.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "auth.json"
        self.c_locale = patch.multiple(
            "locale",
            getencoding=lambda: "ascii",
            getpreferredencoding=lambda do_setlocale=True: "ascii",
        )
        self.c_locale.start()
        self.addCleanup(self.c_locale.stop)

    def test_merge_write_keeps_non_ascii_fields_of_a_shared_store(self) -> None:
        self.path.write_bytes(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "old",
                        "accountName": "Ünïcodé ⛅",
                    },
                    "otherCli": {"token": "keep-me"},
                },
                ensure_ascii=False,
            ).encode("utf-8")
        )

        def rotate(store: dict[str, object]) -> tuple[str, object]:
            oauth = dict(store["claudeAiOauth"])  # type: ignore[call-overload]
            oauth["accessToken"] = "new"
            store["claudeAiOauth"] = oauth
            return "claudeAiOauth", oauth

        fetch_quota._merge_write_json(self.path, rotate)

        self.assertEqual(
            json.loads(fetch_quota._read_text(self.path)),
            {
                "claudeAiOauth": {"accessToken": "new", "accountName": "Ünïcodé ⛅"},
                "otherCli": {"token": "keep-me"},
            },
        )

    def test_atomic_write_round_trips_non_ascii_under_a_c_locale(self) -> None:
        store = {"accountName": "Ünïcodé ⛅"}

        fetch_quota._atomic_write_json(self.path, store)

        self.assertEqual(json.loads(fetch_quota._read_text(self.path)), store)

    def test_provider_cache_reads_non_ascii_labels_under_a_c_locale(self) -> None:
        payload = {"ok": True, "plan": "Max (20x) – Ünïcodé"}
        account = fetch_quota._account_id(_fake_jwt("user_01UTF8"))
        with config_env(QUOTA_WIDGET_CACHE=self.tmp.name):
            with patch.object(fetch_quota, "now_ms", return_value=PINNED_NOW_MS):
                fetch_quota._write_provider_cache("claude", payload, account)
                self.assertEqual(
                    fetch_quota._read_provider_cache("claude", account), payload
                )

    def test_vscdb_cell_that_is_not_utf8_is_dropped(self) -> None:
        self.assertIsNone(fetch_quota._vscdb_str(b"\xff\xfe not utf-8"))
        self.assertEqual(fetch_quota._vscdb_str('"Ünïcodé"'), "Ünïcodé")


class HttpRetryableTest(unittest.TestCase):
    def test_retryable_status_codes(self) -> None:
        self.assertTrue(fetch_quota._http_retryable(429))
        self.assertTrue(fetch_quota._http_retryable(503))
        self.assertTrue(fetch_quota._http_retryable(500))
        self.assertFalse(fetch_quota._http_retryable(401))
        self.assertFalse(fetch_quota._http_retryable(0))


class TransportFailureTest(unittest.TestCase):
    """A dropped connection must stay diagnosable, and a read is retried once
    while a token POST is not."""

    def setUp(self) -> None:
        self.sleeps: list[float] = []
        patcher = patch.object(fetch_quota, "sleep", self.sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _urlopen_raising(self, exc: Exception, calls: list[int]) -> object:
        def fake_urlopen(req: object, timeout: float = 0.0) -> object:
            calls.append(1)
            raise exc

        return fake_urlopen

    def test_get_is_retried_once_then_reports_the_cause(self) -> None:
        calls: list[int] = []
        err = io.StringIO()
        with (
            patch.object(
                urllib.request,
                "urlopen",
                self._urlopen_raising(OSError("no route"), calls),
            ),
            contextlib.redirect_stderr(err),
        ):
            status, body, hdrs = fetch_quota.fetch_http(
                "https://api.test/usage", {"Authorization": "Bearer secret"}
            )
        self.assertEqual((status, body, hdrs), (0, None, None))
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.sleeps, [fetch_quota.NETWORK_RETRY_BACKOFF_S])
        logged = err.getvalue()
        self.assertIn("https://api.test/usage", logged)
        self.assertIn("no route", logged)
        self.assertNotIn("secret", logged)

    def test_second_attempt_can_succeed(self) -> None:
        calls: list[int] = []

        class _Resp:
            status = 200
            headers: object = None

            def read(self) -> bytes:
                return b'{"ok": true}'

            def __enter__(self) -> _Resp:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        def fake_urlopen(req: object, timeout: float = 0.0) -> object:
            calls.append(1)
            if len(calls) == 1:
                raise TimeoutError("timed out")
            return _Resp()

        with (
            patch.object(urllib.request, "urlopen", fake_urlopen),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            status, body, _ = fetch_quota.fetch_http("https://api.test/usage", {})
        self.assertEqual((status, body), (200, {"ok": True}))

    def test_token_post_is_never_retried(self) -> None:
        calls: list[int] = []
        with (
            patch.object(
                urllib.request,
                "urlopen",
                self._urlopen_raising(TimeoutError("timed out"), calls),
            ),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            status, _body, _ = fetch_quota.fetch_http(
                "https://auth.test/token", {}, data=b"grant=x", method="POST"
            )
        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.sleeps, [])


class ProviderCrashTest(unittest.TestCase):
    def test_crash_is_reported_on_stderr_and_stays_transient(self) -> None:
        def boom() -> JsonDict:
            raise RuntimeError("meters exploded")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = fetch_quota._safe_fetch("claude", boom)
        self.assertEqual(out, {"ok": False, "error": "net"})
        logged = err.getvalue()
        self.assertIn("claude", logged)
        self.assertIn("meters exploded", logged)
        self.assertIn("Traceback", logged)


class CodexExpiryClockTest(unittest.TestCase):
    def test_expiry_reads_the_pinned_clock(self) -> None:
        now = 1_777_000_000_000
        with config_env(**{fetch_quota.NOW_MS_ENV: str(now)}):
            exp_s = (now + fetch_quota.TOKEN_SKEW_S * 1000) // 1000
            fresh = _jwt_with_exp(exp_s + 60)
            self.assertFalse(fetch_quota._codex_token_expired({"access_token": fresh}))
            stale = _jwt_with_exp(exp_s - 60)
            self.assertTrue(fetch_quota._codex_token_expired({"access_token": stale}))


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

        auth = self.root / "auth"
        auth.mkdir(exist_ok=True)
        point_config(
            self,
            "claude_cred",
            self._write(
                auth / "claude.json", {"claudeAiOauth": {"accessToken": _fake_jwt("s")}}
            ),
        )
        point_config(
            self,
            "codex_auth",
            self._write(
                auth / "codex.json", {"tokens": {"access_token": _fake_jwt("s")}}
            ),
        )
        point_config(
            self,
            "grok_auth",
            self._write(
                auth / "grok.json",
                {"x": {"key": "tok", "oidc_client_id": "c", "refresh_token": "r"}},
            ),
        )
        point_config(
            self,
            "cursor_auth",
            self._write(
                auth / "cursor.json", {"accessToken": _fake_jwt("auth0|user_01TEST")}
            ),
        )

    def _write(self, path: Path, obj: object) -> Path:
        path.write_text(json.dumps(obj))
        return path

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
            # argv=None would read pytest's own flags off sys.argv.
            fetch_quota.main([])
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


def _jwt_with_exp(exp_s: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": exp_s}).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{payload}.sig"


class RefreshRunsOnceTest(unittest.TestCase):
    """A second poll must not rotate an already-rotated refresh token.

    Providers invalidate the refresh token they hand out, so a duplicate
    refresh leaves the credential file holding a token that can never work.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["QUOTA_WIDGET_CACHE"] = self.tmp.name
        self.addCleanup(lambda: os.environ.pop("QUOTA_WIDGET_CACHE", None))

    def _point(self, name: str, path: Path) -> None:
        env = credential_env(name, path)
        env.__enter__()
        self.addCleanup(env.__exit__, None, None, None)

    def test_claude_second_poll_reuses_rotated_token(self) -> None:
        cred = Path(self.tmp.name) / "cred.json"
        cred.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "old-access",
                        "refreshToken": "old-refresh",
                        "expiresAt": 1,
                        "subscriptionType": "pro",
                    }
                }
            )
        )
        point_config(self, "claude_cred", cred)
        posts: list[str] = []

        def fake_json(
            url: str,
            headers: dict[str, str],
            *,
            timeout: float = 12.0,
            data: bytes | None = None,
            method: str | None = None,
        ) -> tuple[int, object]:
            self.assertIn("/oauth/token", url)
            body = json.loads((data or b"").decode())
            posts.append(str(body["refresh_token"]))
            if body["refresh_token"] != "old-refresh":
                return 400, {"error": "invalid_grant"}
            return 200, {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 28800,
            }

        def fake_http(
            url: str,
            headers: dict[str, str],
            *,
            timeout: float = 12.0,
            data: bytes | None = None,
            method: str | None = None,
        ) -> tuple[int, object, object]:
            return 200, {"five_hour": {"utilization": 4}}, None

        with (
            patch.object(fetch_quota, "fetch_json", fake_json),
            patch.object(fetch_quota, "fetch_http", fake_http),
        ):
            first = fetch_quota.fetch_claude()
            second = fetch_quota.fetch_claude()

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(posts, ["old-refresh"])
        saved = json.loads(cred.read_text())["claudeAiOauth"]
        self.assertEqual(saved["accessToken"], "new-access")
        self.assertEqual(saved["refreshToken"], "new-refresh")

    def test_claude_concurrent_polls_rotate_once(self) -> None:
        cred = Path(self.tmp.name) / "cred.json"
        cred.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "old-access",
                        "refreshToken": "old-refresh",
                        "expiresAt": 1,
                    }
                }
            )
        )
        point_config(self, "claude_cred", cred)
        posts: list[str] = []
        second_started = threading.Event()
        first_in_token_call = threading.Event()

        def fake_json(
            url: str,
            headers: dict[str, str],
            *,
            timeout: float = 12.0,
            data: bytes | None = None,
            method: str | None = None,
        ) -> tuple[int, object]:
            body = json.loads((data or b"").decode())
            posts.append(str(body["refresh_token"]))
            first_in_token_call.set()
            second_started.wait(5)
            if body["refresh_token"] != "old-refresh":
                return 400, {"error": "invalid_grant"}
            return 200, {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 28800,
            }

        def fake_http(
            url: str,
            headers: dict[str, str],
            *,
            timeout: float = 12.0,
            data: bytes | None = None,
            method: str | None = None,
        ) -> tuple[int, object, object]:
            return 200, {"five_hour": {"utilization": 4}}, None

        results: list[JsonDict] = []

        def run() -> None:
            results.append(fetch_quota.fetch_claude())

        with (
            patch.object(fetch_quota, "fetch_json", fake_json),
            patch.object(fetch_quota, "fetch_http", fake_http),
        ):
            first = threading.Thread(target=run)
            first.start()
            first_in_token_call.wait(5)
            second = threading.Thread(target=run)
            second.start()
            second_started.set()
            first.join(10)
            second.join(10)

        self.assertEqual(posts, ["old-refresh"])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["ok"] for r in results))
        saved = json.loads(cred.read_text())["claudeAiOauth"]
        self.assertEqual(saved["refreshToken"], "new-refresh")

    def test_codex_second_poll_reuses_rotated_token(self) -> None:
        auth = Path(self.tmp.name) / "codex-auth.json"
        auth.write_text(
            json.dumps(
                {
                    "tokens": {
                        "access_token": _jwt_with_exp(0),
                        "refresh_token": "old-refresh",
                        "account_id": "acct-1",
                    }
                }
            )
        )
        point_config(self, "codex_auth", auth)
        posts: list[str] = []
        fresh = _jwt_with_exp(int(time.time()) + 3600)

        def fake_json(
            url: str,
            headers: dict[str, str],
            *,
            timeout: float = 12.0,
            data: bytes | None = None,
            method: str | None = None,
        ) -> tuple[int, object]:
            if url == fetch_quota.CODEX_TOKEN_URL:
                body = urllib.parse.parse_qs((data or b"").decode())
                posts.append(body["refresh_token"][0])
                if body["refresh_token"][0] != "old-refresh":
                    return 400, {"error": "invalid_grant"}
                return 200, {
                    "access_token": fresh,
                    "refresh_token": "new-refresh",
                }
            return 200, {
                "plan_type": "plus",
                "rate_limit": {"allowed": True, "primary_window": {}},
            }

        with patch.object(fetch_quota, "fetch_json", fake_json):
            fetch_quota.fetch_codex()
            fetch_quota.fetch_codex()

        self.assertEqual(posts, ["old-refresh"])
        saved = json.loads(auth.read_text())["tokens"]
        self.assertEqual(saved["access_token"], fresh)
        self.assertEqual(saved["refresh_token"], "new-refresh")

    def test_grok_second_poll_reuses_rotated_token(self) -> None:
        past = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
        auth = Path(self.tmp.name) / "grok-auth.json"
        auth.write_text(
            json.dumps(
                {
                    "cli::client-1": {
                        "key": "old-access",
                        "refresh_token": "old-refresh",
                        "oidc_client_id": "client-1",
                        "expires_at": past.isoformat().replace("+00:00", "Z"),
                    }
                }
            )
        )
        point_config(self, "grok_auth", auth)
        posts: list[str] = []
        billing = {
            "config": {
                "isUnifiedBillingUser": True,
                "currentPeriod": {
                    "type": "WEEKLY",
                    "start": "2026-01-01T00:00:00Z",
                    "end": "2026-01-08T00:00:00Z",
                },
            }
        }

        def fake_json(
            url: str,
            headers: dict[str, str],
            *,
            timeout: float = 12.0,
            data: bytes | None = None,
            method: str | None = None,
        ) -> tuple[int, object]:
            if url == fetch_quota.GROK_OIDC_DISCOVERY:
                return 200, {"token_endpoint": "https://auth.test/token"}
            if url == "https://auth.test/token":
                body = urllib.parse.parse_qs((data or b"").decode())
                posts.append(body["refresh_token"][0])
                if body["refresh_token"][0] != "old-refresh":
                    return 400, {"error": "invalid_grant"}
                return 200, {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                }
            self.assertTrue(url.startswith(fetch_quota.GROK_BILLING_URL))
            return 200, billing

        with patch.object(fetch_quota, "fetch_json", fake_json):
            fetch_quota.fetch_grok()
            fetch_quota.fetch_grok()

        self.assertEqual(posts, ["old-refresh"])
        saved = json.loads(auth.read_text())["cli::client-1"]
        self.assertEqual(saved["key"], "new-access")
        self.assertEqual(saved["refresh_token"], "new-refresh")

    def test_refresh_still_runs_where_flock_is_absent(self) -> None:
        auth = Path(self.tmp.name) / "codex-auth.json"
        auth.write_text(
            json.dumps(
                {
                    "tokens": {
                        "access_token": _jwt_with_exp(0),
                        "refresh_token": "old-refresh",
                        "account_id": "acct-1",
                    }
                }
            )
        )
        point_config(self, "codex_auth", auth)
        fresh = _jwt_with_exp(int(time.time()) + 3600)

        def fake_json(
            url: str,
            headers: dict[str, str],
            *,
            timeout: float = 12.0,
            data: bytes | None = None,
            method: str | None = None,
        ) -> tuple[int, object]:
            if url == fetch_quota.CODEX_TOKEN_URL:
                return 200, {"access_token": fresh, "refresh_token": "new-refresh"}
            return 200, {
                "plan_type": "plus",
                "rate_limit": {"allowed": True, "primary_window": {}},
            }

        with (
            patch.object(fetch_quota, "fcntl", None),
            patch.object(fetch_quota, "fetch_json", fake_json),
        ):
            result = fetch_quota.fetch_codex()

        self.assertTrue(result["ok"])
        self.assertEqual(
            json.loads(auth.read_text())["tokens"]["refresh_token"], "new-refresh"
        )


if __name__ == "__main__":
    unittest.main()
