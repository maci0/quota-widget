from __future__ import annotations

import ast
import base64
import contextlib
import dataclasses
import datetime as dt
import email.message
import errno
import hashlib
import http.client
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unicodedata
import unittest
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any
from unittest.mock import patch

import fetch_quota

# Fixed clock for every test that cares about expiry or replay.
PINNED_NOW_MS = 1_777_000_000_000

_RATE_LIMIT: dict[str, object] = {"error": {"type": "rate_limit_error"}}
_NEW_TOKENS: dict[str, object] = {
    "access_token": "new-access",
    "refresh_token": "new-refresh",
    "expires_in": 28800,
}

JsonDict = dict[str, Any]

# The Cursor state.vscdb tables a fixture may build: the one the reader looks
# in, and any other name for the test that proves a missing table reads empty.
_FIXTURE_TABLES = ("ItemTable", "Other")

# The sandbox home and cache belong to the suite, not to this module;
# tests/conftest.py installs them before any test module is imported and drops
# them at session end, so nothing here has to restore the real environment.

PathSetter = Callable[[fetch_quota.Config, Path], fetch_quota.Config]

# Each config path field, paired with the call that rebuilds the config with
# that one field repointed. Keeps point_config free of a field-by-field rebuild.
_CONFIG_PATH_SETTERS: dict[str, PathSetter] = {
    "home": lambda cfg, path: dataclasses.replace(cfg, home=path),
    "claude_cred": lambda cfg, path: dataclasses.replace(cfg, claude_cred=path),
    "codex_auth": lambda cfg, path: dataclasses.replace(cfg, codex_auth=path),
    "grok_auth": lambda cfg, path: dataclasses.replace(cfg, grok_auth=path),
    "cursor_auth": lambda cfg, path: dataclasses.replace(cfg, cursor_auth=path),
    "cursor_state_db": lambda cfg, path: dataclasses.replace(cfg, cursor_state_db=path),
    "cache_dir": lambda cfg, path: dataclasses.replace(cfg, cache_dir=path),
}


def point_config(case: unittest.TestCase, field: str, path: Path) -> None:
    """Point one path of the active config elsewhere, then restore the config."""
    fetch_quota._CONFIG = _CONFIG_PATH_SETTERS[field](fetch_quota.config(), path)
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


class CodexCodeReviewWindowTest(unittest.TestCase):
    """The code review meter arrives in two shapes; both render a bar."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.auth = Path(self.tmp.name) / "codex.json"
        self.auth.write_text(
            json.dumps({"tokens": {"access_token": _jwt_with_exp(4102444800)}})
        )
        env = config_env(
            QUOTA_WIDGET_CACHE=self.tmp.name, QUOTA_WIDGET_CODEX_AUTH=str(self.auth)
        )
        env.__enter__()
        self.addCleanup(env.__exit__, None, None, None)

    def _fetch(self, body: JsonDict) -> JsonDict:
        fake = _http_returning_pair(200, body)
        with patch.object(fetch_quota, "fetch_json", fake):
            out = fetch_quota.fetch_codex()
        fake.hit(self, fetch_quota.CODEX_USAGE_URL)
        return out

    def test_nested_windows_are_prefixed(self) -> None:
        out = self._fetch(
            {
                "plan_type": "pro",
                "code_review_rate_limit": {
                    "primary_window": {
                        "used_percent": 20,
                        "limit_window_seconds": 604800,
                    },
                    "secondary_window": {
                        "used_percent": 5,
                        "limit_window_seconds": 18000,
                    },
                },
            }
        )
        self.assertTrue(out["ok"])
        self.assertEqual(
            [w["label"] for w in out["windows"]],
            ["Code review · Weekly", "Code review · Current session"],
        )
        self.assertEqual([w["util"] for w in out["windows"]], [20.0, 5.0])

    def test_flat_window_gets_its_own_label(self) -> None:
        out = self._fetch(
            {
                "plan_type": "pro",
                "code_review_rate_limit": {
                    "used_percent": 42,
                    "limit_window_seconds": 604800,
                },
            }
        )
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["windows"]), 1)
        self.assertEqual(out["windows"][0]["label"], "Code review")
        self.assertEqual(out["windows"][0]["util"], 42.0)

    def test_rate_windows_come_first_and_both_meters_survive(self) -> None:
        out = self._fetch(
            {
                "plan_type": "pro",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 10,
                        "limit_window_seconds": 259200,
                    }
                },
                "code_review_rate_limit": {"used_percent": 42},
            }
        )
        self.assertTrue(out["ok"])
        self.assertEqual(
            [w["label"] for w in out["windows"]],
            ["3-day", "Code review"],
        )

    def test_a_malformed_review_meter_is_dropped_not_shown_as_zero(self) -> None:
        # NaN is not 0% and not 100%; dropping the bar leaves the card honest.
        out = self._fetch(
            {
                "plan_type": "pro",
                "code_review_rate_limit": {"used_percent": float("nan")},
            }
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["windows"], [])


class RetryAfterTest(unittest.TestCase):
    def test_parses_delta_seconds(self) -> None:
        self.assertEqual(fetch_quota.parse_retry_after("2"), 2.0)
        self.assertEqual(fetch_quota.parse_retry_after("0"), 0.0)
        self.assertIsNone(fetch_quota.parse_retry_after(None))
        self.assertIsNone(fetch_quota.parse_retry_after("nope"))

    def test_infinite_or_nan_delta_is_no_wait(self) -> None:
        # float() reads both words, and a wait on an infinity never ends
        # (time.sleep raises), while a NaN compares false against every
        # bound and would read as a zero-second wait.
        for raw in ("inf", "-inf", "Infinity", "nan", "NaN", "1e400"):
            with self.subTest(header=raw):
                self.assertIsNone(fetch_quota.parse_retry_after(raw))

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
        # config_env reloads the config; setting the variable alone leaves
        # config().cache_dir pointing at the module sandbox, and the entry this
        # test writes then never lands where the deletion check looks for it.
        env = config_env(QUOTA_WIDGET_CACHE=tmp.name)
        env.__enter__()
        self.addCleanup(env.__exit__, None, None, None)
        entry = Path(tmp.name) / "grok.json"
        account = fetch_quota._account_id(_fake_jwt("user_01GROK"))
        fetch_quota._write_provider_cache("grok", {"ok": True, "plan": "Grok"}, account)
        self.assertTrue(entry.exists())

        os.environ[fetch_quota.NOW_MS_ENV] = str(
            PINNED_NOW_MS + fetch_quota.DEFAULT_CACHE_MAX_AGE_S * 1000
        )
        self.assertIsNotNone(fetch_quota._read_provider_cache("grok", account))
        self.assertTrue(entry.exists())
        os.environ[fetch_quota.NOW_MS_ENV] = str(
            PINNED_NOW_MS + (fetch_quota.DEFAULT_CACHE_MAX_AGE_S + 1) * 1000
        )
        self.assertIsNone(fetch_quota._read_provider_cache("grok", account))
        self.assertFalse(entry.exists())

    def test_a_clock_at_the_calendar_ceiling_stays_inside_it(self) -> None:
        # The ceiling the validator accepts is the last instant now_utc() can
        # represent, and every "now + lifetime" sum runs off the end there.
        # The sums saturate: an OverflowError on a refresh path raises after the
        # token POST retired the old refresh token, so the rotated credential is
        # never written back and the user is signed out of the vendor CLI.
        os.environ[fetch_quota.NOW_MS_ENV] = str(fetch_quota.MAX_PINNED_MS)
        # The pin itself is representable: it is the sums past it that are not.
        self.assertEqual(fetch_quota.now_utc().year, 9999)
        self.assertEqual(fetch_quota._shifted(3600), fetch_quota.LAST_UTC)
        # A lifetime past what timedelta holds raises on the delta itself,
        # before the sum is ever taken.
        self.assertEqual(fetch_quota._shifted(1e18), fetch_quota.LAST_UTC)
        self.assertTrue(
            fetch_quota._token_expired({"expires_at": "2030-01-01T00:00:00Z"})
        )
        # A lifetime the calendar can hold still lands on the real sum, so the
        # saturation is not a clamp on ordinary values.
        os.environ[fetch_quota.NOW_MS_ENV] = str(PINNED_NOW_MS)
        self.assertEqual(
            fetch_quota._shifted(3600),
            fetch_quota.now_utc() + dt.timedelta(seconds=3600),
        )

    def test_the_pin_reports_the_range_it_accepts(self) -> None:
        # The old message printed a range whose lower bound the next check
        # rejected, so the value it named as acceptable was the one thing the
        # operator must not pass.
        os.environ[fetch_quota.NOW_MS_ENV] = str(fetch_quota.MAX_PINNED_MS + 1)
        with self.assertRaises(fetch_quota.ConfigError) as ctx:
            fetch_quota.load_config(
                {fetch_quota.NOW_MS_ENV: os.environ[fetch_quota.NOW_MS_ENV]}
            )
        self.assertIn(str(fetch_quota.MAX_PINNED_MS), str(ctx.exception))
        self.assertNotIn("-62135596800000", str(ctx.exception))


class TimeSeamTest(unittest.TestCase):
    """A wait never reaches the real clock on its own.

    Every deadline and every pause in the fetcher has to arrive through
    monotonic() and sleep(), or a contended poll cannot be replayed and the
    suite pays for it in wall time. The two seam bodies are the only places
    allowed to name time.sleep or time.monotonic.
    """

    SEAM_BODIES = frozenset({"sleep", "monotonic"})

    def test_no_call_site_bypasses_the_elapsed_time_seams(self) -> None:
        tree = ast.parse(Path(fetch_quota.__file__).read_text(encoding="utf-8"))
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        self.assertEqual(
            {fn.name for fn in functions} & self.SEAM_BODIES, self.SEAM_BODIES
        )

        bypasses: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or node.attr not in self.SEAM_BODIES:
                continue
            value = node.value
            if not (
                isinstance(value, ast.Name)
                and value.id == "time"
                and isinstance(node.ctx, ast.Load)
            ):
                continue
            enclosing = next(
                (fn.name for fn in functions if node in ast.walk(fn)),
                None,
            )
            if enclosing not in self.SEAM_BODIES:
                bypasses.append(node.lineno)

        self.assertEqual(bypasses, [])


def _fake_jwt(sub: str) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{payload}.sig"


def _scoped(payload: JsonDict) -> JsonDict:
    """A provider payload minus the account digest it is scoped to.

    Success and failure payloads both name the account they were made for, so
    the panel can tell one account's blip from another account's sign-in.
    """
    return {k: v for k, v in payload.items() if k != "account"}


class _RecordingHttp:
    """Serves one canned response to every request and records the URLs it was
    asked for. A fake that answers any URL cannot tell a test that the fetcher
    reached the vendor's usage endpoint from one where it reached something
    else, so the calls land in `urls` and a test checks the endpoint."""

    def __init__(self, status: int, body: object, hdrs: object = None) -> None:
        self.status = status
        self.body = body
        self.hdrs = hdrs
        self.urls: list[str] = []

    def hit(self, case: unittest.TestCase, url: str) -> None:
        case.assertIn(url, self.urls, f"never called {url}, only {self.urls}")


class FakeHttp(_RecordingHttp):
    def __call__(
        self,
        url: str,
        headers: dict[str, str],
        *,
        timeout: float = 12.0,
        data: bytes | None = None,
        method: str | None = None,
    ) -> tuple[int, object, object]:
        self.urls.append(url)
        return self.status, self.body, self.hdrs


def _http_returning(status: int, body: object, hdrs: object = None) -> FakeHttp:
    return FakeHttp(status, body, hdrs)


class FakeJson(_RecordingHttp):
    def __call__(
        self,
        url: str,
        headers: dict[str, str],
        *,
        timeout: float = 12.0,
        data: bytes | None = None,
        method: str | None = None,
    ) -> tuple[int, object]:
        self.urls.append(url)
        return self.status, self.body


def _http_returning_pair(status: int, body: object) -> FakeJson:
    return FakeJson(status, body)


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

        fake = _http_returning(429, _RATE_LIMIT, {"Retry-After": "0"})
        with patch.object(fetch_quota, "fetch_http", fake):
            out = fetch_quota.fetch_claude()
        fake.hit(self, fetch_quota.CLAUDE_URL)
        self.assertTrue(out["ok"])
        self.assertTrue(out["stale"])
        self.assertEqual(out["session"]["util"], 12)

    def test_offline_serves_the_cached_payload(self) -> None:
        # A machine that loses the network between two polls has no panel copy
        # to fall back on when plasmashell just started.
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

        with patch.object(fetch_quota, "fetch_http", _http_returning(0, None)):
            out = fetch_quota.fetch_claude()
        self.assertTrue(out["ok"])
        self.assertTrue(out["stale"])
        self.assertEqual(out["session"]["util"], 12)

    def test_offline_without_a_cache_entry_still_reports_net(self) -> None:
        with patch.object(fetch_quota, "fetch_http", _http_returning(0, None)):
            out = fetch_quota.fetch_claude()
        self.assertEqual(_scoped(out), {"ok": False, "error": "net", "transient": True})

    def test_a_failure_names_the_account_it_was_made_for(self) -> None:
        # The panel keeps a reading through a blip, so it has to know whose
        # reading it is holding when the poll comes back failed.
        account = fetch_quota._account_id(self.access_token)
        with patch.object(
            fetch_quota,
            "fetch_http",
            _http_returning(200, {"five_hour": {"utilization": 4}}, None),
        ):
            ok = fetch_quota.fetch_claude()
        self.assertEqual(ok["account"], account)
        with patch.object(fetch_quota, "fetch_http", _http_returning(0, None)):
            failed = fetch_quota.fetch_claude()
        self.assertEqual(failed["account"], account)

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

        fake = _http_returning(429, _RATE_LIMIT, {"Retry-After": "0"})
        with patch.object(fetch_quota, "fetch_http", fake):
            out = fetch_quota.fetch_claude()
        fake.hit(self, fetch_quota.CLAUDE_URL)
        self.assertEqual(
            _scoped(out), {"ok": False, "error": "http-429", "transient": True}
        )

    def test_credential_without_account_id_writes_no_cache(self) -> None:
        cred_path = fetch_quota.config().claude_cred
        payload = json.loads(cred_path.read_text())
        payload["claudeAiOauth"]["accessToken"] = "opaque-token"
        cred_path.write_text(json.dumps(payload))

        fake = _http_returning(200, {"five_hour": {"utilization": 5}})
        with patch.object(fetch_quota, "fetch_http", fake):
            out = fetch_quota.fetch_claude()
        fake.hit(self, fetch_quota.CLAUDE_URL)
        self.assertTrue(out["ok"])
        self.assertFalse((Path(self.tmp.name) / "claude.json").exists())

    def test_429_without_cache_is_error(self) -> None:
        fake = _http_returning(429, _RATE_LIMIT, {"Retry-After": "0"})
        with patch.object(fetch_quota, "fetch_http", fake):
            out = fetch_quota.fetch_claude()
        fake.hit(self, fetch_quota.CLAUDE_URL)
        self.assertEqual(
            _scoped(out), {"ok": False, "error": "http-429", "transient": True}
        )

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

        usage = _http_returning(401, {"error": {"type": "authentication_error"}})
        with (
            patch.object(
                fetch_quota, "fetch_json", _http_returning_pair(429, _RATE_LIMIT)
            ),
            patch.object(fetch_quota, "fetch_http", usage),
        ):
            out = fetch_quota.fetch_claude()
        usage.hit(self, fetch_quota.CLAUDE_URL)
        self.assertEqual(
            _scoped(out), {"ok": False, "error": "http-429", "transient": True}
        )

    def test_rejected_refresh_after_401_reports_signed_out(self) -> None:
        # The refresh token itself is what was revoked: 401 from the token
        # endpoint, so the card must ask the user to log in again rather than
        # claim a rate limit that will never clear.
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
            return 400, {"error": "invalid_grant"}

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
        self.assertEqual(
            _scoped(out), {"ok": False, "error": "http-401", "transient": False}
        )

    def test_signed_out_with_a_cache_entry_still_reports_401(self) -> None:
        # The cached reading is scoped by the token's `sub`, which a revoked
        # session still carries, so a 401 that follows a successful refresh is
        # that same account's own last good reading. Serving it would keep a
        # revoked session looking healthy for the whole retention window and
        # the card would never ask the user to log in again.
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

        usage = _http_returning(401, {"error": {"type": "authentication_error"}})
        with (
            patch.object(fetch_quota, "fetch_json", fake_json),
            patch.object(fetch_quota, "fetch_http", usage),
        ):
            out = fetch_quota.fetch_claude()
        usage.hit(self, fetch_quota.CLAUDE_URL)
        # One call: the refresh already handed back a fresh token, and the 401
        # is that new token being rejected. A second rotation on it would only
        # burn the refresh token the user still holds.
        self.assertEqual(len(usage.urls), 1)
        self.assertEqual(
            _scoped(out), {"ok": False, "error": "http-401", "transient": False}
        )

    def test_throttled_refresh_with_a_cache_entry_serves_it_stale(self) -> None:
        # The 401 is the expired access token, not a revoked session: the
        # provider throttled the refresh, so the cached reading stands in and
        # the card is told the rate limit, not a sign-out.
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
        cred_path = fetch_quota.config().claude_cred
        payload = json.loads(cred_path.read_text())
        payload["claudeAiOauth"]["refreshToken"] = "old-refresh"
        payload["claudeAiOauth"]["expiresAt"] = 1
        cred_path.write_text(json.dumps(payload))

        usage = _http_returning(401, {"error": {"type": "authentication_error"}})
        with (
            patch.object(
                fetch_quota, "fetch_json", _http_returning_pair(429, _RATE_LIMIT)
            ),
            patch.object(fetch_quota, "fetch_http", usage),
        ):
            out = fetch_quota.fetch_claude()
        usage.hit(self, fetch_quota.CLAUDE_URL)
        self.assertTrue(out["ok"])
        self.assertTrue(out["stale"])
        self.assertEqual(out["session"]["util"], 12)

    def test_spend_used_as_number_does_not_crash(self) -> None:
        body = {"five_hour": {"utilization": 4}, "spend": {"used": 12}}
        fake = _http_returning(200, body)
        with patch.object(fetch_quota, "fetch_http", fake):
            out = fetch_quota.fetch_claude()
        fake.hit(self, fetch_quota.CLAUDE_URL)
        self.assertTrue(out["ok"])
        self.assertIsNone(out["spend"]["used_minor"])

    def _spend_exponent(self, exponent: object) -> float:
        body = {
            "five_hour": {"utilization": 4},
            "spend": {"used": {"amount_minor": 6763, "exponent": exponent}},
        }
        fake = _http_returning(200, body)
        with patch.object(fetch_quota, "fetch_http", fake):
            out = fetch_quota.fetch_claude()
        fake.hit(self, fetch_quota.CLAUDE_URL)
        value = out["spend"]["exponent"]
        assert isinstance(value, float)
        return value

    def test_spend_exponent_outside_the_amount_range_is_cents(self) -> None:
        # The panel scales the minor amount by 10^exponent, so a wire value
        # past the decimal places an amount is counted in renders a real
        # charge as 0.00 (Math.pow(10, 1e308) is Infinity) or inflates it by
        # 10^5 for a negative one.
        for raw in (400, 1e308, -3, 2.5, "two", None, float("nan")):
            with self.subTest(exponent=raw):
                self.assertEqual(self._spend_exponent(raw), 2.0)

    def test_spend_exponent_in_range_is_kept(self) -> None:
        for raw, expected in ((0, 0.0), (2, 2.0), (4, 4.0), (6, 6.0)):
            with self.subTest(exponent=raw):
                self.assertEqual(self._spend_exponent(raw), expected)


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


class ClaudeLimitsArrayTest(unittest.TestCase):
    """The structured `limits` list is what the API returns today. Session
    entries must not become weekly bars, and a scoped entry is labelled by its
    scope rather than by its kind."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cred = Path(self.tmp.name) / "cred.json"
        cred.write_text(
            json.dumps({"claudeAiOauth": {"accessToken": _fake_jwt("user_01LIM")}})
        )
        env = config_env(
            QUOTA_WIDGET_CACHE=self.tmp.name,
            QUOTA_WIDGET_CLAUDE_CREDENTIALS=str(cred),
        )
        env.__enter__()
        self.addCleanup(env.__exit__, None, None, None)

    def _fetch(self, body: JsonDict) -> JsonDict:
        fake = _http_returning(200, body)
        with patch.object(fetch_quota, "fetch_http", fake):
            out = fetch_quota.fetch_claude()
        fake.hit(self, fetch_quota.CLAUDE_URL)
        return out

    def test_weekly_and_session_meters_come_from_one_list(self) -> None:
        out = self._fetch(
            {
                "limits": [
                    {
                        "kind": "weekly_all",
                        "percent": 12,
                        "resets_at": "2026-05-02T14:11:55Z",
                    },
                    {
                        "kind": "weekly_opus",
                        "percent": 34,
                        "scope": {"model": {"display_name": "Opus"}},
                    },
                    {
                        "kind": "weekly_cowork",
                        "percent": 56,
                        "scope": {"surface": "Cowork"},
                    },
                    {
                        "kind": "session",
                        "percent": 78,
                        "resets_at": "2026-05-02T15:00:00Z",
                    },
                ],
                "five_hour": {"utilization": 11, "resets_at": "2026-05-02T12:00:00Z"},
            }
        )
        self.assertTrue(out["ok"])
        self.assertEqual(
            [w["label"] for w in out["weekly"]], ["All models", "Opus", "Cowork"]
        )
        self.assertEqual([w["util"] for w in out["weekly"]], [12.0, 34.0, 56.0])
        self.assertEqual(out["session"]["util"], 78.0)
        self.assertEqual(
            out["session"]["resets_ms"],
            fetch_quota.iso_to_ms("2026-05-02T15:00:00Z"),
        )

    def test_group_marks_an_entry_as_a_session(self) -> None:
        # The key is "group" here, not "kind"; treating it as weekly would add a
        # bar that resets in five hours.
        out = self._fetch(
            {"limits": [{"group": "session", "percent": 5}]},
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["weekly"], [])
        self.assertEqual(out["session"]["util"], 5.0)

    def test_an_entry_that_is_not_an_object_is_skipped(self) -> None:
        out = self._fetch(
            {
                "limits": [
                    "not an object",
                    {"kind": "weekly_all", "percent": 7},
                ]
            }
        )
        self.assertTrue(out["ok"])
        self.assertEqual([w["util"] for w in out["weekly"]], [7.0])

    def test_an_unreadable_percent_leaves_no_reading_not_a_zero(self) -> None:
        # The bar is real, the number is not: util stays absent so the panel
        # shows nothing rather than a healthy 0%.
        out = self._fetch({"limits": [{"kind": "weekly_all", "percent": "n/a"}]})
        self.assertTrue(out["ok"])
        self.assertEqual([w["util"] for w in out["weekly"]], [None])

    def test_legacy_keys_are_used_when_there_is_no_list(self) -> None:
        out = self._fetch(
            {
                "five_hour": {"utilization": 3},
                "seven_day": {"utilization": 40, "resets_at": None},
                "seven_day_opus": {"utilization": 60, "resets_at": None},
            }
        )
        self.assertTrue(out["ok"])
        self.assertEqual([w["label"] for w in out["weekly"]], ["All models", "Opus"])
        self.assertEqual([w["util"] for w in out["weekly"]], [40.0, 60.0])
        self.assertEqual(out["session"]["util"], 3.0)

    def test_the_list_wins_over_the_legacy_keys(self) -> None:
        # The two shapes never describe the same window, so mixing them would
        # show a bar the provider is no longer reporting.
        out = self._fetch(
            {
                "limits": [{"kind": "weekly_all", "percent": 12}],
                "seven_day": {"utilization": 40},
                "seven_day_opus": {"utilization": 60},
            }
        )
        self.assertTrue(out["ok"])
        self.assertEqual([w["util"] for w in out["weekly"]], [12.0])

    def test_a_long_list_is_capped_at_what_the_panel_can_show(self) -> None:
        # The array is vendor data, so its length is not ours to trust: every
        # entry is a meter the panel keeps until the next poll.
        out = self._fetch(
            {"limits": [{"kind": f"weekly_{i}", "percent": i} for i in range(500)]}
        )
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["weekly"]), fetch_quota.MAX_WEEKLY_LIMITS)


