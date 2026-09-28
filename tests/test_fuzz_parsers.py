"""Randomized (property) fuzzing for the parsers fed untrusted input.

Every surface that carries bytes or JSON this process does not control:

- ``parse_cursor_summary`` reads the Cursor usage-summary body off the wire
  and out of ``~/.cache/quota-widget/cursor.json``.
- ``_parse_grok_period`` reads the Grok billing config off the wire and out of
  the same cache.
- ``_claude_weekly`` and ``_claude_session`` read the Claude usage body, whose
  ``limits`` array is the one place a provider sends a list of unbounded
  length and a variable shape.
- ``_codex_window`` and ``_codex_reset_credits`` read the Codex usage body,
  where the window's numbers are rescaled into an instant before they reach
  the panel.
- ``parse_retry_after`` reads the Retry-After header, which reaches sleep().
- ``_vscdb_str`` and ``_jwt_payload`` read cells and tokens out of a Cursor
  SQLite state DB and a vendor credential file.
- ``_write_provider_cache`` and ``_read_provider_cache`` round-trip readings
  through disk.

All of them run inside a plasmashell poll, so a raise is a dead widget until
the next reload. The generators are seeded, so a failure reproduces from the
printed seed. A fuzzer only proves the presence of a bug; the assertions
below are the invariant half, and they turn a wrong answer into a failure
the generator can see.
"""

from __future__ import annotations

import base64
import contextlib
import json
import math
import os
import random
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import fetch_quota

JsonDict = dict[str, Any]

ITERATIONS = 2000
BASE_SEED = 20260928
# The cache harness writes a real file, and every write is fsynced, so it
# runs a shorter pass than the in-memory parsers.
CACHE_ITERATIONS = 150

# Shapes the vendors actually send, plus the ones a broken or hostile
# response would send. Generation starts from these so a short run still
# reaches the meter branches instead of spending every case on `{}`.
SEED_CORPUS: tuple[JsonDict, ...] = (
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
        "teamUsage": {"onDemand": {"enabled": True, "used": 12, "limit": 500}},
    },
    {
        "membershipType": "enterprise",
        "isUnlimited": True,
        "individualUsage": {"overall": {"enabled": True, "used": 195813}},
    },
    {"membershipType": "ultra", "individualUsage": {}},
    {},
    # Wrong types where the schema says string/bool/number.
    {"membershipType": ["pro"], "billingCycleEnd": 1743691915000, "isUnlimited": 1},
    {"individualUsage": {"plan": {"used": "200", "limit": None, "enabled": "yes"}}},
    {
        "individualUsage": {
            "plan": {"used": float("inf"), "totalPercentUsed": float("nan")}
        }
    },
    {"individualUsage": {"plan": {"used": 1e308, "limit": 1e-308}}},
    {"individualUsage": {"plan": {"used": 5, "limit": 0, "totalPercentUsed": None}}},
)

# Leaf values that break a typed reader: wrong types, non-finite numbers,
# control characters, lone surrogates, and deep nesting.
HOSTILE_LEAVES: tuple[Any, ...] = (
    None,
    True,
    False,
    0,
    -1,
    1e400,
    float("nan"),
    float("inf"),
    float("-inf"),
    1 << 62,
    -(1 << 62),
    0.1,
    "",
    "0",
    "nan",
    "pro",
    "PRO_PLUS",
    "auth0|user_01",
    "2026-05-02T14:11:55.000Z",
    "2026-05-02 14:11:55",
    "Z",
    "\x00\x1f",
    "𝕌\U0001f600",
    "ü" * 64,
    [],
    {},
    [1, 2, 3],
    {"nested": {"deep": [None, {"x": float("nan")}]}},
)

# ItemTable cells and credential tokens as they arrive on disk.
CELL_SEEDS: tuple[Any, ...] = (
    b'"keyring::cursorAuth/accessToken"',
    b'"eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhdXRoMHx1c2VyXzAxIn0.sig"',
    b"raw-token",
    b'"\\ud800"',
    b"\xff\xfe not utf-8",
    b'""',
    b'{"not": "a string"}',
    b'["a"]',
    b'"unterminated',
    None,
    "",
    42,
    [],
    {},
)


def _b64url(payload: bytes) -> str:
    """One JWT segment, so a claim a generator cannot reach by chance is
    reachable on purpose."""
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


# Timestamps as the vendors write them, plus the ones a broken value holds.
TIMESTAMP_SEEDS: tuple[Any, ...] = (
    "2026-05-02T14:11:55.000Z",
    "2026-05-02T14:11:55Z",
    "2026-05-02T14:11:55",
    "2026-05-02 14:11:55",
    "2026-05-02T14:11:55+02:00",
    "2026-05-02T14:11:55.123456789Z",
    "  2026-05-02T14:11:55Z  ",
    "0001-01-01T00:00:00Z",
    "9999-12-31T23:59:59.999999Z",
    "2026-13-45T99:99:99Z",
    "2026-02-30T00:00:00Z",
    "Z",
    "+00:00",
    "1970-01-01T00:00:00.000+00:00",
    "",
    "not a date",
    "1e999",
    "\x00",
    "2026-05-02T14:11:55Z\u00a0",
    None,
    1_743_691_915_000,
    0,
    1e308,
    ["2026-05-02T14:11:55Z"],
    {"start": "2026-05-02T14:11:55Z"},
    True,
)

