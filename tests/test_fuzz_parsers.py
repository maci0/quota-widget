"""Randomized (property) fuzzing for the parsers fed untrusted input.

Every surface that carries bytes or JSON this process does not control:

- ``parse_cursor_summary`` reads the Cursor usage-summary body off the wire
  and out of ``~/.cache/quota-widget/cursor.json``.
- ``_claude_weekly`` and ``_claude_session`` read the Claude usage body.
- ``_codex_window`` reads the Codex usage body.
- ``_parse_grok_period`` reads the Grok billing config.
- ``iso_to_ms`` reads the timestamps those bodies carry.
- ``_vscdb_str`` and ``_jwt_payload`` read cells and tokens out of a Cursor
  SQLite state DB and a vendor credential file.

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


# Claude usage bodies: the structured `limits` list, the legacy seven_day
# blocks, and what a changed or hostile response sends instead.
CLAUDE_BODY_SEEDS: tuple[JsonDict, ...] = (
    {
        "five_hour": {
            "utilization": 12,
            "resets_at": "2026-05-02T14:11:55.000Z",
        },
        "limits": [
            {
                "kind": "session",
                "group": "session",
                "percent": 12,
                "resets_at": "2026-05-02T14:11:55.000Z",
            },
            {
                "kind": "weekly_all",
                "percent": 40,
                "resets_at": "2026-05-09T14:11:55.000Z",
                "scope": {},
            },
            {
                "kind": "weekly_model",
                "percent": 33.3,
                "resets_at": "2026-05-09T14:11:55.000Z",
                "scope": {"model": {"display_name": "Opus"}},
            },
            {
                "kind": "weekly_surface",
                "percent": 5,
                "scope": {"surface": "Claude Code"},
            },
        ],
    },
    {
        "five_hour": {"utilization": 0, "resets_at": "2026-05-02T14:11:55.000Z"},
        "seven_day": {"utilization": 40, "resets_at": "2026-05-09T14:11:55.000Z"},
        "seven_day_opus": {"utilization": 80, "resets_at": None},
        "extra_usage": {
            "is_enabled": True,
            "used_credits": 4.2,
            "currency": "USD",
            "monthly_limit": 20,
        },
        "spend": {
            "enabled": True,
            "percent": 17,
            "used": {"amount_minor": 1234, "currency": "USD", "exponent": 2},
        },
    },
    # Percentages where the schema says number, and a limits list of junk.
    {"limits": [{"percent": "40"}, {"percent": float("nan")}, "not-a-dict"]},
    {"limits": [{"kind": "weekly_all", "percent": 1e308}]},
    {"five_hour": {"utilization": None, "resets_at": 1743691915000}},
    {},
)

# Codex usage bodies, in the window shape the reader walks.
CODEX_BODY_SEEDS: tuple[JsonDict, ...] = (
    {
        "plan_type": "pro",
        "rate_limit": {
            "allowed": 3,
            "limit_reached": False,
            "primary_window": {
                "used_percent": 12.5,
                "limit_window_seconds": 18_000,
                "reset_after_seconds": 3600,
            },
            "secondary_window": {
                "used_percent": 33.0,
                "limit_window_seconds": 604_800,
                "reset_at": 1_777_731_115.0,
            },
        },
    },
    {
        "plan_type": "plus",
        "rate_limit": {
            "allowed": 0,
            "limit_reached": True,
            "primary_window": {
                "used_percent": "100",
                "limit_window_seconds": 604_800,
                "reset_at": 1_777_731_115,
            },
        },
        "code_review_rate_limit": {
            "primary_window": {
                "used_percent": 7.5,
                "limit_window_seconds": 604_800,
                "reset_after_seconds": 60,
            }
        },
        "credits": {"has_credits": True, "balance": 12.5, "unlimited": False},
        "rate_limit_reset_credits": {
            "available_count": 0,
            "applicable_available_count": 0,
        },
    },
    {
        # Percentages out of range and non-finite, and a window missing them.
        "rate_limit": {
            "primary_window": {"used_percent": 5000, "limit_window_seconds": 1},
            "secondary_window": {"used_percent": float("nan")},
        }
    },
    {"rate_limit": {"primary_window": {"used_percent": float("inf")}}},
    {"rate_limit": {"primary_window": {"used_percent": 1e308, "reset_at": 1e308}}},
    {"rate_limit": {"primary_window": "not-a-window"}},
    {},
)

# Grok billing configs: the legacy dollar shape and the unified-credits one.
GROK_CFG_SEEDS: tuple[JsonDict, ...] = (
    {
        "onDemandCap": {"val": 1000},
        "used": 123,
        "monthlyLimit": 2000,
        "billingPeriodStart": "2026-05-01T00:00:00Z",
        "billingPeriodEnd": "2026-06-01T00:00:00Z",
    },
    {
        "creditUsagePercent": 12.3,
        "isUnifiedBillingUser": True,
        "currentPeriod": {
            "type": "CREDIT_MONTHLY",
            "start": "2026-05-01T00:00:00Z",
            "end": "2026-06-01T00:00:00Z",
        },
    },
    {
        # Zero credit percent is omitted by the API, so the shape alone means 0.
        "currentPeriod": {"type": "CREDIT_WEEKLY", "start": None, "end": ""},
    },
    {"creditUsagePercent": "12.3", "currentPeriod": {"type": "CREDIT_MONTHLY"}},
    {"used": float("nan"), "monthlyLimit": 0, "monthly_limit": "2000"},
    {"used": {"val": 12.7}, "monthlyLimit": {"val": None}},
    {"onDemandCap": float("inf"), "billingPeriodEnd": 1743691915000},
    {},
)

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


def _rand_body(rng: random.Random, seeds: tuple[JsonDict, ...]) -> Any:
    """A provider body: a real shape with hostile values written over it."""
    base = json.loads(json.dumps(rng.choice(seeds)))
    if not isinstance(base, dict):  # pragma: no cover - corpora are dicts
        return base
    for key in list(base) + rng.sample(
        sorted(base), k=min(len(base), rng.randrange(4))
    ):
        if rng.random() < 0.45:
            base[key] = _rand_json(rng)
    # The wrong-typed leaf usually lands one level down, inside the block the
    # reader walks, so mutate the blocks and their list entries too.
    for value in base.values():
        if isinstance(value, dict) and value and rng.random() < 0.5:
            value[rng.choice(sorted(value))] = _rand_json(rng)
        elif isinstance(value, list) and value and rng.random() < 0.6:
            entry = value[rng.randrange(len(value))]
            if isinstance(entry, dict) and entry and rng.random() < 0.7:
                entry[rng.choice(sorted(entry))] = _rand_json(rng)
            elif rng.random() < 0.3:
                value[rng.randrange(len(value))] = _rand_json(rng)
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


def _assert_meter(case: unittest.TestCase, meter: JsonDict) -> None:
    """A meter is a labelled, JSON-safe reading or an absent one, never junk."""
    case.assertIsInstance(meter["label"], str)
    case.assertTrue(meter["label"])
    case.assertIsInstance(meter["kind"], str)
    util = meter["util"]
    case.assertTrue(util is None or (isinstance(util, float) and math.isfinite(util)))
    resets = meter["resets_ms"]
    case.assertTrue(
        resets is None or (isinstance(resets, int) and not isinstance(resets, bool))
    )


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


def _check_claude(case: unittest.TestCase, data: Any) -> tuple[list[JsonDict], Any]:
    weekly = fetch_quota._claude_weekly(data)
    case.assertIsInstance(weekly, list)
    case.assertLessEqual(len(weekly), fetch_quota.MAX_WEEKLY_LIMITS)
    for meter in weekly:
        _assert_meter(case, meter)
    util, resets_ms = fetch_quota._claude_session(data)
    case.assertTrue(util is None or (isinstance(util, float) and math.isfinite(util)))
    case.assertTrue(
        resets_ms is None
        or (isinstance(resets_ms, int) and not isinstance(resets_ms, bool))
    )
    _assert_serializable(case, {"weekly": weekly, "session": [util, resets_ms]})
    return weekly, util


def _check_codex(case: unittest.TestCase, data: Any) -> list[JsonDict]:
    rate = fetch_quota._as_dict(data.get("rate_limit"))
    windows: list[JsonDict] = []
    for key in ("primary_window", "secondary_window"):
        window = fetch_quota._codex_window(rate.get(key), key)
        if window is not None:
            windows.append(window)
    case.assertLessEqual(len(windows), 2)
    for window in windows:
        _assert_meter(case, window)
        # The meter is a percentage of the vendor's window, so 5000% is not
        # a meter; clamping it is what keeps the gauge on its scale.
        case.assertGreaterEqual(window["util"], 0.0)
        case.assertLessEqual(window["util"], 100.0)
        seconds = window["window_seconds"]
        case.assertTrue(
            seconds is None
            or (isinstance(seconds, int) and not isinstance(seconds, bool))
        )
    credits = fetch_quota._codex_reset_credits(data)
    case.assertIsInstance(credits["reported"], bool)
    _assert_serializable(case, {"windows": windows, "credits": credits})
    return windows


def _check_grok(case: unittest.TestCase, cfg: Any) -> JsonDict:
    period = fetch_quota._parse_grok_period(cfg)
    case.assertIn(period["label"], ("Weekly", "Monthly", "Usage"))
    case.assertEqual(period["unit"], "cents")
    for key in ("util", "used", "limit", "on_demand_cap", "period_start_ms"):
        value = period[key]
        if value is not None:
            case.assertIsInstance(value, (int, float))
        if isinstance(value, float):
            case.assertTrue(math.isfinite(value), f"{key}={value!r} not finite")
    resets = period["resets_ms"]
    case.assertTrue(
        resets is None or (isinstance(resets, int) and not isinstance(resets, bool))
    )
    _assert_serializable(case, period)
    return period


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
        self.assertEqual(first["limit_type"], "user")
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
        self.assertIsNone(parsed["resets_ms"])


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
                # A token that claims an expiry must produce a usable instant.
                exp = fetch_quota._jwt_exp_ms(token)
                self.assertTrue(exp is None or isinstance(exp, int))

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
        for raw in ("", "not a date", "2026-13-45T99:99:99Z", None, 1_743_691_915_000):
            self.assertIsNone(self._check(raw))

    def test_pathological_timestamps_terminate(self) -> None:
        """A megabyte of near-date text must not stall a poll."""
        self.assertIsNone(self._check("2026-05-02T14:11:55Z" * 50_000))
        self.assertIsNone(self._check("9" * 1_000_000))
        self.assertIsNone(self._check("\x00" * 1_000_000))


class ClaudeBodyFuzz(unittest.TestCase):
    """Invariants for the Claude weekly and session window readers."""

    def _check(self, data: Any) -> tuple[list[JsonDict], Any]:
        return _check_claude(self, data)

    def test_fuzz_claude_usage_body(self) -> None:
        for iteration in range(ITERATIONS):
            rng = random.Random(BASE_SEED + 40_000 + iteration)
            data = _rand_body(rng, CLAUDE_BODY_SEEDS)
            with self.subTest(iteration=iteration, seed=BASE_SEED + 40_000 + iteration):
                self._check(data)

    def test_known_shapes_parse_as_before(self) -> None:
        weekly, session = self._check(json.loads(json.dumps(CLAUDE_BODY_SEEDS[0])))
        self.assertEqual(
            [m["label"] for m in weekly], ["All models", "Opus", "Claude Code"]
        )
        self.assertEqual([m["util"] for m in weekly], [40.0, 33.3, 5.0])
        self.assertEqual(session, 12.0)
        legacy, _ = self._check(json.loads(json.dumps(CLAUDE_BODY_SEEDS[1])))
        self.assertEqual([m["label"] for m in legacy], ["All models", "Opus"])
        self.assertIsNone(legacy[1]["resets_ms"])


class CodexBodyFuzz(unittest.TestCase):
    """Invariants for the Codex rate-limit window reader and its labels."""

    def _check(self, data: Any) -> list[JsonDict]:
        return _check_codex(self, data)

    def test_fuzz_codex_usage_body(self) -> None:
        for iteration in range(ITERATIONS):
            rng = random.Random(BASE_SEED + 50_000 + iteration)
            data = _rand_body(rng, CODEX_BODY_SEEDS)
            with self.subTest(iteration=iteration, seed=BASE_SEED + 50_000 + iteration):
                self._check(data)

    def test_fuzz_window_label(self) -> None:
        for iteration in range(ITERATIONS):
            rng = random.Random(BASE_SEED + 60_000 + iteration)
            seconds = rng.choice([None, 0, -1, 1, 18_000, 172_800, 604_800])
            label = fetch_quota._codex_window_label(seconds, "primary_window")
            self.assertIsInstance(label, str)
            self.assertTrue(label)
            self.assertTrue(label.encode("utf-8"))

    def test_known_shapes_parse_as_before(self) -> None:
        windows = self._check(json.loads(json.dumps(CODEX_BODY_SEEDS[0])))
        self.assertEqual([w["label"] for w in windows], ["Current session", "Weekly"])
        self.assertEqual([w["util"] for w in windows], [12.5, 33.0])
        self.assertEqual(windows[1]["resets_ms"], 1_777_731_115_000)
        # A string percentage is a percentage; 5000% clamps to the scale.
        self.assertEqual(
            self._check(json.loads(json.dumps(CODEX_BODY_SEEDS[2])))[0]["util"], 100.0
        )
        # NaN and Infinity are no reading at all, not a full meter.
        self.assertEqual(
            self._check(json.loads(json.dumps(CODEX_BODY_SEEDS[3]))),
            [],
        )
        self.assertEqual(
            self._check(json.loads(json.dumps(CODEX_BODY_SEEDS[4])))[0]["util"], 100.0
        )
        # A reset that no date can render is an absent reset, not a raise.
        self.assertIsNone(
            self._check(json.loads(json.dumps(CODEX_BODY_SEEDS[4])))[0]["resets_ms"]
        )


class GrokBillingFuzz(unittest.TestCase):
    """Invariants for the Grok billing-config reader."""

    def _check(self, cfg: Any) -> JsonDict:
        return _check_grok(self, cfg)

    def test_fuzz_billing_config(self) -> None:
        for iteration in range(ITERATIONS):
            rng = random.Random(BASE_SEED + 70_000 + iteration)
            cfg = _rand_body(rng, GROK_CFG_SEEDS)
            with self.subTest(iteration=iteration, seed=BASE_SEED + 70_000 + iteration):
                self._check(cfg)

    def test_known_shapes_parse_as_before(self) -> None:
        legacy = self._check(json.loads(json.dumps(GROK_CFG_SEEDS[0])))
        self.assertEqual(legacy["label"], "Monthly")
        self.assertEqual(legacy["util"], 6.2)
        self.assertEqual(legacy["used"], 123)  # a dollar amount in cents
        self.assertEqual(legacy["limit"], 2000)
        self.assertEqual(legacy["on_demand_cap"], 1000)
        credits = self._check(json.loads(json.dumps(GROK_CFG_SEEDS[1])))
        self.assertEqual(credits["label"], "Monthly")
        self.assertEqual(credits["util"], 12.3)
        self.assertIsNone(credits["used"])
        # An omitted credit percent is a reported zero, not an absent meter.
        self.assertEqual(
            self._check(json.loads(json.dumps(GROK_CFG_SEEDS[2])))["util"], 0.0
        )


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
        for iteration in range(CACHE_ITERATIONS):
            rng = random.Random(BASE_SEED + 90_000 + iteration)
            weekly, session = _check_claude(self, _rand_body(rng, CLAUDE_BODY_SEEDS))
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