class CursorStateDbTest(unittest.TestCase):
    """The IDE credential path. A user without cursor-agent's auth.json has a
    session in state.vscdb and nothing else, so this is the only source they
    have."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "state.vscdb"

    def _write_db(self, rows: dict[str, object], table: str = "ItemTable") -> None:
        # A table name cannot be a bound parameter, so the two names these
        # tests build are named here and the statement interpolates nothing
        # else.
        if table not in _FIXTURE_TABLES:
            raise ValueError(f"unknown fixture table: {table!r}")
        # closing, not a bare close() at the end: a statement that raises would
        # otherwise leave the connection holding a descriptor open.
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            # A throwaway fixture: the durability the real vscdb needs would
            # cost an fsync per test and buys this suite nothing. SQLite takes
            # no parameter for a table name, and the one here comes from a
            # call-site literal in this module, never from input.
            con.execute("PRAGMA journal_mode = MEMORY")
            con.execute("PRAGMA synchronous = OFF")
            # An identifier cannot be a bound parameter, so the table name is
            # the one part that has to be interpolated. It is a test literal
            # ("ItemTable", "Other"), never input.
            con.execute(f"CREATE TABLE {table} (key TEXT, value BLOB)")
            # The table name is a literal the test itself passes; the values are
            # bound, so there is no injection surface here.
            con.executemany(
                f"INSERT INTO {table} (key, value) VALUES (?, ?)",  # noqa: S608 (checked table)
                list(rows.items()),
            )

    def test_reads_token_and_membership_from_the_db(self) -> None:
        self._write_db(
            {
                "cursorAuth/accessToken": _fake_jwt("auth0|user_01IDB"),
                "cursorAuth/stripeMembershipType": "ultra",
            }
        )
        self.assertEqual(
            fetch_quota._read_cursor_state_db(self.db),
            (_fake_jwt("auth0|user_01IDB"), "ultra"),
        )

    def test_missing_membership_reads_as_empty(self) -> None:
        self._write_db({"cursorAuth/accessToken": _fake_jwt("auth0|user_01IDB")})
        token, plan = fetch_quota._read_cursor_state_db(self.db) or (None, None)
        self.assertEqual(plan, "")
        self.assertIsNotNone(token)

    def test_quoted_and_bytes_cells_decode(self) -> None:
        # VS Code stores a JSON-quoted string, and older builds a blob.
        self._write_db({"cursorAuth/accessToken": '"auth0|user_01Q"'})
        self.assertEqual(
            fetch_quota._read_cursor_state_db(self.db), ("auth0|user_01Q", "")
        )

    def test_db_without_a_token_reads_as_nothing(self) -> None:
        self._write_db({"cursorAuth/stripeMembershipType": "pro"})
        self.assertIsNone(fetch_quota._read_cursor_state_db(self.db))

    def test_a_locked_db_falls_back_to_opening_it_immutable(self) -> None:
        # The IDE holds a write lock while it saves; the read must still work.
        self._write_db({"cursorAuth/accessToken": _fake_jwt("auth0|user_01LOCK")})
        real_connect = sqlite3.connect
        seen: list[str] = []

        def refusing_connect(
            database: str,
            timeout: float = 5.0,
            check_same_thread: bool = True,
            uri: bool = False,
        ) -> sqlite3.Connection:
            seen.append(database)
            if "immutable=1" not in database:
                raise sqlite3.OperationalError("database is locked")
            return real_connect(
                database, timeout=timeout, check_same_thread=check_same_thread, uri=uri
            )

        with patch.object(sqlite3, "connect", refusing_connect):
            got = fetch_quota._read_cursor_state_db(self.db)

        self.assertEqual(len(seen), 2)
        self.assertNotIn("immutable=1", seen[0])
        self.assertIn("immutable=1", seen[1])
        self.assertEqual(got, (_fake_jwt("auth0|user_01LOCK"), ""))

    def test_a_corrupt_db_reads_as_nothing_instead_of_raising(self) -> None:
        self.db.write_bytes(b"not a sqlite database")
        self.assertIsNone(fetch_quota._read_cursor_state_db(self.db))

    def test_a_db_without_the_item_table_reads_as_nothing(self) -> None:
        self._write_db({"k": "v"}, table="Other")
        self.assertIsNone(fetch_quota._read_cursor_state_db(self.db))

    def test_an_undecodable_cell_does_not_cost_the_token(self) -> None:
        # A TEXT cell that is not UTF-8 used to abort the whole query, so a
        # garbage membership string signed the user out of the widget.
        self._write_db({"cursorAuth/accessToken": _fake_jwt("auth0|user_01IDB")})
        con = sqlite3.connect(self.db)
        with con:
            con.execute(
                "UPDATE ItemTable SET value = CAST(? AS TEXT) WHERE key = ?",
                (b"\xff\xfe pro", "cursorAuth/stripeMembershipType"),
            )
        con.close()

        self.assertEqual(
            fetch_quota._read_cursor_state_db(self.db),
            (_fake_jwt("auth0|user_01IDB"), ""),
        )

    def test_load_falls_through_to_the_db_when_auth_json_is_absent(self) -> None:
        self._write_db(
            {
                "cursorAuth/accessToken": _fake_jwt("auth0|user_01IDB"),
                "cursorAuth/stripeMembershipType": "pro",
            }
        )
        env = config_env(
            QUOTA_WIDGET_CACHE=self.tmp.name,
            QUOTA_WIDGET_CURSOR_AUTH=str(Path(self.tmp.name) / "nope.json"),
            QUOTA_WIDGET_CURSOR_STATE_DB=str(self.db),
        )
        env.__enter__()
        self.addCleanup(env.__exit__, None, None, None)

        loaded = fetch_quota._load_cursor_auth()
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded["sub"], "user_01IDB")
        self.assertEqual(loaded["plan"], "pro")

    def test_auth_json_wins_over_the_db(self) -> None:
        # cursor-agent is refreshed on its own schedule; the IDE's copy can be
        # a stale session, so the newer source is read first.
        auth_json = Path(self.tmp.name) / "auth.json"
        auth_json.write_text(
            json.dumps({"accessToken": _fake_jwt("auth0|user_01AGENT")})
        )
        self._write_db(
            {
                "cursorAuth/accessToken": _fake_jwt("auth0|user_01IDB"),
                "cursorAuth/stripeMembershipType": "ultra",
            }
        )
        env = config_env(
            QUOTA_WIDGET_CACHE=self.tmp.name,
            QUOTA_WIDGET_CURSOR_AUTH=str(auth_json),
            QUOTA_WIDGET_CURSOR_STATE_DB=str(self.db),
        )
        env.__enter__()
        self.addCleanup(env.__exit__, None, None, None)

        loaded = fetch_quota._load_cursor_auth()
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded["sub"], "user_01AGENT")
        self.assertEqual(loaded["plan"], "")

    def test_a_claim_that_cannot_be_encoded_names_no_session(self) -> None:
        # A JSON escape can spell a lone surrogate. The sub is percent-encoded
        # into the session cookie, so quoting it raises, and a raised provider
        # is reported to the panel as a dropped connection.
        self.assertIsNone(fetch_quota._jwt_sub(_fake_jwt("auth0|user_\ud800")))
        self.assertIsNotNone(fetch_quota._jwt_sub(_fake_jwt("auth0|user_01ABC")))
        self.assertIsNotNone(fetch_quota._jwt_sub(_fake_jwt("auth0|üser_01ABC")))


class IsoToMsTest(unittest.TestCase):
    def test_parses_zulu(self) -> None:
        ms = fetch_quota.iso_to_ms("2026-05-02T14:11:55.000Z")
        self.assertIsNotNone(ms)
        assert ms is not None
        self.assertGreater(ms, 1_700_000_000_000)

    def test_rejects_missing_or_garbage(self) -> None:
        self.assertIsNone(fetch_quota.iso_to_ms(None))
        self.assertIsNone(fetch_quota.iso_to_ms("nope"))

    def test_a_bare_epoch_is_read_in_the_unit_it_arrives_in(self) -> None:
        # The same field arrives in seconds from one provider and in
        # milliseconds from another; reading both as seconds put a millisecond
        # reset thousands of years out.
        self.assertEqual(fetch_quota.iso_to_ms(1777000000), 1_777_000_000_000)
        self.assertEqual(fetch_quota.iso_to_ms(1777000000.25), 1_777_000_000_250)
        self.assertEqual(fetch_quota.iso_to_ms(1_777_000_000_250), 1_777_000_000_250)

    def test_a_value_that_is_not_an_instant_is_absent(self) -> None:
        for value in (True, [1777000000], {"at": 1777000000}, ""):
            with self.subTest(value=value):
                self.assertIsNone(fetch_quota.iso_to_ms(value))

    def test_offset_free_timestamp_is_utc_not_host_local(self) -> None:
        # A payload timestamp with no offset means UTC. Resolving it against
        # the host zone put the same reading an hour or nine off depending on
        # where plasmashell ran, and the DST offset made it move twice a year.
        expected = int(
            dt.datetime(2026, 5, 2, 14, 11, 55, tzinfo=dt.UTC).timestamp() * 1000
        )
        for zone in ("UTC", "America/New_York", "Europe/Warsaw", "Asia/Kolkata"):
            with self.subTest(tz=zone), _local_tz(zone):
                self.assertEqual(fetch_quota.iso_to_ms("2026-05-02T14:11:55"), expected)

    def test_explicit_offset_wins_over_the_host_zone(self) -> None:
        expected = int(
            dt.datetime(2026, 5, 2, 12, 11, 55, tzinfo=dt.UTC).timestamp() * 1000
        )
        with _local_tz("America/Los_Angeles"):
            self.assertEqual(
                fetch_quota.iso_to_ms("2026-05-02T14:11:55+02:00"), expected
            )


@contextlib.contextmanager
def _local_tz(zone: str) -> Iterator[None]:
    """Run a block with the process in `zone`, the way a user's shell is."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = zone
    if hasattr(time, "tzset"):
        time.tzset()
    try:
        yield
    finally:
        if previous is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = previous
        if hasattr(time, "tzset"):
            time.tzset()


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
        self.assertEqual(window["label"], "Weekly")

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

    def test_grok_ratio_overflow_is_not_a_reading(self) -> None:
        # 100 * 1.7e308 is Infinity before the division even runs.
        period = fetch_quota._parse_grok_period({"used": 1.7e308, "monthlyLimit": 5})
        self.assertIsNone(period["util"])
        self.assertNotIn("Infinity", json.dumps(period))

    def test_cursor_meter_refuses_a_negative_limit(self) -> None:
        meter = fetch_quota._cursor_meter(
            {"enabled": True, "used": 250, "limit": -100}, "On-demand", "cents", 1
        )
        assert meter is not None
        self.assertIsNone(meter["util"])

    def test_emittable_drops_non_finite_and_keeps_the_rest(self) -> None:
        self.assertIsNone(fetch_quota._emittable(float("nan")))
        self.assertIsNone(fetch_quota._emittable(float("inf")))
        self.assertEqual(fetch_quota._emittable(12.5), 12.5)
        self.assertEqual(fetch_quota._emittable("12.50"), "12.50")
        self.assertTrue(fetch_quota._emittable(True))

    def test_codex_reset_counts_stay_finite(self) -> None:
        resets = fetch_quota._codex_reset_credits(
            {
                "rate_limit_reset_credits": {
                    "available_count": float("inf"),
                    "applicable_available_count": 2,
                }
            }
        )
        self.assertEqual(resets["available"], 0)
        self.assertEqual(resets["applicable"], 2)
        self.assertNotIn("Infinity", json.dumps(resets))

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

    def test_on_demand_cap_of_zero_is_a_cap(self) -> None:
        # A zero cap forbids on-demand spend, so dropping it for the snake_case
        # fallback would report the plan as uncapped.
        for key in ("onDemandCap", "on_demand_cap"):
            parsed = fetch_quota._parse_grok_period(
                {key: 0, "used": 250, "monthlyLimit": 1000}
            )
            self.assertEqual(parsed["on_demand_cap"], 0)

    def test_snake_case_limit_is_used_when_the_camel_key_is_absent(self) -> None:
        parsed = fetch_quota._parse_grok_period(
            {"used": 250, "monthly_limit": 1000, "on_demand_cap": 750}
        )
        self.assertEqual(parsed["util"], 25.0)
        self.assertEqual(parsed["on_demand_cap"], 750)


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
        self.assertEqual(
            _scoped(out), {"ok": False, "error": "http-503", "transient": True}
        )

    def test_signed_out_reports_401(self) -> None:
        out = self._fetch(401, None, 200)
        self.assertEqual(
            _scoped(out), {"ok": False, "error": "http-401", "transient": False}
        )

    def test_offline_reports_net(self) -> None:
        out = self._fetch(0, None, 0)
        self.assertEqual(_scoped(out), {"ok": False, "error": "net", "transient": True})