# Retry-After header values: delta-seconds, HTTP-date, and neither.
RETRY_AFTER_SEEDS: tuple[Any, ...] = (
    "0",
    "120",
    "  5  ",
    "-10",
    "1e999",
    "nan",
    "inf",
    "Wed, 02 May 2026 14:11:55 GMT",
    "Wednesday, 02-May-26 14:11:55 GMT",
    "Wed, 99 Xxx 2026 99:99:99 GMT",
    "",
    "   ",
    "\x00",
    None,
    5,
    ["5"],
)


def _rand_json(rng: random.Random, depth: int = 0) -> Any:
    """A JSON value with the shape of a usage-summary body, roughly."""
    if depth >= 3 or rng.random() < 0.25:
        return rng.choice(HOSTILE_LEAVES)
    if rng.random() < 0.5:
        return [_rand_json(rng, depth + 1) for _ in range(rng.randrange(4))]
    return {str(k): _rand_json(rng, depth + 1) for k in range(rng.randrange(5))}


def _rand_summary(rng: random.Random) -> Any:
    base = json.loads(json.dumps(rng.choice(SEED_CORPUS)))
    if not isinstance(base, dict):  # pragma: no cover - corpus is dicts
        return _rand_json(rng)
    # Overwrite each top-level key with a random value now and then, so the
    # schema-following path and the wrong-type path both run.
    for key in list(base) + rng.sample(
        [
            "membershipType",
            "billingCycleEnd",
            "isUnlimited",
            "individualUsage",
            "teamUsage",
            "limitType",
        ],
        k=rng.randrange(4),
    ):
        if rng.random() < 0.5:
            base[key] = _rand_json(rng)
    if rng.random() < 0.2:
        base["extra_unknown_key"] = _rand_json(rng)
    return base




def _rand_cell(rng: random.Random) -> Any:
    if rng.random() < 0.3:
        return rng.choice(CELL_SEEDS)
    kind = rng.randrange(4)
    raw = bytes(rng.randrange(256) for _ in range(rng.randrange(48)))
    if kind == 0:
        return raw
    text = raw.decode("latin-1")
    if kind == 1:
        return f'"{text}"'
    if kind == 2:
        return f'"\\u{raw.hex()}"'
    return text


def _assert_serializable(case: unittest.TestCase, obj: Any) -> None:
    """The panel parses this with JSON; a NaN or Infinity blanks the widget."""
    json.loads(json.dumps(obj, allow_nan=False))


@contextlib.contextmanager
def _cache_dir(path: Path) -> Iterator[None]:
    """Point the fetcher's cache at `path` for the duration of a test."""
    saved = os.environ.get("QUOTA_WIDGET_CACHE")
    os.environ["QUOTA_WIDGET_CACHE"] = str(path)
    fetch_quota.load_config()
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("QUOTA_WIDGET_CACHE", None)
        else:
            os.environ["QUOTA_WIDGET_CACHE"] = saved
        fetch_quota.load_config()


class CursorSummaryFuzz(unittest.TestCase):
    """Invariants every usage-summary body must satisfy, whatever it holds."""

    def _check(self, data: Any) -> JsonDict:
        parsed = fetch_quota.parse_cursor_summary(data)
        self.assertIs(parsed["ok"], True)
        self.assertIsInstance(parsed["plan"], str)
        self.assertTrue(parsed["plan"])
        self.assertIsInstance(parsed["unlimited"], bool)
        self.assertIsInstance(parsed["periods"], list)
        for period in parsed["periods"]:
            self.assertIsInstance(period["label"], str)
            self.assertTrue(period["label"])
            self.assertIn(period["unit"], ("count", "cents", "percent"))
            for key in ("util", "used", "limit", "resets_ms"):
                value = period[key]
                if isinstance(value, float):
                    self.assertTrue(math.isfinite(value), f"{key}={value!r} not finite")
        _assert_serializable(self, parsed)
        return parsed

    def test_fuzz_usage_summary(self) -> None:
        for iteration in range(ITERATIONS):
            rng = random.Random(BASE_SEED + iteration)
            data = _rand_summary(rng)
            with self.subTest(iteration=iteration, seed=BASE_SEED + iteration):
                self._check(data)

    def test_known_shapes_parse_as_before(self) -> None:
        """The corpus entries the widget depends on keep their meaning."""
        first = self._check(json.loads(json.dumps(SEED_CORPUS[0])))
        by_label = {p["label"]: p for p in first["periods"]}
        self.assertEqual(first["plan"], "Pro")
        self.assertEqual(by_label["Included"]["util"], 40)
        self.assertEqual(by_label["Auto + Composer"]["util"], 10)
        self.assertEqual(by_label["API"]["util"], 40)
        self.assertEqual(by_label["On-demand"]["util"], 23.1)
        self.assertEqual(by_label["Team on-demand"]["util"], 2.4)
        self.assertEqual(first["resets_ms"], 1777731115000)
        unlimited = self._check(json.loads(json.dumps(SEED_CORPUS[1])))
        self.assertEqual(unlimited["periods"], [])
        self.assertEqual(unlimited["plan"], "Enterprise")
        self.assertIs(unlimited["unlimited"], True)

    def test_wrong_typed_membership_and_cycle_end_are_survivable(self) -> None:
        parsed = self._check(
            {"membershipType": ["pro"], "billingCycleEnd": 1743691915000}
        )
        self.assertIsInstance(parsed["plan"], str)
        # A bare epoch is an instant like any other: reading only the string
        # form dropped the cycle end of every response that sent a number.
        self.assertEqual(parsed["resets_ms"], 1743691915000)

    def test_unreadable_cycle_end_is_absent_not_a_wrong_date(self) -> None:
        for value in (True, [1743691915], {"at": 1743691915}, "not a date", ""):
            with self.subTest(value=value):
                parsed = self._check({"billingCycleEnd": value})
                self.assertIsNone(parsed["resets_ms"])


