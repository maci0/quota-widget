"""Randomized (property) fuzzing for the parsers fed untrusted input.

Two surfaces carry bytes or JSON that this process does not control:

- ``parse_cursor_summary`` reads the Cursor usage-summary body off the wire
  and out of ``~/.cache/quota-widget/cursor.json``.
- ``_vscdb_str`` and ``_jwt_payload`` read cells and tokens out of a Cursor
  SQLite state DB and a vendor credential file.

Both run inside a plasmashell poll, so a raise is a dead widget until the
next reload. The generators are seeded, so a failure reproduces from the
printed seed. A fuzzer only proves the presence of a bug; the assertions
below are the invariant half, and they turn a wrong answer into a failure
the generator can see.
"""

from __future__ import annotations

import json
import math
import random
import unittest
from typing import Any

import fetch_quota

JsonDict = dict[str, Any]

ITERATIONS = 2000
BASE_SEED = 20260928

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
        # The panel parses this with JSON; a NaN or Infinity here blanks the
        # whole widget, so the emitted document must round-trip.
        json.loads(json.dumps(parsed, allow_nan=False))
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


if __name__ == "__main__":
    unittest.main()