class GrokBothCallsOverlapTest(unittest.TestCase):
    """The weekly and monthly reads are two round trips to one host, so the
    poll waits the slower of the two rather than the sum of the two."""

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

    def test_both_calls_are_in_flight_at_once(self) -> None:
        # A barrier no sequential pair can pass: the second call is only
        # reached once the first has returned, and the wait then expires.
        both_in_flight = threading.Barrier(2, timeout=5)

        def fake_json(
            url: str,
            headers: dict[str, str],
            *,
            timeout: float = 12.0,
            data: bytes | None = None,
            method: str | None = None,
        ) -> tuple[int, object]:
            both_in_flight.wait()
            if "format=credits" in url:
                return 200, {
                    "config": {
                        "creditUsagePercent": 30,
                        "currentPeriod": {"type": "WEEKLY"},
                    }
                }
            return 200, {"config": {"used": 250, "monthlyLimit": 1000}}

        with patch.object(fetch_quota, "fetch_json", fake_json):
            out = fetch_quota.fetch_grok()

        self.assertTrue(out["ok"])
        self.assertEqual([p["label"] for p in out["periods"]], ["Weekly", "Monthly"])

    def test_a_refresh_from_either_call_reaches_the_others_account(self) -> None:
        # A 401 on the weekly call rotates the token; the reading is scoped by
        # the token in hand after both calls, not the one the poll started
        # with.
        def fake_json(
            url: str,
            headers: dict[str, str],
            *,
            timeout: float = 12.0,
            data: bytes | None = None,
            method: str | None = None,
        ) -> tuple[int, object]:
            if "format=credits" in url and headers["Authorization"] == "Bearer tok":
                return 401, None
            if "format=credits" in url:
                return 200, {
                    "config": {
                        "creditUsagePercent": 30,
                        "currentPeriod": {"type": "WEEKLY"},
                    }
                }
            return 200, {"config": {"used": 250, "monthlyLimit": 1000}}

        rotated = {"key": "new", "refresh_token": "r", "oidc_client_id": "cli"}

        with (
            patch.object(fetch_quota, "fetch_json", fake_json),
            patch.object(fetch_quota, "_refresh_grok", return_value=rotated),
            patch.object(
                fetch_quota,
                "_account_id",
                side_effect=lambda token, fallback=None: f"acct-{token}",
            ),
        ):
            out = fetch_quota.fetch_grok()

        self.assertTrue(out["ok"])
        self.assertEqual(out["account"], "acct-new")