GROK_SEED_CORPUS: tuple[JsonDict, ...] = (
    {
        "used": 2500,
        "monthlyLimit": 10000,
        "onDemandCap": 5000,
        "billingPeriodStart": "2026-04-02T14:11:55.000Z",
        "billingPeriodEnd": "2026-05-02T14:11:55.000Z",
    },
    {
        "currentPeriod": {"type": "WEEKLY", "start": "2026-04-02T00:00:00Z"},
        "creditUsagePercent": 12.5,
        "isUnifiedBillingUser": True,
    },
    {"isUnifiedBillingUser": True, "currentPeriod": {"type": "MONTHLY"}},
    {},
    # Wrong types where the schema says number or string.
    {"used": ["2500"], "monthlyLimit": None},
    {"used": 0, "monthlyLimit": 0, "onDemandCap": 0},
    {"used": 2500, "monthly_limit": 10000, "on_demand_cap": 5000},
    {"used": 1.7e308, "monthlyLimit": 1, "onDemandCap": float("inf")},
    {"used": float("nan"), "monthlyLimit": float("nan"), "creditUsagePercent": "x"},
)


def _rand_grok_period(rng: random.Random) -> Any:
    base = json.loads(json.dumps(rng.choice(GROK_SEED_CORPUS)))
    for key in list(base) + rng.sample(
        [
            "used",
            "monthlyLimit",
            "monthly_limit",
            "onDemandCap",
            "on_demand_cap",
            "creditUsagePercent",
            "currentPeriod",
        ],
        k=rng.randrange(5),
    ):
        if rng.random() < 0.5:
            base[key] = _rand_json(rng)
    return base


class GrokPeriodFuzz(unittest.TestCase):
    """Invariants every Grok billing config must satisfy.

    The credits and dollar shapes share one period parser, and its util is a
    ratio of two wire numbers, so the values that can reach it are the ones a
    response can invent: zero, negative, and non-finite.
    """

    def _check(self, data: Any) -> JsonDict:
        parsed = fetch_quota._parse_grok_period(data)
        self.assertIn(parsed["label"], ("Weekly", "Monthly", "Usage"))
        self.assertEqual(parsed["unit"], "cents")
        self.assertIsInstance(parsed["currency"], str)
        for key in ("util", "used", "limit", "on_demand_cap"):
            value = parsed[key]
            if isinstance(value, float):
                self.assertTrue(math.isfinite(value), f"{key}={value!r} not finite")
        if parsed["used"] is not None and parsed["limit"] is not None:
            if parsed["limit"] <= 0:
                self.assertIsNone(parsed["util"])
            elif parsed["util"] is not None:
                self.assertEqual(
                    parsed["util"],
                    round(100.0 * parsed["used"] / parsed["limit"], 1),
                )
        # The panel parses this with JSON; a NaN or Infinity blanks the widget.
        json.loads(json.dumps(parsed, allow_nan=False))
        return parsed

    def test_fuzz_billing_config(self) -> None:
        for iteration in range(ITERATIONS):
            rng = random.Random(BASE_SEED + 30_000 + iteration)
            with self.subTest(iteration=iteration, seed=BASE_SEED + 30_000 + iteration):
                self._check(_rand_grok_period(rng))

    def test_known_shapes_parse_as_before(self) -> None:
        dollars = self._check(json.loads(json.dumps(GROK_SEED_CORPUS[0])))
        self.assertEqual(dollars["label"], "Monthly")
        self.assertEqual(dollars["util"], 25.0)
        self.assertEqual(dollars["used"], 2500)
        self.assertEqual(dollars["limit"], 10000)
        self.assertEqual(dollars["on_demand_cap"], 5000)
        weekly = self._check(json.loads(json.dumps(GROK_SEED_CORPUS[1])))
        self.assertEqual(weekly["label"], "Weekly")
        self.assertEqual(weekly["util"], 12.5)
        self.assertEqual(weekly["on_demand_cap"], None)
        empty = self._check(json.loads(json.dumps(GROK_SEED_CORPUS[2])))
        self.assertEqual(empty["util"], 0.0)


class TokenAndCellFuzz(unittest.TestCase):
    """Invariants for the on-disk token and SQLite cell readers."""

    def test_fuzz_vscdb_cell(self) -> None:
        for iteration in range(ITERATIONS):
            rng = random.Random(BASE_SEED + 10_000 + iteration)
            cell = _rand_cell(rng)
            with self.subTest(iteration=iteration, seed=BASE_SEED + 10_000 + iteration):
                value = fetch_quota._vscdb_str(cell)
                if value is None:
                    continue
                self.assertIsInstance(value, str)
                self.assertEqual(value, value.strip())
                # The cell becomes an Authorization header and a cache entry.
                value.encode("utf-8")
                json.dumps(value)

    def test_fuzz_jwt_token(self) -> None:
        for iteration in range(ITERATIONS):
            rng = random.Random(BASE_SEED + 20_000 + iteration)
            token = _rand_cell(rng)
            if not isinstance(token, str):
                token = str(token)
            with self.subTest(iteration=iteration, seed=BASE_SEED + 20_000 + iteration):
                payload = fetch_quota._jwt_payload(token)
                self.assertTrue(payload is None or isinstance(payload, dict))
                sub = fetch_quota._jwt_sub(token)
                self.assertTrue(sub is None or isinstance(sub, str))
                # The sub is percent-encoded into the session cookie, so a
                # claim that decodes but does not encode must be dropped
                # before it reaches the header.
                self.assertTrue(sub is None or fetch_quota._utf8_encodable(sub))
                # A token that claims an expiry must produce a usable instant.
                exp = fetch_quota._jwt_exp_ms(token)
                self.assertTrue(exp is None or isinstance(exp, int))
                # The account id digests the sub claim, which a "\ud800"
                # escape spells as a lone surrogate: no id, never a raise.
                account = fetch_quota._account_id(token)
                self.assertTrue(
                    account is None
                    or (
                        len(account) == 16
                        and all(c in "0123456789abcdef" for c in account)
                    )
                )

    def test_known_token_shapes(self) -> None:
        header = fetch_quota._jwt_payload("h." + "aGVsbG8" + ".s")
        self.assertIsNone(header)  # not a JSON object
        claims = fetch_quota._jwt_payload("h.eyJzdWIiOiJhdXRoMHx1c2VyXzAxIn0.s")
        self.assertEqual(claims, {"sub": "auth0|user_01"})
        self.assertEqual(
            fetch_quota._jwt_sub("h.eyJzdWIiOiJhdXRoMHx1c2VyXzAxIn0.s"), "user_01"
        )
        self.assertIsNone(fetch_quota._jwt_payload("no-dot-token"))
        self.assertEqual(fetch_quota._vscdb_str('"Ünïcodé"'), "Ünïcodé")
        self.assertIsNone(fetch_quota._vscdb_str(b"\xff\xfe not utf-8"))
        # An exp the widget cannot place on a date is absent, not a raise:
        # the millisecond product of 1e308 s overflows a double.
        self.assertIsNone(
            fetch_quota._jwt_exp_ms("h." + _b64url(b'{"exp":1e308}') + ".s")
        )
        self.assertEqual(
            fetch_quota._jwt_exp_ms("h." + _b64url(b'{"exp":1778000000}') + ".s"),
            1_778_000_000_000,
        )


CLAUDE_SEED_CORPUS: tuple[JsonDict, ...] = (
    {
        "five_hour": {"utilization": 42, "resets_at": "2026-05-02T14:11:55.000Z"},
        "limits": [
            {
                "kind": "weekly_all",
                "percent": 37,
                "resets_at": "2026-05-09T14:11:55.000Z",
                "scope": {"surface": "Claude Code"},
            },
            {
                "kind": "weekly_model",
                "percent": 12,
                "resets_at": 1778401915000,
                "scope": {"model": {"display_name": "Opus"}},
            },
            {
                "kind": "session",
                "percent": 8,
                "resets_at": "2026-05-02T16:00:00Z",
            },
        ],
        "extra_usage": {
            "is_enabled": True,
            "used_credits": 3.5,
            "currency": "USD",
            "monthly_limit": 100,
        },
        "spend": {"used": {"amount_minor": 1250, "exponent": 2}},
    },
    # The legacy shape, before `limits` existed.
    {
        "five_hour": {"utilization": 5, "resets_at": 1778000000000},
        "seven_day": {"utilization": 61, "resets_at": "2026-05-09T00:00:00Z"},
        "seven_day_opus": {"utilization": 20},
        "seven_day_sonnet": {"utilization": None},
        "seven_day_cowork": {"utilization": "not a number"},
    },
    # `limits` empty falls back to the legacy keys; a non-list `limits` is
    # read as absent, so the session falls through to `five_hour`.
    {"limits": [], "five_hour": {"utilization": 3}},
    {"limits": {"kind": "weekly_all"}, "five_hour": {"utilization": 3}},
    {"limits": ["not a dict", None, 7], "five_hour": {"utilization": 3}},
    {"limits": [{"group": "session", "percent": float("inf")}], "five_hour": {}},
    {"limits": [{"kind": "weekly_all", "percent": 1e308, "resets_at": 1e308}]},
    # More entries than MAX_WEEKLY_LIMITS, so the cap has to be the reason a
    # label is missing rather than the parser losing one.
    {
        "limits": [
            {
                "kind": "weekly_model",
                "percent": i,
                "scope": {"model": {"display_name": f"m{i}"}},
            }
            for i in range(40)
        ]
    },
    {},
)