class GrokAuthStoreTest(unittest.TestCase):
    """The store is written by several tools, so the selection cannot assume
    one expires_at spelling."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.auth = Path(tmp.name) / "grok.json"
        self.env = config_env(
            QUOTA_WIDGET_CACHE=tmp.name, QUOTA_WIDGET_GROK_AUTH=str(self.auth)
        )
        self.env.__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)
        os.environ[fetch_quota.NOW_MS_ENV] = str(PINNED_NOW_MS)
        self.addCleanup(os.environ.pop, fetch_quota.NOW_MS_ENV, None)

    def _write(self, store: JsonDict) -> None:
        self.auth.write_text(json.dumps(store))

    def test_longest_lived_entry_wins_across_offsets(self) -> None:
        # As text, "2026-04-22T09:00:00+01:00" > "2026-04-22T08:30:00Z", but
        # that entry expires an hour earlier. Lexicographic order picked the
        # shorter-lived token and the request then 401'd.
        self._write(
            {
                "cli::short": {
                    "key": "tok-short",
                    "expires_at": "2026-04-22T08:30:00Z",
                },
                "cli::long": {
                    "key": "tok-long",
                    "expires_at": "2026-04-22T09:00:00+01:00",
                },
            }
        )
        loaded = fetch_quota._load_grok_auth()
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded[1]["key"], "tok-short")

    def test_offset_free_expiry_is_read_as_utc(self) -> None:
        # A naive value used to raise against the aware clock, and the except
        # branch reported the token as never expiring.
        now = dt.datetime.fromtimestamp(PINNED_NOW_MS / 1000, tz=dt.UTC)
        stale = (now - dt.timedelta(hours=1)).replace(tzinfo=None).isoformat()
        fresh = (now + dt.timedelta(hours=1)).replace(tzinfo=None).isoformat()
        self.assertTrue(fetch_quota._token_expired({"expires_at": stale}))
        self.assertFalse(fetch_quota._token_expired({"expires_at": fresh}))

    def test_unparseable_expiry_is_not_read_as_expired(self) -> None:
        self.assertFalse(fetch_quota._token_expired({"expires_at": "whenever"}))
        self.assertFalse(fetch_quota._token_expired({}))


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

    def test_an_entry_from_another_release_is_dropped(self) -> None:
        # The panel reads a replayed payload field by field, so an entry a
        # release with a different payload shape left behind is not a reading
        # this one can serve. It is deleted, and the next write takes the file.
        path = Path(self.tmp.name) / "grok.json"
        fetch_quota._write_provider_cache(
            "grok", {"ok": True, "plan": "Grok"}, self.account
        )
        entry = json.loads(path.read_text())
        entry["schema"] = fetch_quota.PAYLOAD_SCHEMA + 1
        path.write_text(json.dumps(entry))

        self.assertIsNone(fetch_quota._read_provider_cache("grok", self.account))
        self.assertFalse(path.exists())
        self.assertFalse(fetch_quota._cache_holds_newer(path, 0, self.account))

        fetch_quota._write_provider_cache(
            "grok", {"ok": True, "plan": "Grok"}, self.account
        )
        got = fetch_quota._read_provider_cache("grok", self.account)
        assert got is not None
        self.assertEqual(got["plan"], "Grok")

    def test_unidentifiable_caller_reads_nothing(self) -> None:
        fetch_quota._write_provider_cache(
            "grok", {"ok": True, "plan": "Grok"}, self.account
        )
        self.assertIsNone(fetch_quota._read_provider_cache("grok", None))

    def test_an_entry_that_is_not_an_object_reads_as_absent(self) -> None:
        path = Path(self.tmp.name) / "grok.json"
        path.write_text("[1, 2]", encoding="utf-8")
        self.assertIsNone(fetch_quota._read_provider_cache("grok", self.account))
        self.assertFalse(
            fetch_quota._cache_holds_newer(path, 0, self.account),
        )

    def _write_expired_entry(self, name: str) -> Path:
        """Write a provider entry aged a minute past the retention window."""
        fetch_quota._write_provider_cache(
            name, {"ok": True, "plan": "Grok"}, self.account
        )
        path = Path(self.tmp.name) / f"{name}.json"
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
        return path

    def test_entry_older_than_the_stale_window_is_ignored(self) -> None:
        self._write_expired_entry("grok")
        self.assertIsNone(fetch_quota._stale_cache("grok", self.account))

    def test_an_expired_entry_is_deleted_whatever_account_asks(self) -> None:
        # The retention window outlives the account that wrote the entry: once
        # another account is signed in, nothing ever reads that file again
        # under the digest that scopes it, so an expiry checked only after the
        # account matched would never fire for it.
        path = self._write_expired_entry("grok")
        self.assertIsNone(fetch_quota._read_provider_cache("grok", "acct-2"))
        self.assertFalse(path.exists())

    def test_replayed_reading_reports_its_write_time_not_the_read_time(self) -> None:
        written_ms = int(
            (dt.datetime.now(dt.UTC).timestamp() - 3600) * 1000  # an hour ago
        )
        path = Path(self.tmp.name) / "grok.json"
        fetch_quota._write_provider_cache(
            "grok", {"ok": True, "plan": "Grok"}, self.account
        )
        entry = json.loads(path.read_text())
        entry["cached_ms"] = written_ms
        path.write_text(json.dumps(entry))

        replayed = fetch_quota._stale_cache("grok", self.account)
        assert replayed is not None
        self.assertEqual(replayed["fetched_ms"], written_ms)

    def test_a_late_run_does_not_rewind_the_entry(self) -> None:
        # Two runs overlap: the panel drops the slow one for outliving
        # pollTimeoutMs, and it lands its write after the poll that replaced
        # it. Its reading is the older one, so the entry keeps the newer.
        with config_env(QUOTA_WIDGET_NOW_MS=str(PINNED_NOW_MS)):
            fetch_quota._write_provider_cache(
                "grok",
                {"ok": True, "plan": "Grok", "fetched_ms": PINNED_NOW_MS},
                self.account,
            )
            fetch_quota._write_provider_cache(
                "grok",
                {
                    "ok": True,
                    "plan": "Grok from the run before",
                    "fetched_ms": PINNED_NOW_MS - 60_000,
                },
                self.account,
            )
            got = fetch_quota._read_provider_cache("grok", self.account)
        assert got is not None
        self.assertEqual(got["plan"], "Grok")

    def test_replaying_the_same_reading_leaves_the_entry_alone(self) -> None:
        payload = {"ok": True, "plan": "Grok", "fetched_ms": PINNED_NOW_MS}
        path = Path(self.tmp.name) / "grok.json"
        with config_env(QUOTA_WIDGET_NOW_MS=str(PINNED_NOW_MS)):
            fetch_quota._write_provider_cache("grok", payload, self.account)
            first = path.read_bytes()
            fetch_quota._write_provider_cache("grok", payload, self.account)
            second = path.read_bytes()
        self.assertEqual(first, second)

    def test_a_write_waits_for_the_entry_lock(self) -> None:
        # The "only move forward" rule is a comparison and a write with
        # nothing between them. Two runs that both read the older stamp before
        # either renames, then write in the order they reach the rename, leave
        # the older reading on disk, so the write has to hold the lock across
        # both halves.
        path = Path(self.tmp.name) / "grok.json"
        done = threading.Event()
        errors: list[BaseException] = []

        def write() -> None:
            try:
                fetch_quota._write_provider_cache(
                    "grok",
                    {"ok": True, "plan": "Grok", "fetched_ms": PINNED_NOW_MS},
                    self.account,
                )
            except BaseException as exc:  # noqa: BLE001 (surfaced below)
                errors.append(exc)
            finally:
                done.set()

        with config_env(QUOTA_WIDGET_NOW_MS=str(PINNED_NOW_MS)):
            with fetch_quota._entry_lock(path):
                worker = threading.Thread(target=write)
                worker.start()
                blocked = not done.wait(fetch_quota.ENTRY_LOCK_WAIT_S / 4)
            self.assertTrue(blocked, "the write did not wait for the entry lock")
            self.assertTrue(done.wait(fetch_quota.ENTRY_LOCK_WAIT_S * 2))
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            got = fetch_quota._read_provider_cache("grok", self.account)
        assert got is not None
        self.assertEqual(got["plan"], "Grok")

    def test_a_read_waits_for_the_entry_lock(self) -> None:
        # The retention verdict is computed from the entry the read saw, and
        # an expired one is unlinked on that basis, so the read holds the lock
        # too: a poll that renamed a fresh entry in between must not lose it
        # to a verdict computed against the file it replaced.
        path = Path(self.tmp.name) / "grok.json"
        done = threading.Event()
        read: list[fetch_quota.JsonDict | None] = []

        def load() -> None:
            read.append(fetch_quota._read_provider_cache("grok", self.account))
            done.set()

        with config_env(QUOTA_WIDGET_NOW_MS=str(PINNED_NOW_MS)):
            fetch_quota._write_provider_cache(
                "grok",
                {"ok": True, "plan": "Grok", "fetched_ms": PINNED_NOW_MS},
                self.account,
            )
            with fetch_quota._entry_lock(path):
                worker = threading.Thread(target=load)
                worker.start()
                self.assertFalse(
                    done.wait(fetch_quota.ENTRY_LOCK_WAIT_S / 4),
                    "the read did not wait for the entry lock",
                )
            self.assertTrue(done.wait(fetch_quota.ENTRY_LOCK_WAIT_S * 2))
            worker.join(timeout=5)
        self.assertEqual(
            read, [{"ok": True, "plan": "Grok", "fetched_ms": PINNED_NOW_MS}]
        )

    def test_two_racing_writers_never_rewind_the_entry(self) -> None:
        # The interleaving the lock rules out, exercised rather than argued:
        # one run's reading is older than the other's, and the entry must end
        # on the newer one whichever reaches the rename last.
        path = Path(self.tmp.name) / "grok.json"
        rounds = 40
        stamps = (PINNED_NOW_MS, PINNED_NOW_MS - 60_000)
        start = threading.Barrier(len(stamps))

        def write(stamp: int) -> None:
            start.wait(timeout=5)
            fetch_quota._write_provider_cache(
                "grok",
                {"ok": True, "plan": "Grok", "fetched_ms": stamp},
                self.account,
            )

        with config_env(QUOTA_WIDGET_NOW_MS=str(PINNED_NOW_MS)):
            for _ in range(rounds):

                workers = [threading.Thread(target=write, args=(s,)) for s in stamps]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(timeout=10)
                    self.assertFalse(worker.is_alive())
                entry = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(entry["cached_ms"], PINNED_NOW_MS)

    def test_a_second_account_replaces_the_entry(self) -> None:
        # The guard is scoped to one account: a different identity owns the
        # file, and the older reading it brings is its only one.
        with config_env(QUOTA_WIDGET_NOW_MS=str(PINNED_NOW_MS)):
            fetch_quota._write_provider_cache(
                "grok",
                {"ok": True, "plan": "Grok", "fetched_ms": PINNED_NOW_MS},
                self.account,
            )
            fetch_quota._write_provider_cache(
                "grok",
                {
                    "ok": True,
                    "plan": "Grok",
                    "fetched_ms": PINNED_NOW_MS - 60_000,
                },
                "acct-2",
            )
            first = fetch_quota._read_provider_cache("grok", self.account)
            second = fetch_quota._read_provider_cache("grok", "acct-2")
        self.assertIsNone(first)
        self.assertIsNotNone(second)


class AccountKeyTest(unittest.TestCase):
    """The account digest is a scope, not a published identifier.

    The value behind it is a short vendor account id, so a digest of it that
    anyone can compute from a guess is a way to name the account, not a way to
    keep the entry to the account. The per-installation key is what makes the
    guess useless; the scoping it serves is unchanged, because it only ever
    compares two digests taken under the same key.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _digest_under(self, cache: Path, account: str) -> str | None:
        """A digest taken with no key loaded, so the file is the only source."""
        saved = fetch_quota._ACCOUNT_SALT
        self.addCleanup(setattr, fetch_quota, "_ACCOUNT_SALT", saved)
        fetch_quota._ACCOUNT_SALT = None
        with config_env(QUOTA_WIDGET_CACHE=str(cache)):
            return fetch_quota._digest(account)

    def test_the_digest_is_not_the_bare_hash_of_the_account_id(self) -> None:
        cache = Path(self.tmp.name) / "cache"
        bare = hashlib.sha256(b"user_01ABC").hexdigest()[:16]
        self.assertNotEqual(self._digest_under(cache, "user_01ABC"), bare)

    def test_two_installations_do_not_agree_on_the_same_account(self) -> None:
        one = Path(self.tmp.name) / "one"
        two = Path(self.tmp.name) / "two"
        self.assertNotEqual(
            self._digest_under(one, "user_01ABC"), self._digest_under(two, "user_01ABC")
        )

    def test_the_key_is_reused_across_polls(self) -> None:
        cache = Path(self.tmp.name) / "cache"
        first = self._digest_under(cache, "user_01ABC")
        self.assertIsNotNone(first)
        self.assertEqual(first, self._digest_under(cache, "user_01ABC"))

    def test_the_key_is_written_private_next_to_the_entries(self) -> None:
        cache = Path(self.tmp.name) / "cache"
        self._digest_under(cache, "user_01ABC")
        key = cache / fetch_quota.ACCOUNT_SALT_NAME
        self.assertTrue(key.is_file())
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(cache.stat().st_mode & 0o777, fetch_quota.CACHE_DIR_MODE)

    def test_the_key_is_installed_once_and_never_replaced(self) -> None:
        # A run that mints its own key while another installs one leaves the
        # entries written under the first key readable by nobody, and scopes
        # the panel's digest by a key no file names.
        cache = Path(self.tmp.name) / "cache"
        fetch_quota._private_dir(cache)
        key = cache / fetch_quota.ACCOUNT_SALT_NAME
        first = b"\x01" * fetch_quota.ACCOUNT_SALT_BYTES
        fetch_quota._install_salt(key, first)
        fetch_quota._install_salt(key, b"\x02" * fetch_quota.ACCOUNT_SALT_BYTES)
        self.assertEqual(fetch_quota._salt_on_disk(key), first)

    def test_a_key_installed_while_this_run_was_deciding_is_the_one_kept(self) -> None:
        cache = Path(self.tmp.name) / "cache"
        fetch_quota._private_dir(cache)
        key = cache / fetch_quota.ACCOUNT_SALT_NAME
        installed = b"\x03" * fetch_quota.ACCOUNT_SALT_BYTES
        real = fetch_quota._salt_on_disk

        def blocked(path: Path) -> bytes | None:
            value = real(path)
            if path.name == fetch_quota.ACCOUNT_SALT_NAME and value is None:
                fetch_quota._atomic_write_json(key, {"salt": installed.hex()})
            return value

        with config_env(QUOTA_WIDGET_CACHE=str(cache)):
            fetch_quota._salt_on_disk = blocked
            try:
                chosen = fetch_quota._load_or_create_salt()
            finally:
                fetch_quota._salt_on_disk = real
        self.assertEqual(chosen, installed)
        self.assertEqual(real(key), installed)

    def test_two_runs_that_reach_it_together_end_on_one_key(self) -> None:
        cache = Path(self.tmp.name) / "cache"
        keys: list[bytes] = []
        real = fetch_quota._salt_on_disk
        arrived = 0
        gate = threading.Condition()

        def blocked(path: Path) -> bytes | None:
            nonlocal arrived
            value = real(path)
            if path.name == fetch_quota.ACCOUNT_SALT_NAME and value is None:
                # Hold a run at the read that found nothing, so the two that
                # reach it together both try to install a key.
                with gate:
                    arrived += 1
                    if arrived < 2:
                        gate.wait_for(lambda: arrived >= 2, timeout=10)
                    else:
                        gate.notify_all()
            return value

        def load() -> None:
            keys.append(fetch_quota._load_or_create_salt())

        with config_env(QUOTA_WIDGET_CACHE=str(cache)):
            fetch_quota._salt_on_disk = blocked
            try:
                threads = [threading.Thread(target=load) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)
            finally:
                fetch_quota._salt_on_disk = real
            on_disk = real(cache / fetch_quota.ACCOUNT_SALT_NAME)
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[0], on_disk)
        self.assertEqual(keys[1], on_disk)

    def test_a_key_left_empty_by_a_killed_run_is_replaced(self) -> None:
        cache = Path(self.tmp.name) / "cache"
        fetch_quota._private_dir(cache)
        key = cache / fetch_quota.ACCOUNT_SALT_NAME
        key.touch(mode=0o600)
        with config_env(QUOTA_WIDGET_CACHE=str(cache)):
            installed = fetch_quota._load_or_create_salt()
        self.assertEqual(fetch_quota._salt_on_disk(key), installed)