def _rand_claude(rng: random.Random) -> Any:
    base = json.loads(json.dumps(rng.choice(CLAUDE_SEED_CORPUS)))
    if not isinstance(base, dict):  # pragma: no cover - corpus is dicts
        return _rand_json(rng)
    for key in list(base) + rng.sample(
        ["five_hour", "limits", "seven_day", "extra_usage", "spend"],
        k=rng.randrange(4),
    ):
        if rng.random() < 0.5:
            base[key] = _rand_json(rng)
    if isinstance(base.get("limits"), list) and rng.random() < 0.5:
        base["limits"] = [_rand_json(rng) for _ in range(rng.randrange(8))] + base[
            "limits"
        ]
    return base


def _check_period(period: JsonDict, where: str) -> None:
    self_label = period["label"]
    if not isinstance(self_label, str) or not self_label:
        raise AssertionError(f"{where}: label {self_label!r} is not a name")
    util = period["util"]
    if util is not None and not math.isfinite(util):
        raise AssertionError(f"{where}: util {util!r} not finite")
    resets = period["resets_ms"]
    if resets is not None and not isinstance(resets, int):
        raise AssertionError(f"{where}: resets_ms {resets!r} is not an instant")


class ClaudeUsageFuzz(unittest.TestCase):
    """Invariants the Claude usage body must satisfy, whatever it holds.

    The `limits` array is a list of variable-shape objects the vendor can grow
    without bound, and both the weekly meters and the session read it.
    """

    def _check(self, data: Any) -> tuple[list[JsonDict], tuple[Any, Any]]:
        parsed = data if isinstance(data, dict) else {}
        weekly = fetch_quota._claude_weekly(parsed)
        self.assertIsInstance(weekly, list)
        self.assertLessEqual(len(weekly), fetch_quota.MAX_WEEKLY_LIMITS)
        for period in weekly:
            _check_period(period, "weekly")
        util, resets = fetch_quota._claude_session(parsed)
        if util is not None:
            self.assertTrue(math.isfinite(util), f"session util {util!r} not finite")
        if resets is not None:
            self.assertIsInstance(resets, int)
        # The panel parses this with JSON; a NaN or Infinity blanks the widget.
        json.dumps({"weekly": weekly, "session": [util, resets]}, allow_nan=False)
        return weekly, (util, resets)

    def test_fuzz_usage_body(self) -> None:
        for iteration in range(ITERATIONS):
            rng = random.Random(BASE_SEED + 40_000 + iteration)
            with self.subTest(iteration=iteration, seed=BASE_SEED + 40_000 + iteration):
                self._check(_rand_claude(rng))

    def test_known_shapes_parse_as_before(self) -> None:
        weekly, (util, resets) = self._check(
            json.loads(json.dumps(CLAUDE_SEED_CORPUS[0]))
        )
        by_label = {p["label"]: p for p in weekly}
        self.assertEqual(by_label["All models"]["util"], 37)
        self.assertEqual(by_label["Opus"]["util"], 12)
        self.assertEqual(by_label["Opus"]["resets_ms"], 1778401915000)
        self.assertEqual(util, 8)
        self.assertEqual(resets, fetch_quota.iso_to_ms("2026-05-02T16:00:00Z"))
        legacy, (legacy_util, _) = self._check(
            json.loads(json.dumps(CLAUDE_SEED_CORPUS[1]))
        )
        self.assertEqual(
            [p["label"] for p in legacy], ["All models", "Opus", "Sonnet", "Cowork"]
        )
        # A present block with an unreadable percentage is a meter with no
        # reading, not a missing one.
        self.assertIsNone(legacy[3]["util"])
        self.assertEqual(legacy_util, 5)

    def test_limits_array_is_capped(self) -> None:
        weekly, _ = self._check(json.loads(json.dumps(CLAUDE_SEED_CORPUS[7])))
        self.assertEqual(len(weekly), fetch_quota.MAX_WEEKLY_LIMITS)

    def test_empty_limits_falls_back_to_legacy_keys(self) -> None:
        weekly, (util, _) = self._check(json.loads(json.dumps(CLAUDE_SEED_CORPUS[2])))
        self.assertEqual(weekly, [])
        self.assertEqual(util, 3.0)


CODEX_SEED_CORPUS: tuple[JsonDict, ...] = (
    {
        "plan_type": "pro_plus",
        "rate_limit": {
            "allowed": True,
            "limit_reached": False,
            "primary_window": {
                "used_percent": 12.5,
                "limit_window_seconds": 18_000,
                "reset_at": 1_778_000_000,
            },
            "secondary_window": {
                "used_percent": 61,
                "limit_window_seconds": 604_800,
                "reset_after_seconds": 86_400,
            },
        },
        "rate_limit_reset_credits": {
            "available_count": 0,
            "applicable_available_count": 2,
        },
        "credits": {"has_credits": True, "balance": "12.34", "unlimited": False},
    },
    # A window that is a string percentage, and one that is not a number.
    {
        "rate_limit": {
            "primary_window": {
                "used_percent": "  42.5 ",
                "limit_window_seconds": 172_800,
            },
            "secondary_window": {
                "used_percent": "nan",
                "limit_window_seconds": 604_800,
            },
        }
    },
    # Out-of-range and non-finite percentages: clamped, or no reading at all.
    {
        "rate_limit": {
            "primary_window": {"used_percent": 1e308, "limit_window_seconds": 3600},
            "secondary_window": {"used_percent": -5, "limit_window_seconds": 2_592_000},
        }
    },
    # A reset so large the millisecond product overflows a double.
    {
        "rate_limit": {
            "primary_window": {"used_percent": 10, "reset_at": 1e308},
            "secondary_window": {"used_percent": 10, "reset_after_seconds": 1e308},
        }
    },
    {"rate_limit": {"primary_window": None, "secondary_window": "not a dict"}},
    {"rate_limit": {"primary_window": {"used_percent": None}}},
    {"rate_limit": {"primary_window": {"used_percent": float("nan")}}},
    # A reported zero balance, and one reported with nothing in it.
    {"rate_limit_reset_credits": {}},
    {"rate_limit_reset_credits": {"available_count": float("inf")}},
    {},
)


def _rand_codex(rng: random.Random) -> Any:
    base = json.loads(json.dumps(rng.choice(CODEX_SEED_CORPUS)))
    if not isinstance(base, dict):  # pragma: no cover - corpus is dicts
        return _rand_json(rng)
    for key in list(base) + rng.sample(
        ["rate_limit", "primary_window", "secondary_window", "code_review_rate_limit"],
        k=rng.randrange(4),
    ):
        if rng.random() < 0.5:
            base[key] = _rand_json(rng)
    return base


def _rand_window(rng: random.Random) -> Any:
    if rng.random() < 0.2:
        return rng.choice((None, "not a dict", [], 7))
    if rng.random() < 0.4:
        return _rand_json(rng)
    block: JsonDict = {}
    for key in (
        "used_percent",
        "limit_window_seconds",
        "reset_at",
        "reset_after_seconds",
        "extra",
    ):
        if rng.random() < 0.6:
            block[key] = rng.choice(HOSTILE_LEAVES)
    return block