class RedactionTest(unittest.TestCase):
    """A printed line never spells out whose machine it came from.

    A path under the home directory carries the account name, and the journal
    and the panel both keep the line long after the poll wrote it.
    """

    def test_a_warning_hides_the_home_directory(self) -> None:
        stderr = io.StringIO()
        with config_env(QUOTA_WIDGET_HOME="/home/someone"):
            with contextlib.redirect_stderr(stderr):
                fetch_quota.warn("could not write /home/someone/.codex/auth.json")
        self.assertIn("~/.codex/auth.json", stderr.getvalue())
        self.assertNotIn("someone", stderr.getvalue())

    def test_an_exception_text_hides_the_home_directory(self) -> None:
        stderr = io.StringIO()
        with config_env(QUOTA_WIDGET_HOME="/home/someone"):
            with contextlib.redirect_stderr(stderr):
                fetch_quota.warn(
                    "cache entry failed: "
                    "[Errno 13] Permission denied: '/home/someone/.cache/qw/grok.json'"
                )
        self.assertNotIn("someone", stderr.getvalue())

    def test_a_home_outside_the_message_changes_nothing(self) -> None:
        self.assertEqual(fetch_quota._redact("nothing to hide"), "nothing to hide")


class ReadingAgeTest(unittest.TestCase):
    """Every payload carries the instant the reading was taken, so a consumer
    ages a value by its age and not by when it happened to arrive."""

    def test_stale_cache_reports_the_write_time(self) -> None:
        with config_env(QUOTA_WIDGET_NOW_MS=str(PINNED_NOW_MS)):
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            with config_env(QUOTA_WIDGET_CACHE=tmp.name):
                account = "acct-1"
                fetch_quota._write_provider_cache(
                    "grok", {"ok": True, "plan": "Grok"}, account
                )
                replayed = fetch_quota._stale_cache("grok", account)
        assert replayed is not None
        self.assertEqual(replayed["fetched_ms"], PINNED_NOW_MS)

    def test_fresh_reading_is_stamped_with_the_poll_clock(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cred = Path(tmp.name) / "cred.json"
        cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok"}}))

        def fake_http(
            url: str,
            headers: dict[str, str],
            **kwargs: object,
        ) -> tuple[int, object, None]:
            return 200, {"five_hour": {"utilization": 3, "resets_at": None}}, None

        with (
            config_env(
                QUOTA_WIDGET_NOW_MS=str(PINNED_NOW_MS),
                QUOTA_WIDGET_CLAUDE_CREDENTIALS=str(cred),
                QUOTA_WIDGET_CACHE=str(Path(tmp.name) / "cache"),
            ),
            patch.object(fetch_quota, "fetch_http", fake_http),
        ):
            out = fetch_quota.fetch_claude()
        self.assertTrue(out["ok"])
        self.assertEqual(out["fetched_ms"], PINNED_NOW_MS)


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

    def test_decomposed_and_composed_spellings_share_one_scope(self) -> None:
        # An NFD id (a macOS- or vendor-produced string) and its NFC twin are
        # one account, and a byte-unequal digest would split their cache.
        nfc = unicodedata.normalize("NFC", "café-01ABC")
        nfd = unicodedata.normalize("NFD", "café-01ABC")
        self.assertNotEqual(nfc, nfd)
        self.assertEqual(fetch_quota._digest(nfc), fetch_quota._digest(nfd))
        self.assertEqual(
            fetch_quota._account_id(_fake_jwt(nfc)),
            fetch_quota._account_id(_fake_jwt(nfd)),
        )

    def test_an_unencodable_id_is_no_id_rather_than_a_crash(self) -> None:
        # json.loads decodes a "\ud800" escape into a lone surrogate, which
        # encodes to nothing; raising here would lose the whole provider card
        # to a digest and reach the panel as a network error.
        lone = json.loads('"\\ud800"')
        self.assertIsNone(fetch_quota._digest(lone))
        self.assertIsNone(fetch_quota._account_id(_fake_jwt(lone), lone))


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
        cred = Path(tmp.name) / "cred.json"
        cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok"}}))
        os.environ["QUOTA_WIDGET_CLAUDE_CREDENTIALS"] = str(cred)
        self.addCleanup(lambda: os.environ.pop("QUOTA_WIDGET_CLAUDE_CREDENTIALS", None))
        body = b"account user_01ABC@example.com not found"
        out = io.StringIO()
        # main() reloads the config, so the credential path travels as env.
        with (
            config_env(
                QUOTA_WIDGET_CACHE=tmp.name,
                QUOTA_WIDGET_CLAUDE_CREDENTIALS=str(cred),
            ),
            patch.object(urllib.request, "urlopen", side_effect=self._error(body)),
            patch.object(sys, "argv", ["fetch_quota.py"]),
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

    def test_pinned_clock_must_be_epoch_ms(self) -> None:
        for bad in ("yesterday", "", "  ", "1.5", "-1"):
            with (
                self.subTest(bad=bad),
                self.assertRaises(fetch_quota.ConfigError) as ctx,
            ):
                fetch_quota.load_config({"QUOTA_WIDGET_NOW_MS": bad})
            self.assertIn("QUOTA_WIDGET_NOW_MS", str(ctx.exception))

    def test_pinned_clock_must_be_a_real_date(self) -> None:
        # now_utc() adds the pin as a timedelta, so a value past datetime's
        # range raised mid-poll, after a refresh had already retired the old
        # token. The pin is a knob like any other: it is rejected up front.
        for bad in (
            str(fetch_quota.MAX_PINNED_MS + 1),
            "99999999999999999999",
            "-1" + "0" * 20,
        ):
            with (
                self.subTest(bad=bad),
                self.assertRaises(fetch_quota.ConfigError) as ctx,
            ):
                fetch_quota.load_config({"QUOTA_WIDGET_NOW_MS": bad})
            self.assertIn("QUOTA_WIDGET_NOW_MS", str(ctx.exception))

    def test_a_misspelled_knob_is_rejected_not_ignored(self) -> None:
        # An unknown name used to be indistinguishable from an unset one: the
        # poll succeeded and the setting the user asked for did nothing.
        with self.assertRaises(fetch_quota.ConfigError) as ctx:
            fetch_quota.load_config(
                {
                    "QUOTA_WIDGET_HOME": "/home/widget",
                    "QUOTA_WIDGET_CACHE_MAX_AGES": "60",
                }
            )
        self.assertIn("QUOTA_WIDGET_CACHE_MAX_AGES", str(ctx.exception))

    def test_every_documented_knob_is_accepted(self) -> None:
        # The help table and the membership test are one list; a name in one
        # and not the other would make a valid setting fail the poll.
        self.assertEqual(
            {name for name, _ in fetch_quota.ENV_DOCS}, fetch_quota.KNOWN_ENV
        )
        for name, _ in fetch_quota.ENV_DOCS:
            with self.subTest(name=name):
                self.assertIn(name, fetch_quota.HELP)

    def test_unrelated_variables_are_left_alone(self) -> None:
        cfg = fetch_quota.load_config(
            {
                "QUOTA_WIDGET_HOME": "/home/widget",
                "XDG_CACHE_HOME": "/xdg/cache",
                "PATH": "/usr/bin",
                "QUOTA_WIDGET": "",
            }
        )
        self.assertEqual(cfg.cache_dir, Path("/xdg/cache/quota-widget"))

    def test_a_misspelled_knob_reports_config_instead_of_polling(self) -> None:
        out = io.StringIO()
        with (
            patch.dict(os.environ, {"QUOTA_WIDGET_CASH": "/tmp"}),
            patch.object(fetch_quota, "fetch_claude") as claude,
            patch.object(sys, "argv", ["fetch_quota.py"]),
            contextlib.redirect_stderr(io.StringIO()),
            contextlib.redirect_stdout(out),
            self.assertRaises(SystemExit),
        ):
            fetch_quota.main([])
        claude.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())["error"], "config")

    def test_a_bad_pin_reports_config_instead_of_killing_the_poll(self) -> None:
        # The emit path reads the clock, so an unvalidated pin would abort the
        # whole run rather than produce the JSON plasmashell needs.
        out = io.StringIO()
        with (
            patch.dict(os.environ, {"QUOTA_WIDGET_NOW_MS": "yesterday"}),
            patch.object(sys, "argv", ["fetch_quota.py"]),
            contextlib.redirect_stderr(io.StringIO()),
            contextlib.redirect_stdout(out),
            self.assertRaises(SystemExit),
        ):
            fetch_quota.main([])

        emitted = json.loads(out.getvalue())
        self.assertEqual(emitted["error"], "config")
        self.assertIn("QUOTA_WIDGET_NOW_MS", emitted["config_error"])
        self.assertIsInstance(emitted["fetched_ms"], int)

    def test_describe_exposes_paths_only(self) -> None:
        described = fetch_quota.load_config(
            {"QUOTA_WIDGET_HOME": "/home/widget"}
        ).describe()
        self.assertEqual(described["cache_dir"], "/home/widget/.cache/quota-widget")
        self.assertNotIn("token", json.dumps(described).lower())

    def test_describe_names_the_account_key_without_printing_it(self) -> None:
        # Whether the key is an input to the run is worth answering; the key
        # is not, and --print-config writes to the same stdout a caller logs.
        generated = fetch_quota.load_config(
            {"QUOTA_WIDGET_HOME": "/home/widget"}
        ).describe()
        pinned = fetch_quota.load_config(
            {
                "QUOTA_WIDGET_HOME": "/home/widget",
                "QUOTA_WIDGET_ACCOUNT_SALT": "11" * 32,
            }
        ).describe()
        self.assertEqual(generated["account_salt"], "generated")
        self.assertEqual(pinned["account_salt"], "pinned")
        self.assertNotIn("11" * 32, json.dumps(pinned))

    def test_a_pinned_account_key_must_be_hex_of_the_right_length(self) -> None:
        for value in ("zzzz", "11" * 16, "11" * 64):
            with self.subTest(value=value):
                with self.assertRaises(fetch_quota.ConfigError) as ctx:
                    fetch_quota.load_config(
                        {
                            "QUOTA_WIDGET_HOME": "/home/widget",
                            "QUOTA_WIDGET_ACCOUNT_SALT": value,
                        }
                    )
                self.assertIn("QUOTA_WIDGET_ACCOUNT_SALT", str(ctx.exception))

    def test_an_unpinned_account_key_is_left_to_the_fetcher(self) -> None:
        cfg = fetch_quota.load_config({"QUOTA_WIDGET_HOME": "/home/widget"})
        self.assertIsNone(cfg.account_salt)

    def test_poll_reports_the_effective_cache_window(self) -> None:
        # The panel ages a kept reading against this number, so an override has
        # to travel with the payload rather than live only in the fetcher.
        out = io.StringIO()
        stub = {"ok": False, "error": "net"}
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        with (
            config_env(
                QUOTA_WIDGET_CACHE=str(Path(tmp) / "cache"),
                QUOTA_WIDGET_CACHE_MAX_AGE_S="300",
            ),
            patch.object(fetch_quota, "fetch_claude", return_value=dict(stub)),
            patch.object(fetch_quota, "fetch_cursor", return_value=dict(stub)),
            patch.object(fetch_quota, "fetch_grok", return_value=dict(stub)),
            patch.object(fetch_quota, "fetch_codex", return_value=dict(stub)),
            contextlib.redirect_stderr(io.StringIO()),
            contextlib.redirect_stdout(out),
            self.assertRaises(SystemExit),
        ):
            fetch_quota.main([])
        self.assertEqual(json.loads(out.getvalue())["cache_max_age_s"], 300)

    def _poll_payload(self, **env: str) -> JsonDict:
        """The payload one poll prints, with every provider stubbed out."""
        out = io.StringIO()
        stub = {"ok": False, "error": "net"}
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        with (
            config_env(QUOTA_WIDGET_CACHE=str(Path(tmp) / "cache"), **env),
            patch.object(fetch_quota, "fetch_claude", return_value=dict(stub)),
            patch.object(fetch_quota, "fetch_cursor", return_value=dict(stub)),
            patch.object(fetch_quota, "fetch_grok", return_value=dict(stub)),
            patch.object(fetch_quota, "fetch_codex", return_value=dict(stub)),
            contextlib.redirect_stderr(io.StringIO()),
            contextlib.redirect_stdout(out),
            self.assertRaises(SystemExit),
        ):
            fetch_quota.main([])
        parsed: JsonDict = json.loads(out.getvalue())
        return parsed

    def test_poll_reports_a_watchdog_budget_its_own_timeout_justifies(self) -> None:
        # The panel's watchdog drops a run that outlasts it, and
        # QUOTA_WIDGET_HTTP_TIMEOUT is settable past what a constant in the QML
        # holds, so the budget a poll is entitled to take travels with it.
        payload = self._poll_payload(QUOTA_WIDGET_HTTP_TIMEOUT="300")
        self.assertEqual(
            payload["poll_timeout_s"],
            fetch_quota.MAX_SEQUENTIAL_REQUESTS * 300.0 + fetch_quota.POLL_OVERHEAD_S,
        )
        # Longer than the panel's own 10-minute default, which is the case that
        # would otherwise drop every poll.
        self.assertGreater(payload["poll_timeout_s"], 10 * 60)

    def test_the_budget_scales_with_the_timeout_it_is_derived_from(self) -> None:
        default = self._poll_payload()["poll_timeout_s"]
        long_run = self._poll_payload(QUOTA_WIDGET_HTTP_TIMEOUT="60")["poll_timeout_s"]
        self.assertGreater(long_run, default)
        self.assertEqual(
            long_run - default,
            fetch_quota.MAX_SEQUENTIAL_REQUESTS
            * (60.0 - fetch_quota.DEFAULT_HTTP_TIMEOUT_S),
        )

    def test_print_config_reports_the_budget_it_would_report(self) -> None:
        out = io.StringIO()
        with (
            config_env(QUOTA_WIDGET_HTTP_TIMEOUT="45"),
            contextlib.redirect_stderr(io.StringIO()),
            contextlib.redirect_stdout(out),
            self.assertRaises(SystemExit),
        ):
            fetch_quota.main(["--print-config"])
            described = json.loads(out.getvalue())["config"]
            self.assertEqual(
                described["poll_timeout_s"],
                fetch_quota.config().poll_timeout_s,
            )

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

    def test_clear_cache_erases_the_entries_and_the_account_key(self) -> None:
        # The key outlives the entries it scopes, so erasing one without the
        # other leaves a key that can still read a restored backup.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cache_dir = Path(tmp.name) / "cache"
        with config_env(QUOTA_WIDGET_CACHE=str(cache_dir)):
            # The key is loaded once per process, so drop it and let this
            # cache directory create its own, the way a fresh install would.
            saved = fetch_quota._ACCOUNT_SALT
            self.addCleanup(setattr, fetch_quota, "_ACCOUNT_SALT", saved)
            fetch_quota._ACCOUNT_SALT = None
            account = fetch_quota._account_id(_fake_jwt("user_01GROK"))
            fetch_quota._write_provider_cache(
                "grok", {"ok": True, "plan": "G"}, account
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                with self.assertRaises(SystemExit) as ctx:
                    fetch_quota.main(["--clear-cache"])
        self.assertEqual(ctx.exception.code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(sorted(payload["removed"]), ["account-salt", "grok.json"])
        # The advisory locks carry no reading and a poll may hold one, so they
        # are the only thing left in the directory, and they stay empty.
        left = sorted(p for p in cache_dir.iterdir())
        self.assertEqual([p.name for p in left], ["grok.json.lock"])
        self.assertEqual(left[0].stat().st_size, 0)

    def test_clear_cache_on_an_empty_cache_dir_is_not_an_error(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cache_dir = Path(tmp.name) / "cache"
        with config_env(QUOTA_WIDGET_CACHE=str(cache_dir)):
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                with self.assertRaises(SystemExit) as ctx:
                    fetch_quota.main(["--clear-cache"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["removed"], [])
        self.assertFalse(cache_dir.exists())

    def test_unknown_argument_exits_nonzero(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                fetch_quota.main(["--nope"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("usage: fetch_quota.py", stderr.getvalue())

    def test_a_usage_error_points_at_help(self) -> None:
        # The fetcher, the installer, and print_smoke answer the same mistake
        # the same way, so the closing line is one string in each.
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                fetch_quota.main(["--nope"])
        self.assertIn("try 'fetch_quota.py --help'", stderr.getvalue())

    def test_unknown_argument_outranks_a_broken_config(self) -> None:
        # A typo is the operator's to fix, so it stays a usage error even when
        # the environment is bad: the config payload exits 0, and a script
        # reading stdout would never learn its argument was wrong.
        stderr = io.StringIO()
        with (
            patch.dict(os.environ, {"QUOTA_WIDGET_CACHE": "relative/path"}),
            contextlib.redirect_stderr(stderr),
        ):
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                with self.assertRaises(SystemExit) as ctx:
                    fetch_quota.main(["--nope"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("'--nope'", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")

    def test_extra_argument_names_the_extra_one(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                fetch_quota.main(["--print-config", "extra"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("'extra'", stderr.getvalue())
        self.assertNotIn("--print-config", stderr.getvalue().splitlines()[0])

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

    def test_providers_are_polled_concurrently(self) -> None:
        # A poll waits on the network, so one provider's round trip must not
        # be spent before the next one starts. The barrier only clears once all
        # four are inside their fetch, which a sequential main() never reaches.
        names = ("claude", "cursor", "grok", "codex")
        started = threading.Barrier(len(names), timeout=10)
        patches = [
            patch.object(
                fetch_quota,
                f"fetch_{name}",
                lambda name=name: (started.wait(), {"ok": True, "provider": name})[1],
            )
            for name in names
        ]
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                with self.assertRaises(SystemExit):
                    fetch_quota.main([])
        payload = json.loads(stdout.getvalue())
        self.assertEqual([payload[name]["provider"] for name in names], list(names))


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
            # dict() picks its overload from a homogeneous value type, and this
            # store is mixed JSON, so the copy is the deliberate cast.
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
                got = fetch_quota._read_provider_cache("claude", account)
                self.assertEqual(got, {**payload, "fetched_ms": PINNED_NOW_MS})

    def test_vscdb_cell_that_is_not_utf8_is_dropped(self) -> None:
        self.assertIsNone(fetch_quota._vscdb_str(b"\xff\xfe not utf-8"))
        self.assertEqual(fetch_quota._vscdb_str('"Ünïcodé"'), "Ünïcodé")


class TransientFailureTest(unittest.TestCase):
    def test_transient_status_codes(self) -> None:
        self.assertTrue(fetch_quota._transient_failure(429))
        self.assertTrue(fetch_quota._transient_failure(503))
        self.assertTrue(fetch_quota._transient_failure(500))
        self.assertFalse(fetch_quota._transient_failure(401))
        self.assertFalse(fetch_quota._transient_failure(403))
        # A request that never landed is the case the panel already keeps a
        # reading through, so the on-disk entry has to be served too.
        self.assertTrue(fetch_quota._transient_failure(0))


class FailurePayloadTest(unittest.TestCase):
    """A failure carries the classification the panel acts on.

    The panel keeps the card it holds through a transient failure and replaces
    it on a final one, so that decision travels in the payload: the fetcher is
    the only place that knows the rule, and the panel reading it back out of the
    error code is how a code added later comes out final.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.auth = Path(self.tmp.name) / "codex.json"
        self.auth.write_text(
            json.dumps(
                {
                    "tokens": {
                        "access_token": _jwt_with_exp(4102444800),
                        "account_id": "acct_test",
                    }
                }
            )
        )
        env = config_env(
            QUOTA_WIDGET_CACHE=self.tmp.name, QUOTA_WIDGET_CODEX_AUTH=str(self.auth)
        )
        env.__enter__()
        self.addCleanup(env.__exit__, None, None, None)

    def _fetch(self, status: int) -> JsonDict:
        fake = _http_returning_pair(status, None)
        with patch.object(fetch_quota, "fetch_json", fake):
            out = fetch_quota.fetch_codex()
        fake.hit(self, fetch_quota.CODEX_USAGE_URL)
        return out

    def test_a_sign_out_is_final_and_names_its_account(self) -> None:
        # The other three providers name the account a failure was made for, so
        # the panel can tell this account's sign-in from another account's blip.
        out = self._fetch(401)
        self.assertEqual(
            _scoped(out), {"ok": False, "error": "http-401", "transient": False}
        )
        self.assertIsInstance(out.get("account"), str)

    def test_a_server_error_holds_the_card(self) -> None:
        self.assertIs(self._fetch(503)["transient"], True)

    def test_a_config_error_is_final(self) -> None:
        # Nothing ran, so every provider is final: the panel replaces each card
        # rather than ageing a reading against a window it never polled with.
        out = io.StringIO()
        with (
            patch.dict(os.environ, {"QUOTA_WIDGET_CACHE_MAX_AGE_S": "0"}),
            contextlib.redirect_stderr(io.StringIO()),
            contextlib.redirect_stdout(out),
            self.assertRaises(SystemExit),
        ):
            fetch_quota.main([])
        payload = json.loads(out.getvalue())
        for provider in ("claude", "cursor", "grok", "codex"):
            self.assertIs(payload[provider]["transient"], False, provider)


class Utf8StreamTest(unittest.TestCase):
    """The journal and the payload are written as UTF-8 whatever the locale is.

    A plasmashell started without LANG gets a C locale, where the streams are
    ASCII: a warning naming a credential path or vendor text that is not ASCII
    then raises UnicodeEncodeError mid-poll, and the panel is left with no
    payload at all, which is the state this fetcher must never reach.
    """

    @staticmethod
    def _ascii_stream() -> tuple[io.TextIOWrapper, io.BytesIO]:
        raw = io.BytesIO()
        return io.TextIOWrapper(raw, encoding="ascii"), raw

    def test_warn_writes_non_ascii_to_an_ascii_locale(self) -> None:
        stderr, raw = self._ascii_stream()
        with patch.object(sys, "stderr", stderr):
            fetch_quota._use_utf8_streams()
            fetch_quota.warn("could not read /home/Ünïcodé/.codex/auth.json")

        stderr.flush()
        self.assertEqual(
            raw.getvalue(),
            "fetch_quota: could not read /home/Ünïcodé/.codex/auth.json\n".encode(),
        )

    def test_main_pins_the_streams_it_writes_through(self) -> None:
        stdout, _ = self._ascii_stream()
        stderr, _stderr_raw = self._ascii_stream()
        with (
            patch.object(sys, "stdout", stdout),
            patch.object(sys, "stderr", stderr),
            config_env(),
            self.assertRaises(SystemExit),
        ):
            fetch_quota.main(["--print-config"])
        self.assertEqual(stdout.encoding, "utf-8")
        self.assertEqual(stderr.encoding, "utf-8")

    def test_a_stream_without_reconfigure_is_left_alone(self) -> None:
        # A captured stream (this suite) has no reconfigure to call.
        with patch.object(sys, "stderr", io.StringIO()):
            fetch_quota._use_utf8_streams()


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

            def read(self, size: int = -1) -> bytes:
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
        self.assertEqual(_scoped(out), {"ok": False, "error": "net", "transient": True})
        logged = err.getvalue()
        self.assertIn("claude", logged)
        self.assertIn("meters exploded", logged)
        self.assertIn("Traceback", logged)

    def test_the_traceback_hides_the_home_directory(self) -> None:
        """A frame names a file under the checkout, and the checkout is under
        the home directory, so an unredacted traceback spells the account name
        into the journal on every install path."""
        home = "/home/someone"
        frame_file = Path(home) / "quota-widget" / "fetch_quota.py"
        namespace: dict[str, Any] = {}
        exec(  # noqa: S102 (a frame under a named home, which is the subject)
            compile(
                "def boom():\n    raise RuntimeError('meters exploded')\n",
                str(frame_file),
                "exec",
            ),
            namespace,
        )
        boom: Callable[[], JsonDict] = namespace["boom"]

        err = io.StringIO()
        with config_env(QUOTA_WIDGET_HOME=home):
            with contextlib.redirect_stderr(err):
                fetch_quota._safe_fetch("claude", boom)
        logged = err.getvalue()
        self.assertIn("Traceback", logged)
        self.assertIn("~/quota-widget/fetch_quota.py", logged)
        self.assertNotIn("someone", logged)


class CodexExpiryClockTest(unittest.TestCase):
    def test_expiry_reads_the_pinned_clock(self) -> None:
        now = 1_777_000_000_000
        with config_env(**{fetch_quota.NOW_MS_ENV: str(now)}):
            exp_s = (now + fetch_quota.TOKEN_SKEW_S * 1000) // 1000
            fresh = _jwt_with_exp(exp_s + 60)
            self.assertFalse(fetch_quota._codex_token_expired({"access_token": fresh}))
            stale = _jwt_with_exp(exp_s - 60)
            self.assertTrue(fetch_quota._codex_token_expired({"access_token": stale}))


# The credential file each provider of a ReplayTest poll reads: the key in
# CREDENTIAL_ENV, the file name under the fixture's auth/ directory, and what
# it holds. The tokens carry a sub claim, since that claim is what the account
# digest is taken from.
_REPLAY_CREDENTIALS = {
    "CLAUDE_CRED": ("claude.json", {"claudeAiOauth": {"accessToken": _fake_jwt("s")}}),
    "CODEX_AUTH": ("codex.json", {"tokens": {"access_token": _fake_jwt("s")}}),
    "GROK_AUTH": (
        "grok.json",
        {"x": {"key": "tok", "oidc_client_id": "c", "refresh_token": "r"}},
    ),
    "CURSOR_AUTH_JSON": ("cursor.json", {"accessToken": _fake_jwt("user_01TEST")}),
}


class ReplayTest(unittest.TestCase):
    """One pinned clock value plus one fixed HTTP script must reproduce the
    poll byte-for-byte, cache writes included."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.enterContext(config_env(QUOTA_WIDGET_CACHE=str(self.root)))
        os.environ[fetch_quota.NOW_MS_ENV] = str(PINNED_NOW_MS)
        self.addCleanup(lambda: os.environ.pop(fetch_quota.NOW_MS_ENV, None))

        auth = self.root / "auth"
        auth.mkdir(exist_ok=True)
        # The credentials go in the environment, not into the Config record:
        # main() loads the config from the environment before any provider
        # runs, so a path pointed only in the record is the default again by
        # the time a provider reads it.
        for name, (filename, store) in _REPLAY_CREDENTIALS.items():
            os.environ[CREDENTIAL_ENV[name]] = str(self._write(auth / filename, store))
            self.addCleanup(os.environ.pop, CREDENTIAL_ENV[name], None)

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
        # Only Claude reads its answer through fetch_http; the other three
        # providers go through fetch_json, so a body returned here would never
        # reach them.
        return 200, {"limits": [{"kind": "weekly_all", "percent": 12}]}, None

    def _fake_json(
        self,
        url: str,
        headers: dict[str, str],
        *,
        timeout: float = 12.0,
        data: bytes | None = None,
        method: str | None = None,
    ) -> tuple[int, object]:
        if "cursor.com" in url:
            return 200, {
                "membershipType": "pro",
                "individualUsage": {"plan": {"used": 1, "limit": 2}},
            }
        if "chatgpt.com" in url:
            return 200, {
                "plan_type": "pro",
                "rate_limit": {"primary_window": {"used_percent": 5}},
            }
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

    def test_the_replay_covers_every_provider(self) -> None:
        # A poll that answered nothing is byte-identical to itself, so the
        # replay above only means something once each provider has run.
        payload = json.loads(self._poll())
        for provider in ("claude", "cursor", "grok", "codex"):
            with self.subTest(provider=provider):
                self.assertTrue(payload[provider]["ok"], payload[provider])

    def test_replay_does_not_depend_on_a_running_cache(self) -> None:
        first = self._poll()
        for cached in self.root.glob("*.json"):
            if cached.name.startswith(("claude", "cursor", "grok", "codex")):
                cached.unlink()
        self.assertEqual(self._poll(), first)

    def _set_config(self, **fields: Any) -> None:
        """Repoint the active config for this test, then restore it."""
        self.addCleanup(setattr, fetch_quota, "_CONFIG", fetch_quota._CONFIG)
        fetch_quota._CONFIG = dataclasses.replace(fetch_quota.config(), **fields)

    def _poll_on_a_fresh_install(self, salt: str | None = None) -> str:
        """A poll whose cache holds no account key, which is what a first run,
        a sandboxed cache, and a run after --clear-cache each are. main()
        reloads the config from the environment, so the credentials this
        the credentials are in the environment, so they survive the reload."""
        cache = Path(tempfile.mkdtemp(dir=self.root))
        self.addCleanup(shutil.rmtree, cache, ignore_errors=True)
        self.addCleanup(
            setattr, fetch_quota, "_ACCOUNT_SALT", fetch_quota._ACCOUNT_SALT
        )
        fetch_quota._ACCOUNT_SALT = None
        env = {"QUOTA_WIDGET_CACHE": str(cache)}
        if salt is not None:
            env["QUOTA_WIDGET_ACCOUNT_SALT"] = salt
        with config_env(**env):
            return self._poll()

    def test_a_pinned_key_replays_a_fresh_install(self) -> None:
        first = self._poll_on_a_fresh_install("11" * 32)
        second = self._poll_on_a_fresh_install("11" * 32)
        self.assertEqual(first, second)

    def test_without_a_pinned_key_the_account_digest_is_the_whole_difference(
        self,
    ) -> None:
        # What the key leaks into a run: the digest each card carries and
        # nothing else, so two first runs of one machine differ in exactly the
        # fields a replay cannot reproduce.
        first = json.loads(self._poll_on_a_fresh_install())
        second = json.loads(self._poll_on_a_fresh_install())
        for provider in ("claude", "cursor", "grok", "codex"):
            with self.subTest(provider=provider):
                self.assertNotEqual(
                    first[provider]["account"], second[provider]["account"]
                )

    def test_a_pinned_key_is_used_and_not_kept(self) -> None:
        cache = Path(tempfile.mkdtemp(dir=self.root))
        self.addCleanup(shutil.rmtree, cache, ignore_errors=True)
        self.addCleanup(
            setattr, fetch_quota, "_ACCOUNT_SALT", fetch_quota._ACCOUNT_SALT
        )
        fetch_quota._ACCOUNT_SALT = None
        self._set_config(cache_dir=cache, account_salt=bytes.fromhex("11" * 32))
        self.assertEqual(fetch_quota._load_or_create_salt(), bytes.fromhex("11" * 32))
        # A key the run named is one of its inputs. Writing it would leave a
        # chosen key on the machine for every later poll to scope entries
        # under, and an unset poll would then read them back.
        self.assertFalse((cache / fetch_quota.ACCOUNT_SALT_NAME).exists())

    def test_a_key_on_disk_wins_over_a_pinned_one(self) -> None:
        # The entries in that directory were taken under the file's key, so a
        # poll that renamed them would orphan every one of them.
        cache = Path(tempfile.mkdtemp(dir=self.root))
        self.addCleanup(shutil.rmtree, cache, ignore_errors=True)
        (cache / fetch_quota.ACCOUNT_SALT_NAME).write_text(
            json.dumps({"salt": "22" * 32})
        )
        self.addCleanup(
            setattr, fetch_quota, "_ACCOUNT_SALT", fetch_quota._ACCOUNT_SALT
        )
        fetch_quota._ACCOUNT_SALT = None
        self._set_config(cache_dir=cache, account_salt=bytes.fromhex("11" * 32))
        self.assertEqual(fetch_quota._load_or_create_salt(), bytes.fromhex("22" * 32))


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
        self.enterContext(config_env(QUOTA_WIDGET_CACHE=self.tmp.name))

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

        usage = _http_returning(200, {"five_hour": {"utilization": 4}})
        with (
            patch.object(fetch_quota, "fetch_json", fake_json),
            patch.object(fetch_quota, "fetch_http", usage),
        ):
            first = fetch_quota.fetch_claude()
            second = fetch_quota.fetch_claude()

        usage.hit(self, fetch_quota.CLAUDE_URL)
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

        results: list[JsonDict] = []

        def run() -> None:
            results.append(fetch_quota.fetch_claude())

        usage = _http_returning(200, {"five_hour": {"utilization": 4}})
        with (
            patch.object(fetch_quota, "fetch_json", fake_json),
            patch.object(fetch_quota, "fetch_http", usage),
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
        self.assertEqual(len(usage.urls), 2, usage.urls)
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
                return 200, {
                    "token_endpoint": f"https://{fetch_quota.GROK_OIDC_HOST}/token"
                }
            if url == f"https://{fetch_quota.GROK_OIDC_HOST}/token":
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


class RefreshLockWaitTest(unittest.TestCase):
    """The wait for the refresh lock is driven by a seam, not by real time.

    The lock is the one place two runs of the fetcher contend, so a replay of
    a contended poll is only byte-for-byte if the poll interval and the
    deadline both come from injectable clocks. A deadline read through the
    pinned wall clock would never expire, and a sleep the suite cannot patch
    would burn real seconds.
    """

    class FakeFcntl:
        """A flock that reports the lock busy for a fixed number of tries."""

        LOCK_EX = 1
        LOCK_NB = 2
        LOCK_UN = 4

        def __init__(self, busy_tries: int) -> None:
            self.busy_tries = busy_tries
            self.tries = 0
            self.released: list[int] = []

        def flock(self, fd: int, operation: int) -> None:
            if operation == self.LOCK_UN:
                self.released.append(fd)
                return
            self.tries += 1
            if self.tries <= self.busy_tries:
                raise OSError(errno.EAGAIN, "resource temporarily unavailable")

    class UnlockableFcntl:
        """A flock on a filesystem that cannot take a lock at all."""

        LOCK_EX = 1
        LOCK_NB = 2
        LOCK_UN = 4

        def __init__(self) -> None:
            self.tries = 0
            self.released: list[int] = []

        def flock(self, fd: int, operation: int) -> None:
            if operation == self.LOCK_UN:
                self.released.append(fd)
                return
            self.tries += 1
            raise OSError(errno.ENOLCK, "no locks available")

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        point_config(self, "cache_dir", Path(tmp.name) / "cache")
        self.clock = [0.0]
        self.slept: list[float] = []

    def _monotonic(self) -> float:
        return self.clock[0]

    def _sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.clock[0] += seconds

    def _acquire(
        self, fake: RefreshLockWaitTest.FakeFcntl | RefreshLockWaitTest.UnlockableFcntl
    ) -> list[str]:
        """Run the lock body once, returning what it recorded while held."""
        held: list[str] = []
        with (
            patch.object(fetch_quota, "fcntl", fake),
            patch.object(fetch_quota, "monotonic", self._monotonic),
            patch.object(fetch_quota, "sleep", self._sleep),
            fetch_quota._refresh_lock(),
        ):
            held.append("body")
        return held

    def test_polls_at_the_interval_until_the_lock_frees(self) -> None:
        fake = self.FakeFcntl(busy_tries=3)

        self.assertEqual(self._acquire(fake), ["body"])
        self.assertEqual(fake.tries, 4)
        self.assertEqual(self.slept, [fetch_quota.REFRESH_LOCK_POLL_S] * 3)
        self.assertEqual(len(fake.released), 1)

    def test_deadline_expires_on_elapsed_time_not_the_pinned_clock(self) -> None:
        # A pinned wall clock never advances, so a deadline taken through
        # now_ms() would spin here forever. The wait ends on the elapsed seam.
        with config_env(**{fetch_quota.NOW_MS_ENV: str(PINNED_NOW_MS)}):
            fake = self.FakeFcntl(busy_tries=10**6)

            self.assertEqual(self._acquire(fake), ["body"])
        expected = int(
            fetch_quota.REFRESH_LOCK_WAIT_S / fetch_quota.REFRESH_LOCK_POLL_S
        )
        self.assertEqual(fake.tries, expected + 1)
        self.assertEqual(self.clock[0], fetch_quota.REFRESH_LOCK_WAIT_S)

    def test_a_lock_that_never_frees_still_refreshes(self) -> None:
        fake = self.FakeFcntl(busy_tries=10**6)

        with config_env(**{fetch_quota.NOW_MS_ENV: str(PINNED_NOW_MS)}):
            self.assertEqual(self._acquire(fake), ["body"])
        # Unguarded, not never: a holder that died must not block a poll.
        self.assertEqual(self.clock[0], fetch_quota.REFRESH_LOCK_WAIT_S)

    def test_a_lock_the_filesystem_cannot_take_is_not_waited_out(self) -> None:
        # ENOLCK is not contention, so waiting for the deadline would add the
        # full REFRESH_LOCK_WAIT_S to every refresh on such a filesystem.
        fake = self.UnlockableFcntl()

        self.assertEqual(self._acquire(fake), ["body"])
        self.assertEqual(fake.tries, 1)
        self.assertEqual(self.slept, [])
        # The lock was never taken, so releasing one would be a lie.
        self.assertEqual(fake.released, [])


class LazyConfigTest(unittest.TestCase):
    """config() is read from every provider thread, so it loads once.

    main() publishes the config before the pool starts, so a poll finds it
    without the lock. A caller that reaches a provider without that would
    otherwise have every thread build the module global at the same time.
    """

    THREADS = 8

    def test_concurrent_callers_share_one_load(self) -> None:
        barrier = threading.Barrier(self.THREADS, timeout=10)
        loads: list[fetch_quota.Config] = []
        seen: list[fetch_quota.Config] = []
        real = fetch_quota.load_config

        def counting_load(env: Mapping[str, str] | None = None) -> fetch_quota.Config:
            # Long enough that a thread which skipped the lock is still inside
            # load_config when the next one starts one.
            time.sleep(0.05)
            cfg = real(env)
            loads.append(cfg)
            return cfg

        def run() -> None:
            barrier.wait()
            seen.append(fetch_quota.config())

        fetch_quota._CONFIG = None
        self.addCleanup(fetch_quota.load_config)
        with patch.object(fetch_quota, "load_config", counting_load):
            threads = [threading.Thread(target=run) for _ in range(self.THREADS)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)

        self.assertEqual(len(seen), self.THREADS)
        self.assertEqual(len(loads), 1)
        self.assertTrue(all(cfg is seen[0] for cfg in seen))


class SilentWriteTest(unittest.TestCase):
    """A write that cannot land must reach the journal, not just the next poll."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = config_env(QUOTA_WIDGET_CACHE=self.tmp.name)
        env.__enter__()
        self.addCleanup(env.__exit__, None, None, None)

    def test_merge_write_reports_a_writer_that_keeps_winning(self) -> None:
        path = Path(self.tmp.name) / "auth.json"
        real_write = fetch_quota._atomic_write_json

        def racing_write(target: Path, obj: object) -> None:
            real_write(target, obj)
            # A concurrent writer replaces the value after every commit.
            real_write(target, {"tokens": "cli"})

        def put_tokens(store: dict[str, object]) -> tuple[str, object]:
            store["tokens"] = "widget"
            return "tokens", "widget"

        err = io.StringIO()
        with (
            patch.object(fetch_quota, "_atomic_write_json", racing_write),
            contextlib.redirect_stderr(err),
        ):
            fetch_quota._merge_write_json(path, put_tokens)

        logged = err.getvalue()
        self.assertIn(str(path), logged)
        self.assertIn("attempts", logged)

    def test_cache_write_failure_is_reported(self) -> None:
        def refusing_mkdir(*args: object, **kwargs: object) -> None:
            raise OSError("no space left on device")

        err = io.StringIO()
        with (
            patch.object(Path, "mkdir", refusing_mkdir),
            contextlib.redirect_stderr(err),
        ):
            fetch_quota._write_provider_cache(
                "claude", {"ok": True, "plan": "Max"}, "acct-1"
            )

        logged = err.getvalue()
        self.assertIn("claude", logged)
        self.assertIn("no space left on device", logged)


class CursorFailureReportingTest(unittest.TestCase):
    """A Cursor read that fails is not a signed-out session."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        auth_json = Path(self.tmp.name) / "auth.json"
        auth_json.write_text(json.dumps({"accessToken": _fake_jwt("auth0|user_01ERR")}))
        env = config_env(
            QUOTA_WIDGET_CACHE=self.tmp.name, QUOTA_WIDGET_CURSOR_AUTH=str(auth_json)
        )
        env.__enter__()
        self.addCleanup(env.__exit__, None, None, None)

    def _fetch_with_status(self, status: int) -> JsonDict:
        def fake_json(
            url: str,
            headers: dict[str, str],
            *,
            timeout: float = 12.0,
            data: bytes | None = None,
            method: str | None = None,
        ) -> tuple[int, object]:
            return status, None

        with patch.object(fetch_quota, "fetch_json", fake_json):
            return fetch_quota.fetch_cursor()

    def test_signed_out_still_reports_401(self) -> None:
        self.assertEqual(
            _scoped(self._fetch_with_status(401)),
            {"ok": False, "error": "http-401", "transient": False},
        )

    def test_forbidden_reports_its_own_status(self) -> None:
        # 401 renders as "Sign in to Cursor"; a 403 is an edge rejection, so
        # reporting it as 401 sends the user to re-authenticate for nothing.
        self.assertEqual(
            _scoped(self._fetch_with_status(403)),
            {"ok": False, "error": "http-403", "transient": False},
        )

    def test_an_unreadable_state_db_is_reported(self) -> None:
        db = Path(self.tmp.name) / "state.vscdb"
        db.write_bytes(b"not a sqlite database")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(fetch_quota._read_cursor_state_db(db))
        self.assertIn(str(db), err.getvalue())

    def test_a_locked_state_db_stays_quiet(self) -> None:
        db = Path(self.tmp.name) / "state.vscdb"
        db.write_bytes(b"not a sqlite database")
        err = io.StringIO()
        with (
            patch.object(
                sqlite3,
                "connect",
                side_effect=sqlite3.OperationalError("database is locked"),
            ),
            contextlib.redirect_stderr(err),
        ):
            self.assertIsNone(fetch_quota._read_cursor_state_db(db))
        self.assertEqual(err.getvalue(), "")


class HomeDirectoryTest(unittest.TestCase):
    """An unresolvable home is a config error, so the panel still gets JSON."""

    def setUp(self) -> None:
        self.addCleanup(fetch_quota.load_config)

    def test_no_home_directory_raises_config_error(self) -> None:
        with patch.object(Path, "home", side_effect=RuntimeError("no HOME")):
            with self.assertRaises(fetch_quota.ConfigError) as ctx:
                fetch_quota.load_config({})
        self.assertIn("home directory", str(ctx.exception))

    def test_main_emits_a_payload_when_home_is_unresolvable(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        with (
            patch.object(Path, "home", side_effect=RuntimeError("no HOME")),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            self.assertRaises(SystemExit) as ctx,
        ):
            fetch_quota.main([])
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(json.loads(out.getvalue())["error"], "config")
        self.assertIn("home directory", err.getvalue())


class OriginBoundRedirectTest(unittest.TestCase):
    """A 30x must not walk the user's token to whichever host it names.

    urllib copies the request headers onto the redirected request, so the
    default handler hands a Bearer token (or Cursor's session cookie) to
    whatever host a vendor endpoint answers 3xx with.
    """

    def _redirect(self, url: str, newurl: str, headers: dict[str, str]) -> object:
        # S310: a Request is built, never opened, and both URLs are literals
        # naming the origins the check is about.
        req = urllib.request.Request(url, headers=headers)  # noqa: S310 (never opened)
        return fetch_quota._OriginBoundRedirect().redirect_request(
            req,
            io.BytesIO(b""),
            302,
            "Found",
            http.client.HTTPMessage(),
            newurl,
        )

    def test_same_origin_redirect_is_followed(self) -> None:
        new = self._redirect(
            "https://api.anthropic.com/api/oauth/usage",
            "https://api.anthropic.com/api/oauth/usage?page=2",
            {"Authorization": "Bearer tok"},
        )
        self.assertEqual(
            getattr(new, "full_url", None),
            "https://api.anthropic.com/api/oauth/usage?page=2",
        )

    def test_cross_origin_redirect_is_refused(self) -> None:
        with self.assertRaises(urllib.error.HTTPError):
            self._redirect(
                "https://api.anthropic.com/api/oauth/usage",
                "https://attacker.test/collect",
                {"Authorization": "Bearer tok"},
            )

    def test_session_cookie_counts_as_a_credential(self) -> None:
        with self.assertRaises(urllib.error.HTTPError):
            self._redirect(
                "https://cursor.com/api/usage-summary",
                "https://attacker.test/collect",
                {"Cookie": "WorkosCursorSessionToken=tok"},
            )

    def test_scheme_downgrade_is_cross_origin(self) -> None:
        with self.assertRaises(urllib.error.HTTPError):
            self._redirect(
                "https://api.anthropic.com/api/oauth/usage",
                "http://api.anthropic.com/api/oauth/usage",
                {"Authorization": "Bearer tok"},
            )

    def test_credential_free_request_may_still_redirect(self) -> None:
        new = self._redirect(
            "https://api.anthropic.com/discovery", "https://cdn.anthropic.com/doc", {}
        )
        self.assertEqual(
            getattr(new, "full_url", None), "https://cdn.anthropic.com/doc"
        )

    def test_the_installed_opener_carries_the_handler(self) -> None:
        # urlopen() uses a global opener it builds itself; the refusal only
        # holds if the opener carrying the handler is the installed one.
        handlers = urllib.request.urlopen.__globals__["_opener"]
        self.assertTrue(
            any(
                isinstance(h, fetch_quota._OriginBoundRedirect)
                for h in handlers.handlers
            )
        )


class ResponseSizeCapTest(unittest.TestCase):
    """A body longer than the cap is not read into the panel's heap whole."""

    class _Resp:
        status = 200
        headers: object = None

        def __init__(self, payload: bytes) -> None:
            self._payload = payload
            self.requested: list[int] = []

        def read(self, size: int = -1) -> bytes:
            self.requested.append(size)
            return self._payload if size < 0 else self._payload[:size]

        def __enter__(self) -> ResponseSizeCapTest._Resp:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def test_oversized_body_is_refused_without_reading_it_all(self) -> None:
        resp = self._Resp(b'{"ok": true}' + b" " * fetch_quota.MAX_RESPONSE_BYTES)
        with (
            patch.object(urllib.request, "urlopen", lambda *a, **k: resp),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            status, body, _hdrs = fetch_quota.fetch_http("https://api.test/usage", {})

        self.assertEqual(status, 200)
        self.assertIsNone(body)
        self.assertEqual(
            resp.requested, [fetch_quota.MAX_RESPONSE_BYTES + 1], "read past the cap"
        )

    def test_a_body_under_the_cap_still_parses(self) -> None:
        resp = self._Resp(b'{"ok": true}')
        with patch.object(urllib.request, "urlopen", lambda *a, **k: resp):
            status, body, _hdrs = fetch_quota.fetch_http("https://api.test/usage", {})

        self.assertEqual((status, body), (200, {"ok": True}))

    def test_the_discarded_error_body_is_capped_too(self) -> None:
        # A 429 or a 5xx is the response a peer picks at length, and this body
        # is drained and thrown away: reading it whole is the same heap cost.
        class _Big(io.BytesIO):
            def __init__(self) -> None:
                super().__init__(b"x" * 4096)
                self.requested: list[int] = []

            def read(self, size: int | None = -1) -> bytes:
                self.requested.append(-1 if size is None else size)
                return super().read(size)

        body = _Big()
        error = urllib.error.HTTPError(
            "https://api.test/usage",
            429,
            "Too Many Requests",
            email.message.Message(),
            body,
        )
        with (
            patch.object(urllib.request, "urlopen", side_effect=error),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            status, data, _hdrs = fetch_quota.fetch_http("https://api.test/usage", {})

        self.assertEqual(status, 429)
        self.assertIsNone(data)
        self.assertEqual(body.requested, [fetch_quota.MAX_RESPONSE_BYTES + 1])


class CacheDirModeTest(unittest.TestCase):
    """The cache holds readings and account digests; the folder is private."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "cache"

    def test_a_wide_existing_folder_is_closed_up(self) -> None:
        self.folder.mkdir(mode=0o755)
        self.folder.chmod(0o755)

        fetch_quota._private_dir(self.folder)

        self.assertEqual(self.folder.stat().st_mode & 0o777, 0o700)

    def test_a_private_folder_is_left_alone(self) -> None:
        self.folder.mkdir(mode=0o700)

        fetch_quota._private_dir(self.folder)

        self.assertEqual(self.folder.stat().st_mode & 0o777, 0o700)

    def test_the_written_cache_is_not_readable_by_others(self) -> None:
        point_config(self, "cache_dir", self.folder)
        fetch_quota._write_provider_cache(
            "grok",
            {"ok": True, "plan": "Grok"},
            fetch_quota._account_id(_fake_jwt("user_01GROK")),
        )
        self.assertEqual(self.folder.stat().st_mode & 0o777, 0o700)
        entry = self.folder / "grok.json"
        self.assertEqual(entry.stat().st_mode & 0o777, 0o600)


class GrokTokenEndpointTest(unittest.TestCase):
    """The refresh token goes to an endpoint the discovery document named, so
    that endpoint has to be the vendor's own over https."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        past = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
        self.auth = Path(self.tmp.name) / "grok-auth.json"
        self.auth.write_text(
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
        point_config(self, "grok_auth", self.auth)

    def _discovery(self, token_endpoint: object) -> list[str]:
        posted: list[str] = []

        def fake_json(
            url: str,
            headers: dict[str, str],
            *,
            timeout: float = 12.0,
            data: bytes | None = None,
            method: str | None = None,
        ) -> tuple[int, object]:
            if url == fetch_quota.GROK_OIDC_DISCOVERY:
                return 200, {"token_endpoint": token_endpoint}
            if data is not None:
                posted.append(url)
            return 200, {"error": "not reached"}

        with (
            patch.object(fetch_quota, "fetch_json", fake_json),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertIsNone(fetch_quota._refresh_grok("cli::client-1", {}))
        return posted

    def test_a_cleartext_endpoint_is_refused(self) -> None:
        self.assertEqual(self._discovery("http://auth.x.ai/token"), [])

    def test_another_host_is_refused(self) -> None:
        self.assertEqual(self._discovery("https://attacker.test/token"), [])

    def test_a_host_lookalike_is_refused(self) -> None:
        self.assertEqual(self._discovery("https://auth.x.ai.evil.test/token"), [])

    def test_a_non_url_is_refused(self) -> None:
        self.assertEqual(self._discovery(["https://auth.x.ai/token"]), [])

    def test_the_vendor_endpoint_is_accepted(self) -> None:
        self.assertTrue(
            fetch_quota._is_grok_token_url(
                f"https://{fetch_quota.GROK_OIDC_HOST}/oauth/token"
            )
        )


if __name__ == "__main__":
    unittest.main()