class CodexWindowFuzz(unittest.TestCase):
    """Invariants every Codex rate-limit window must satisfy.

    A window is a handful of wire numbers that get rescaled: the percentage is
    clamped into 0..100, and the reset is turned into an instant. Both are
    places a value the panel's JSON parser rejects, or a raise, can come from.
    """

    def _check(self, block: Any, name: str) -> JsonDict | None:
        window = fetch_quota._codex_window(block, name)
        if window is None:
            return None
        label = window["label"]
        if not isinstance(label, str) or not label:
            raise AssertionError(f"{name}: label {label!r} is not a name")
        util = window["util"]
        if not isinstance(util, float) or not math.isfinite(util):
            raise AssertionError(f"{name}: util {util!r} is not a finite float")
        if not 0.0 <= util <= 100.0:
            raise AssertionError(f"{name}: util {util!r} outside 0..100")
        resets = window["resets_ms"]
        if resets is not None and not isinstance(resets, int):
            raise AssertionError(f"{name}: resets_ms {resets!r} is not an instant")
        # The panel parses this with JSON; a NaN or Infinity blanks the widget.
        json.dumps(window, allow_nan=False)
        return window

    def _one(self, block: Any, name: str) -> JsonDict:
        """The window a case that must produce one produces."""
        window = self._check(block, name)
        self.assertIsNotNone(window, f"{name} produced no window for {block!r}")
        assert window is not None
        return window

    def test_fuzz_window(self) -> None:
        for iteration in range(ITERATIONS):
            rng = random.Random(BASE_SEED + 50_000 + iteration)
            block = _rand_window(rng)
            with self.subTest(iteration=iteration, seed=BASE_SEED + 50_000 + iteration):
                self._check(block, "primary_window")

    def test_fuzz_usage_body(self) -> None:
        for iteration in range(ITERATIONS):
            rng = random.Random(BASE_SEED + 60_000 + iteration)
            with self.subTest(iteration=iteration, seed=BASE_SEED + 60_000 + iteration):
                self._check(_rand_codex(rng), "code_review")

    def test_known_shapes_parse_as_before(self) -> None:
        body = json.loads(json.dumps(CODEX_SEED_CORPUS[0]))
        primary = self._one(body["rate_limit"]["primary_window"], "primary_window")
        # A 5-hour window is the current session; the label is derived, so a
        # different mapping has to fail here rather than on a user's panel.
        self.assertEqual(primary["label"], "Current session")
        self.assertEqual(primary["util"], 12.5)
        self.assertEqual(primary["resets_ms"], 1_778_000_000_000)
        secondary = self._one(
            body["rate_limit"]["secondary_window"], "secondary_window"
        )
        self.assertEqual(secondary["label"], "Weekly")
        self.assertEqual(secondary["util"], 61.0)
        # A string percentage the panel must still be able to draw.
        self.assertEqual(
            self._one({"used_percent": "  42.5 "}, "primary_window")["util"], 42.5
        )

    def test_out_of_range_percentages_are_clamped_or_absent(self) -> None:
        self.assertEqual(
            self._one({"used_percent": 1e308}, "primary_window")["util"], 100.0
        )
        self.assertEqual(self._one({"used_percent": -5}, "primary_window")["util"], 0.0)
        # NaN is no reading: not a clamped zero and not a full 100%.
        self.assertIsNone(self._check({"used_percent": float("nan")}, "primary_window"))
        self.assertIsNone(self._check({"used_percent": "nan"}, "primary_window"))
        self.assertIsNone(self._check({"used_percent": None}, "primary_window"))
        self.assertIsNone(self._check({"used_percent": True}, "primary_window"))
        self.assertIsNone(self._check("not a dict", "primary_window"))

    def test_out_of_range_reset_is_an_absent_date(self) -> None:
        # 1e308 s overflows the millisecond product, and round() raises on the
        # infinity it leaves: a raise here took the whole poll down.
        window = self._one({"used_percent": 10, "reset_at": 1e308}, "primary_window")
        self.assertIsNone(window["resets_ms"])
        window = self._one(
            {"used_percent": 10, "reset_after_seconds": 1e308}, "primary_window"
        )
        self.assertIsNone(window["resets_ms"])
        self.assertEqual(
            self._one({"used_percent": 10, "reset_at": 0}, "primary_window")[
                "resets_ms"
            ],
            0,
        )

    def test_fuzz_reset_credits(self) -> None:
        for iteration in range(ITERATIONS):
            rng = random.Random(BASE_SEED + 70_000 + iteration)
            data = _rand_codex(rng)
            with self.subTest(iteration=iteration, seed=BASE_SEED + 70_000 + iteration):
                reset_credits = fetch_quota._codex_reset_credits(
                    data if isinstance(data, dict) else {}
                )
                self.assertIsInstance(reset_credits["reported"], bool)
                for key in ("available", "applicable"):
                    value = reset_credits[key]
                    if isinstance(value, float):
                        self.assertTrue(math.isfinite(value), f"{key}={value!r}")
                json.dumps(reset_credits, allow_nan=False)

    def test_known_reset_credit_shapes(self) -> None:
        reported = fetch_quota._codex_reset_credits(CODEX_SEED_CORPUS[0])
        self.assertIs(reported["reported"], True)
        self.assertEqual(reported["available"], 0)
        self.assertEqual(reported["applicable"], 2)
        # A reported balance the wire left empty is a zero, not a missing card.
        empty = fetch_quota._codex_reset_credits(CODEX_SEED_CORPUS[7])
        self.assertEqual(empty, {"reported": True, "available": 0, "applicable": 0})
        # A count the panel's JSON parser cannot read is the reported-zero path,
        # the same as a count the wire left empty.
        self.assertEqual(
            fetch_quota._codex_reset_credits(CODEX_SEED_CORPUS[8])["available"], 0
        )
        absent = fetch_quota._codex_reset_credits({})
        self.assertIs(absent["reported"], False)
        self.assertIsNone(absent["available"])

    def test_expiry_out_of_range_is_not_a_date(self) -> None:
        # An exp far past any renderable instant is finite, and rounding it
        # to milliseconds is what used to raise out of the poll.
        for claim in ('{"exp":1e308}', '{"exp":-1e308}', '{"exp":1e400}'):
            payload = base64.urlsafe_b64encode(claim.encode()).rstrip(b"=").decode()
            with self.subTest(claim=claim):
                self.assertIsNone(fetch_quota._jwt_exp_ms(f"h.{payload}.s"))
        # The same value in the Claude credential file, read as an expiry.
        self.assertIs(fetch_quota._claude_expired({"expiresAt": -1e308}), False)
        self.assertIs(fetch_quota._claude_expired({"expiresAt": 1e308}), False)

    def test_fuzz_window_label(self) -> None:
        for iteration in range(ITERATIONS):
            rng = random.Random(BASE_SEED + 60_000 + iteration)
            seconds = rng.choice([None, 0, -1, 1, 18_000, 172_800, 604_800])
            label = fetch_quota._codex_window_label(seconds, "primary_window")
            self.assertIsInstance(label, str)
            self.assertTrue(label)
            self.assertTrue(label.encode("utf-8"))


class TimestampFuzz(unittest.TestCase):
    """Invariants for the ISO-8601 reader every provider body feeds."""

    def _check(self, raw: Any) -> int | None:
        ms = fetch_quota.iso_to_ms(raw)
        self.assertTrue(
            ms is None or (isinstance(ms, int) and not isinstance(ms, bool)),
            f"iso_to_ms({raw!r}) = {ms!r}",
        )
        return ms

    def test_fuzz_vendor_timestamps(self) -> None:
        for iteration in range(ITERATIONS):
            rng = random.Random(BASE_SEED + 30_000 + iteration)
            raw = rng.choice(TIMESTAMP_SEEDS) if rng.random() < 0.5 else _rand_json(rng)
            with self.subTest(iteration=iteration, seed=BASE_SEED + 30_000 + iteration):
                self._check(raw)

    def test_known_timestamps(self) -> None:
        self.assertEqual(self._check("2026-05-02T14:11:55.000Z"), 1777731115000)
        # Offset-free is UTC, never the host zone.
        self.assertEqual(self._check("2026-05-02T14:11:55"), 1777731115000)
        self.assertEqual(self._check("2026-05-02T16:11:55+02:00"), 1777731115000)
        self.assertEqual(self._check(1_743_691_915_000), 1743691915000)
        for raw in ("", "not a date", "2026-13-45T99:99:99Z", None):
            self.assertIsNone(self._check(raw))

    def test_pathological_timestamps_terminate(self) -> None:
        """A megabyte of near-date text must not stall a poll."""
        self.assertIsNone(self._check("2026-05-02T14:11:55Z" * 50_000))
        self.assertIsNone(self._check("9" * 1_000_000))
        self.assertIsNone(self._check("\x00" * 1_000_000))



class RetryAfterFuzz(unittest.TestCase):
    """Invariants for the Retry-After header, which reaches sleep() as-is."""

    def test_fuzz_retry_after(self) -> None:
        for iteration in range(ITERATIONS):
            rng = random.Random(BASE_SEED + 80_000 + iteration)
            raw = (
                rng.choice(RETRY_AFTER_SEEDS) if rng.random() < 0.5 else _rand_json(rng)
            )
            with self.subTest(iteration=iteration, seed=BASE_SEED + 80_000 + iteration):
                wait = fetch_quota.parse_retry_after(raw)
                self.assertTrue(wait is None or isinstance(wait, float))
                if wait is not None:
                    # This value reaches sleep(): finite, never negative, so
                    # a hostile header cannot park a poll.
                    self.assertTrue(math.isfinite(wait))
                    self.assertGreaterEqual(wait, 0.0)

    def test_known_retry_after(self) -> None:
        self.assertEqual(fetch_quota.parse_retry_after("5"), 5.0)
        self.assertEqual(fetch_quota.parse_retry_after("-10"), 0.0)
        self.assertIsNone(fetch_quota.parse_retry_after("nan"))
        self.assertIsNone(fetch_quota.parse_retry_after("inf"))
        self.assertIsNone(fetch_quota.parse_retry_after("1e999"))
        self.assertIsNone(fetch_quota.parse_retry_after("not a date"))
        # A header the parser hands back as something other than text is not
        # a wait; it must not reach the strip() below it.
        self.assertIsNone(fetch_quota.parse_retry_after({"Retry-After": 5}))  # type: ignore[arg-type]


class ProviderCacheRoundTripFuzz(unittest.TestCase):
    """A parsed reading must survive the cache write and read unchanged."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._env = _cache_dir(Path(self.tmp.name) / "cache")
        self._env.__enter__()
        self.addCleanup(self._env.__exit__, None, None, None)
        self.account = "acct-fuzz"

    def _check(self, payload: JsonDict) -> None:
        _assert_serializable(self, payload)
        fetch_quota._write_provider_cache("claude", payload, self.account)
        got = fetch_quota._read_provider_cache("claude", self.account)
        self.assertIsNotNone(got)
        assert got is not None
        # fetched_ms comes back as the instant of the write, not of the read,
        # so a replay cannot buy a second fresh window.
        self.assertGreaterEqual(got["fetched_ms"], payload["fetched_ms"])
        self.assertLessEqual(got["fetched_ms"], fetch_quota.now_ms())
        for key, value in payload.items():
            if key == "fetched_ms":
                continue
            self.assertEqual(got[key], value, f"{key} did not survive the round trip")
        stale = fetch_quota._stale_cache("claude", self.account)
        self.assertIsNotNone(stale)
        assert stale is not None
        self.assertIs(stale["stale"], True)

    def test_fuzz_parsed_readings_survive_the_cache(self) -> None:
        fuzz = ClaudeUsageFuzz()
        for iteration in range(CACHE_ITERATIONS):
            rng = random.Random(BASE_SEED + 90_000 + iteration)
            weekly, (session, _) = fuzz._check(_rand_claude(rng))
            payload = fetch_quota._reading(
                {
                    "ok": True,
                    "plan": "Pro",
                    "session": {"util": session, "resets_ms": None},
                    "weekly": weekly,
                }
            )
            with self.subTest(iteration=iteration, seed=BASE_SEED + 90_000 + iteration):
                self._check(payload)

    def test_fuzz_unserializable_readings_leave_no_entry(self) -> None:
        for iteration in range(CACHE_ITERATIONS):
            rng = random.Random(BASE_SEED + 100_000 + iteration)
            leaf = _rand_json(rng, 2)
            payload = fetch_quota._reading({"ok": True, "plan": leaf, "extra": leaf})
            with self.subTest(
                iteration=iteration, seed=BASE_SEED + 100_000 + iteration
            ):
                # A fresh cache per iteration, so a refused write is judged
                # against an empty store rather than the previous entry.
                with _cache_dir(Path(self.tmp.name) / f"case{iteration}"):
                    try:
                        self._check(payload)
                    except (TypeError, ValueError):
                        # The write refuses what JSON cannot carry; the next
                        # read must find nothing rather than a torn entry.
                        self.assertIsNone(
                            fetch_quota._read_provider_cache("claude", self.account)
                        )


if __name__ == "__main__":
    unittest.main()
