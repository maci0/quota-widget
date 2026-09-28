#!/usr/bin/env python3
"""Fetch Claude + Cursor + Grok + Codex usage quotas for the Plasma widget.

Claude: GET https://api.anthropic.com/api/oauth/usage
  (same numbers as claude.ai Settings → Usage / Claude Code /usage)
  Auth: ~/.claude/.credentials.json → claudeAiOauth.accessToken

Cursor: GET https://cursor.com/api/usage-summary
  (same numbers as cursor.com/dashboard → Usage)
  Auth: cursor-agent ~/.config/cursor/auth.json, else the Cursor IDE session in
  state.vscdb (that order is the lookup order)

Grok:   GET https://cli-chat-proxy.grok.com/v1/billing
  Auth: ~/.grok/auth.json OIDC access token (auto-refreshed)

Codex:  GET https://chatgpt.com/backend-api/wham/usage
  (same numbers as chatgpt.com/codex/settings/usage and Codex /status)
  Auth: ~/.codex/auth.json ChatGPT OAuth tokens (auto-refreshed)

Configuration is read once at startup from QUOTA_WIDGET_* environment
variables and validated before any request; run with --print-config to see the
active values. See README "Configuration".

One module, in this order: environment names and their validation, the clock,
Config and load_config, the emit/warn/redact output pair, JSON and filesystem
helpers, the account digest and the two cache layers, HTTP, then one section
per provider (Claude, Grok, Codex, Cursor) and main() at the foot. A provider
section owns its credential lookup, its refresh, and its parser, and reaches
for everything else through the helpers above it.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import email.utils
import errno
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from email.message import Message
from functools import lru_cache
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Literal, TypeAlias
from urllib.request import pathname2url

if TYPE_CHECKING:
    from http.client import HTTPMessage
    from types import ModuleType

# flock is POSIX-only; on Windows the refresh lock degrades to no lock, which
# costs a possible double refresh, not a broken poll.
fcntl: ModuleType | None
try:
    import fcntl
except ImportError:
    fcntl = None

# Unversioned HTTP JSON: keys and nesting change by plan, host, and API revision.
JsonDict: TypeAlias = dict[str, Any]

# Overrides every wall-clock read in this module; see now_ms().
NOW_MS_ENV = "QUOTA_WIDGET_NOW_MS"

# Every knob, in one place: the name load_config reads, the name --help prints,
# and the membership test that rejects a misspelling. Adding a knob means
# adding its row, not a third copy of the name.
ENV_HOME = "QUOTA_WIDGET_HOME"
ENV_CACHE = "QUOTA_WIDGET_CACHE"
ENV_CACHE_MAX_AGE_S = "QUOTA_WIDGET_CACHE_MAX_AGE_S"
ENV_ACCOUNT_SALT = "QUOTA_WIDGET_ACCOUNT_SALT"
ENV_HTTP_TIMEOUT = "QUOTA_WIDGET_HTTP_TIMEOUT"
ENV_CLAUDE_CREDENTIALS = "QUOTA_WIDGET_CLAUDE_CREDENTIALS"
ENV_CODEX_AUTH = "QUOTA_WIDGET_CODEX_AUTH"
ENV_GROK_AUTH = "QUOTA_WIDGET_GROK_AUTH"
ENV_CURSOR_AUTH = "QUOTA_WIDGET_CURSOR_AUTH"
ENV_CURSOR_STATE_DB = "QUOTA_WIDGET_CURSOR_STATE_DB"

# Prefix a knob must carry, so a variable the widget does not own is never
# mistaken for a typo of one that it does.
ENV_PREFIX = "QUOTA_WIDGET_"

# (name, help text) in --help order. README's table carries the defaults.
ENV_DOCS: tuple[tuple[str, str], ...] = (
    (ENV_HOME, "base for credential and cache paths"),
    (ENV_CACHE, "provider cache dir"),
    (ENV_CACHE_MAX_AGE_S, "seconds a reading stays fresh (24 h, max 24 h)"),
    (ENV_ACCOUNT_SALT, "hex account-salt key, for replays"),
    (ENV_HTTP_TIMEOUT, "per-request timeout, 0 < s <= 300"),
    (NOW_MS_ENV, "pin the clock (ms since epoch) for replays"),
    (ENV_CLAUDE_CREDENTIALS, "Claude credentials file"),
    (ENV_CODEX_AUTH, "Codex auth file"),
    (ENV_GROK_AUTH, "Grok auth file"),
    (ENV_CURSOR_AUTH, "Cursor auth file"),
    (ENV_CURSOR_STATE_DB, "Cursor state db"),
)
KNOWN_ENV = frozenset(name for name, _ in ENV_DOCS)


def _unknown_env(env: Mapping[str, str]) -> str | None:
    """The first QUOTA_WIDGET_* name with no knob behind it, or None.

    A misspelling is otherwise indistinguishable from an unset variable: the
    poll succeeds and the setting the user asked for silently does nothing.
    XDG_* are the base-directory spec's own and are read, not validated here.
    """
    for name in sorted(env):
        if name.startswith(ENV_PREFIX) and name not in KNOWN_ENV:
            return name
    return None


# Every credential, cache, and state file is UTF-8 JSON, including the ones the
# vendor CLIs write. Naming it beats open()'s locale default, which is ASCII
# under a C locale (a plasmashell started without LANG) and would decode a
# store holding a non-ASCII account name into a read error.
JSON_ENCODING = "utf-8"

# One form for every text that is compared or keyed: NFC, the form a provider's
# own UI and a desktop file manager both store. An id spelled NFD (a macOS or
# decomposed vendor string) and the same id spelled NFC are equal strings after
# this and hash to the same account scope; left raw they are two accounts.
NORMALIZATION_FORM: Literal["NFC"] = "NFC"

# Bottom and top of the datetime range now_utc() can represent, in epoch-ms. A
# pinned clock outside it is a config error, not a poll-time crash. The bottom
# is the epoch, which the "before the epoch" check above already refuses, so in
# the range test only the top bound can fire.
MIN_PINNED_MS = -62_135_596_800_000  # 0001-01-01T00:00:00Z
MAX_PINNED_MS = 253_402_300_799_999  # 9999-12-31T23:59:59.999Z

# update(obj) mutates obj and returns the (key, value) pair that must survive.
MergeUpdate: TypeAlias = Callable[[JsonDict], "tuple[str, Any]"]


def _as_dict(value: object) -> JsonDict:
    return value if isinstance(value, dict) else {}


def _as_text(value: object) -> str | None:
    """A wire field that the UI renders as text, or None.

    The emitted document goes to plasmashell through its JSON parser, so a
    field that arrives as a number, a list, or NaN has to be dropped rather
    than passed through.
    """
    return value if isinstance(value, str) else None


def _read_text(path: Path) -> str:
    """Read a JSON state file as UTF-8, whatever the process locale says."""
    return path.read_text(encoding=JSON_ENCODING)


def _utf8_encodable(value: str) -> bool:
    """Whether the value survives being encoded as UTF-8.

    Text off the wire can hold an unpaired surrogate (a JSON "\\ud800" escape,
    or a cell a state.vscdb holds as raw bytes), which decodes but does not
    encode: percent-encoding a header, digesting an account id, or writing a
    cache entry all raise on it. Such a value is dropped where it arrives, the
    rule _vscdb_str and _digest already follow.
    """
    try:
        value.encode(JSON_ENCODING)
    except UnicodeEncodeError:
        return False
    return True


# A label the panel renders comes off the wire, so it is bounded and free of
# the characters a label never needs: Cc is a control character and Cf a
# format one, the set carrying the bidi overrides and zero-width joiners that
# reorder or hide the text beside them. An unbounded name is copied into the
# cache entry and drawn on every poll.
MAX_LABEL_CHARS = 64
LABEL_CATEGORIES = frozenset({"Cc", "Cf"})


def _label_text(value: object) -> str:
    """Display text for a wire value, or "" for one that names nothing."""
    if not isinstance(value, str):
        return ""
    text = "".join(
        ch for ch in value if unicodedata.category(ch) not in LABEL_CATEGORIES
    )
    return text.strip()[:MAX_LABEL_CHARS].strip()


def _read_json_dict(path: Path) -> JsonDict | None:
    """The JSON object at path, or None if it is missing, unreadable, or not
    an object. A store that holds a list reads as absent rather than raising on
    the caller's first .get()."""
    try:
        obj = json.loads(_read_text(path))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _transient_failure(status: int) -> bool:
    """Whether a cached reading beats reporting this failure.

    Status 0 is a transport failure, not an HTTP status: the request never
    reached the vendor. REFUSED_STATUS is a request this fetcher would not
    send, which is the same kind of news: the account, the credential, and the
    vendor are all as they were, so the last good reading is still the best
    answer. The panel already keeps its last good reading through a transport
    failure, so a machine that is offline at the first poll of a session would
    otherwise show a blank card where the same reading is sitting on disk.
    A 401 or 403 is a decision by the vendor and is reported as one.

    A body the fetcher refused to read is transient for the same reason a 5xx
    is: the vendor answered, nothing about the answer can be measured, and the
    next poll reads a real one. A kept reading beats a blank card, and the
    journal says which body was refused.
    """
    return status in {0, REFUSED_STATUS, UNREADABLE_BODY_STATUS, 429} or status >= 500


def _failure(
    error: str, account: str | None = None, *, transient: bool = False
) -> JsonDict:
    """A provider failure payload, in the one shape every provider emits.

    `transient` is the classification, not a hint: the panel decides whether a
    failed poll keeps the card it holds by reading this flag. The fetcher is the
    only place that knows the rule (see _transient_failure), and the panel
    re-deriving it from the code text is how the two drift apart: a code added
    later would silently read as final and blank a card on a rate limit.
    """
    out: JsonDict = {"ok": False, "error": error, "transient": transient}
    if account is not None:
        out["account"] = account
    return out


def _http_error(status: int, account: str | None) -> JsonDict:
    """Failure payload for a provider call. Status 0 is the fetcher's own code
    for a request that never got a response, and reads as "net" to the panel.
    UNREADABLE_BODY_STATUS is the other: the vendor answered, and the answer
    was not one this module can measure. A failure names the account it was
    made for like a success does, so the panel keeps the card scoped to the
    account that is still signed in."""
    if status == UNREADABLE_BODY_STATUS:
        label = BAD_BODY_ERROR
    else:
        label = _error_code(status)
    return _failure(
        label,
        account,
        transient=_transient_failure(status),
    )


def _error_code(status: int) -> str:
    """The panel-facing code for a status.

    The two codes that are not a vendor's answer say so: a request that never
    got a response is "net", and one this fetcher refused to send is "refused".
    Both would otherwise be spelled "http-<status>", which claims the vendor
    returned a status it never returned, and hands the panel a family it reads
    as a decision about the account.
    """
    if status == REFUSED_STATUS:
        return "refused"
    return f"http-{status}" if status else "net"


class ConfigError(ValueError):
    """A configuration value is unset-but-empty, unparsable, or out of range."""


def _pinned_ms(raw: str) -> int:
    """The pinned clock. Every other knob is validated by load_config, and this
    one has to be too: the emit path reads the clock outside _safe_fetch, so a
    malformed pin would abort the whole poll instead of one provider."""
    if not raw.strip():
        raise ConfigError(f"{NOW_MS_ENV} is set but empty")
    try:
        pinned = int(raw)
    except ValueError:
        raise ConfigError(
            f"{NOW_MS_ENV}={raw!r} is not an integer epoch-ms value"
        ) from None
    if pinned < 0:
        raise ConfigError(f"{NOW_MS_ENV}={raw!r} is before the epoch")
    # now_utc() adds the pin to the epoch as a timedelta, and so does every
    # "now + expiry" calculation in a refresh. A pin past datetime's range
    # raises there, mid-poll, after the token POST has already retired the old
    # refresh token: the credential is rotated away and never written back.
    # _shifted() holds the sums that would raise, so the ceiling here is the
    # last instant now_utc() itself can represent.
    if pinned > MAX_PINNED_MS:
        raise ConfigError(
            f"{NOW_MS_ENV}={raw!r} is past the representable date range, "
            f"0..{MAX_PINNED_MS} epoch-ms"
        )
    return pinned


def now_ms() -> int:
    """Epoch milliseconds. QUOTA_WIDGET_NOW_MS pins the clock to a fixed value,
    so a whole poll replays byte-for-byte; unset in production, real clock."""
    override = os.environ.get(NOW_MS_ENV)
    if override is None:
        return ms_from_seconds(time.time())
    return _pinned_ms(override)


EPOCH_UTC = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


def ms_from_seconds(seconds: float) -> int:
    """Epoch-ms from a seconds value, rounded to the nearest millisecond.

    Truncation drops a millisecond whenever the double lands a hair under the
    real value, so a reset the API sent in whole milliseconds displays a
    minute early after the seconds are divided back out.
    """
    return round(seconds * 1000)


def ms_from_seconds_or_none(seconds: float) -> int | None:
    """Epoch-ms from a wire seconds value, or None when it is out of range.

    A finite double can still overflow the millisecond product (1e308 s is
    1e311 ms), and round() raises on the resulting infinity. A reset the
    widget cannot place on a date is shown as absent, never as a bogus one.
    """
    try:
        return ms_from_seconds(seconds)
    except (OverflowError, ValueError):
        return None


def now_utc() -> dt.datetime:
    return EPOCH_UTC + dt.timedelta(milliseconds=now_ms())


# The last instant datetime holds, used where "now + something" runs off the end
# of the calendar. It is the end itself, not a second calendar: the sums below
# are the only ones that can reach it, and a value past it has no earlier
# instant to compare against.
LAST_UTC = dt.datetime.max.replace(tzinfo=dt.UTC)


def _shifted(seconds: float) -> dt.datetime:
    """now_utc() plus a lifetime in seconds, saturating at the end of the
    calendar.

    A pinned clock and a token lifetime each reach this sum on their own: a pin
    at the ceiling the validator accepts, an `expires_in` past datetime's own
    range (timedelta raises on the value, before the sum is ever taken), or a
    lifetime so long the sum lands past the last instant. The plain expression
    raises OverflowError in all three, and on a refresh path that is after the
    token POST has retired the old refresh token, so the credential is rotated
    away and never written back. Past the last instant every lifetime is the
    same instant, so the result saturates instead.
    """
    try:
        return now_utc() + dt.timedelta(seconds=seconds)
    except OverflowError:
        return LAST_UTC


def _finite_number(value: object) -> float | None:
    """A JSON number that survives serialization, or None.

    json.loads accepts NaN and 1e400 (Infinity), and json.dumps writes them
    back as bare NaN/Infinity, which is not JSON and which plasmashell's
    parser rejects. A missing number must read as absent, never as a clamped
    zero or a full 100%.

    An integer literal is the other shape the docstring covers. json.loads
    turns one into a Python int of whatever size the body spells, and
    float() raises OverflowError past 1e308, so a 309-digit reading in a
    well-formed 200 took the whole provider down instead of reading as no
    reading. A number no float can hold is not one this payload can carry.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _emittable(value: object) -> Any:
    """A wire value the panel's JSON parser can read.

    Codex sends a balance and a reset-credit count as numbers, and a non-finite
    one (json.loads accepts both) would be written back as bare NaN/Infinity
    and take the whole poll down with it. Values that are not numbers, the
    string balances the QML documents, are passed through untouched.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    return _finite_number(value)


def _amount(value: object) -> float | str | None:
    """A wire amount the UI formats: a finite number, or the vendor's own
    numeric string, which carries precision a float would round away."""
    if isinstance(value, str):
        return value
    return _finite_number(value)


def sleep(seconds: float) -> None:
    """A seam the tests patch, so a Retry-After, a retry backoff, or a lock poll
    costs no wall clock in the suite."""
    time.sleep(seconds)


def monotonic() -> float:
    """Elapsed seconds, on a clock no wall-clock pin can move.

    A wait that ends when a deadline passes is measured here, not through
    now_ms(): a pinned QUOTA_WIDGET_NOW_MS never advances, so a deadline read
    through it would expire on the first contended poll. The seam also lets a
    test drive the wait on a virtual clock instead of the real one.
    """
    return time.monotonic()


CLAUDE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_TOKEN_URLS = (
    "https://platform.claude.com/v1/oauth/token",
    "https://console.anthropic.com/v1/oauth/token",
)

GROK_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing"
GROK_OIDC_DISCOVERY = "https://auth.x.ai/.well-known/openid-configuration"

CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"  # noqa: S105 (not a secret)
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

METADATA_JSON = Path(__file__).resolve().parents[2] / "metadata.json"


def _package_version() -> str:
    metadata = _as_dict(json.loads(METADATA_JSON.read_text(encoding="utf-8")))
    plugin = _as_dict(metadata.get("KPlugin"))
    version = plugin.get("Version")
    if not isinstance(version, str):
        raise SystemExit(f"KPlugin.Version missing from {METADATA_JSON}")
    return version


USER_AGENT = f"quota-widget/{_package_version()}"
# Anthropic rate-limits /api/oauth/usage per User-Agent; Claude Code's bucket works.
# https://github.com/anthropics/claude-code/issues/30930
CLAUDE_USER_AGENT = "claude-code/2.1.251"

CURSOR_SUMMARY_URL = "https://cursor.com/api/usage-summary"

REFRESH_LOCK_NAME = "refresh.lock"
# The key every account digest is taken under, kept beside the entries it
# scopes and nowhere else. It is a secret only in the sense that a copy of the
# cache directory is a copy of the key; there is no other copy to leak from.
ACCOUNT_SALT_NAME = "account-salt"
ACCOUNT_SALT_BYTES = 32
# flock(LOCK_NB) reports a held lock through these errnos and nothing else, so
# a wait can tell contention from a lock this platform cannot take at all.
LOCK_BUSY_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN})
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400
# One poll holds the lock for at most a token round trip; a longer wait means
# the holder died, and the caller refreshes anyway rather than never.
REFRESH_LOCK_WAIT_S = 20.0
REFRESH_LOCK_POLL_S = 0.25
# Interpreter start plus the fetcher's own import of its metadata.json.
POLL_STARTUP_S = 2.0
# Longest a cached reading may be shown after the vendor API fails, unless
# QUOTA_WIDGET_CACHE_MAX_AGE_S says otherwise. The plasmoid keeps its own copy
# for the same window, and takes the length from the poll payload so an
# override reaches it; see staleKeepMs in package/contents/ui/main.qml.
DEFAULT_CACHE_MAX_AGE_S = SECONDS_PER_DAY
MAX_CACHE_MAX_AGE_S = SECONDS_PER_DAY
# Shape of a cached reading, stamped into the entry that holds it. A poll
# replays the payload straight into the panel, so an entry written by another
# release of the QML is a value this one cannot read: it is dropped instead of
# parsed. Raise it when a provider payload changes shape, and a downgrade then
# leaves the panel without a reading rather than with a misread one.
PAYLOAD_SCHEMA = 1
DEFAULT_HTTP_TIMEOUT_S = 12.0
MAX_HTTP_TIMEOUT_S = 300.0
CACHE_DIR_MODE = 0o700
FILE_MODE_PRIVATE = 0o600
# Re-read-after-write retries before a token store is left to the racing writer.
MERGE_WRITE_ATTEMPTS = 3
# A cache entry's read-check-write is a file read and a rename, never a network
# round trip, so a holder is gone in microseconds. The deadline only bounds a
# holder that died mid-section, which then costs a later poll the same bypass
# the refresh lock takes.
ENTRY_LOCK_WAIT_S = 5.0
ENTRY_LOCK_POLL_S = 0.02
ENTRY_LOCK_SUFFIX = ".lock"
ENTRY_SUFFIX = ".json"
# Meters kept from the structured `limits` array. The panel builds a gauge per
# entry and keeps it until the next poll, so a list that grows with whatever
# the API reports is memory the widget holds for the rest of the session.
MAX_WEEKLY_LIMITS = 12
# How far a split plan meter may sit from the included meter and still be
# reported as its own gauge. A rounding difference is not a second meter.
CURSOR_METER_AGREEMENT_PCT = 0.5
TOKEN_SKEW_S = 120
TOKEN_SKEW_MS = TOKEN_SKEW_S * 1000
RETRY_AFTER_MIN_S = 0.5
RETRY_AFTER_MAX_S = 10.0
# A dropped connection is usually one blip. One retry on the idempotent reads
# (GET) covers it; a token POST is never retried, a repeated refresh can retire
# the refresh token.
NETWORK_RETRY_BACKOFF_S = 0.5
# Unix seconds vs milliseconds: values above this are treated as ms.
MS_EPOCH_CUTOFF = 10_000_000_000
# How many sequential requests one provider may make, counted in per-request
# timeouts. Grok is the longest: the OIDC discovery, a token POST, then a
# billing call that can come back 401 and be repeated, and a GET spends two of
# them because _request retries a dropped connection once. Four requests, two
# of them GETs, is the ceiling; Claude (two token URLs, then a GET) and Codex
# (a token POST, then a GET) are both shorter.
MAX_SEQUENTIAL_REQUESTS = 8
# Everything a poll can wait on that is not a request: waiting behind another
# run's token refresh, a rate limit's Retry-After, the retry backoff between
# two attempts, and interpreter start.
POLL_OVERHEAD_S = (
    REFRESH_LOCK_WAIT_S + RETRY_AFTER_MAX_S + NETWORK_RETRY_BACKOFF_S + POLL_STARTUP_S
)
CODEX_SESSION_MAX_S = 6 * SECONDS_PER_HOUR
CODEX_TWO_DAY_S = 2 * SECONDS_PER_DAY
CODEX_WEEK_MIN_S = 6 * SECONDS_PER_DAY
CODEX_WEEK_MAX_S = 8 * SECONDS_PER_DAY
CODEX_MONTH_MIN_S = 28 * SECONDS_PER_DAY
CODEX_MONTH_MAX_S = 32 * SECONDS_PER_DAY

# How many decimal places a money amount is counted in. Claude's spend block
# names it (2 for cents, 0 for whole units) and the panel scales the minor
# amount by 10^exponent, so a wire value outside this range would divide a real
# charge by an unplaceable power of ten: Math.pow(10, 1e308) is Infinity in the
# panel and renders the spend as 0.00. Anything unusable is read as cents.
DEFAULT_SPEND_EXPONENT = 2
MIN_SPEND_EXPONENT = 0
MAX_SPEND_EXPONENT = 6
# Longest vendor response body read into memory. Every reading is a few tens of
# kilobytes of JSON, so this is generous by two orders of magnitude; it exists
# because the alternative is a peer that names no length answering with an
# endless body, and plasmashell reads that into its own heap once a poll, every
# poll, until the desktop session is killed by the OOM killer.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

# The status the fetcher raises for itself when the vendor answered and the
# body is not one this module can read: over the cap above, empty, or not JSON.
# Passing the vendor's own 200 on would report a reading that was never
# measured as a success, and the panel then names a status no HTTP client
# sends. 599 is out of every range a vendor answers in; _transient_failure and
# _http_error name it, and the panel has a wording of its own for it.
UNREADABLE_BODY_STATUS = 599
BAD_BODY_ERROR = "bad-body"

# Host that serves the Grok OIDC discovery document. The document names the
# token endpoint, and a refresh token is POSTed there, so an endpoint on any
# other host is not followed.
GROK_OIDC_HOST = "auth.x.ai"

# Request headers that carry the user's access token. urllib copies the whole
# header set onto a redirect target, these two included.
CREDENTIAL_HEADERS = frozenset({"authorization", "cookie"})

# The status for a request the fetcher declined to send, as against the 0 that
# names a request that never got a response. A refused redirect is neither: the
# vendor did answer, with a 3xx, and the 3xx is not a verdict about the account,
# so reporting it as one drops the last good reading over a decision the panel
# made. No HTTP status is negative, and a 3xx is one the panel would render as an
# unavailable provider rather than as the endpoint having moved.
REFUSED_STATUS = -1

# The usage multiplier a Claude rate-limit tier spells ("max_20x").
TIER_MULTIPLIER = re.compile(r"(\d+)x")


# ── configuration ───────────────────────────────────────────────────────────
# Every knob is an environment variable read once at startup and validated
# before any request. See README "Configuration" for the documented set.


@dataclass(frozen=True)
class Config:
    home: Path
    claude_cred: Path
    codex_auth: Path
    grok_auth: Path
    cursor_auth: Path
    cursor_state_db: Path
    cache_dir: Path
    http_timeout_s: float
    cache_max_age_s: int
    account_salt: bytes | None

    @property
    def poll_timeout_s(self) -> float:
        """The longest a poll with this config can be entitled to take.

        The panel drops a run that outlasts its watchdog, so a watchdog shorter
        than this turns a slow but honest poll into an "exec" failure with no
        payload behind it. QUOTA_WIDGET_HTTP_TIMEOUT is settable up to 300 s,
        which by itself is more than a fixed watchdog can hold, so the budget
        travels with the payload instead of being a constant the panel owns.
        """
        return MAX_SEQUENTIAL_REQUESTS * self.http_timeout_s + POLL_OVERHEAD_S

    def describe(self) -> JsonDict:
        """Active values for `--print-config`: paths and knobs, and no
        token is read to produce them. The account key is reported as pinned or
        generated, never as the bytes: whether a run's key is one of its inputs
        is a fact about the run, and the key itself is worth nothing printed."""
        return {
            "home": str(self.home),
            "claude_cred": str(self.claude_cred),
            "codex_auth": str(self.codex_auth),
            "grok_auth": str(self.grok_auth),
            "cursor_auth": str(self.cursor_auth),
            "cursor_state_db": str(self.cursor_state_db),
            "cache_dir": str(self.cache_dir),
            "http_timeout_s": self.http_timeout_s,
            "poll_timeout_s": self.poll_timeout_s,
            "cache_max_age_s": self.cache_max_age_s,
            "account_salt": "pinned" if self.account_salt is not None else "generated",
        }


def _env_value(env: Mapping[str, str], name: str) -> str | None:
    """The stripped value of name, or None when it is unset.

    Unset-but-empty is a config error, not a default: a variable exported
    empty reads as a setting the user made and the fetcher would ignore.
    """
    raw = env.get(name)
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        raise ConfigError(f"{name} is set but empty")
    return value


def _env_path(env: Mapping[str, str], name: str, default: Path) -> Path:
    value = _env_value(env, name)
    if value is None:
        return default
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ConfigError(f"{name} must be an absolute path, got {value!r}")
    return path


def _env_number(
    env: Mapping[str, str], name: str, default: float, maximum: float
) -> float:
    value = _env_value(env, name)
    if value is None:
        return default
    try:
        number = float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc
    if not 0 < number <= maximum:
        raise ConfigError(f"{name} must be in (0, {maximum:g}], got {number:g}")
    return number


def _env_salt(env: Mapping[str, str], name: str) -> bytes | None:
    """The account-salt key this run is to use, or None to make its own.

    A replay has to name the key as well as the clock. Left to draw a fresh
    one, a run whose cache holds no key yet (a fresh install, a sandboxed
    `QUOTA_WIDGET_CACHE`, a run after `--clear-cache`, a cache directory that
    cannot be written) digests every account under bytes no second run draws,
    so the same inputs produce a different `account` in every provider card
    and the run cannot be reproduced from them.

    Unset in production, where the key is 32 bytes of `os.urandom` kept beside
    the entries it scopes. Set to a literal, the value is a test and smoke
    input: it is used, never written, so nothing persists it past the run and
    an unpinned poll keeps the random key it would have had.
    """
    value = _env_value(env, name)
    if value is None:
        return None
    try:
        key = bytes.fromhex(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be hex, got {value!r}") from exc
    if len(key) != ACCOUNT_SALT_BYTES:
        raise ConfigError(
            f"{name} must be {ACCOUNT_SALT_BYTES * 2} hex characters "
            f"({ACCOUNT_SALT_BYTES} bytes), got {len(value)}"
        )
    return key


def _xdg_dir(env: Mapping[str, str], name: str, default: Path) -> Path:
    """XDG base directory; a relative value is invalid, so the default stands.

    https://specifications.freedesktop.org/basedir-spec/latest/
    """
    raw = env.get(name)
    if not raw:
        return default
    path = Path(raw)
    return path if path.is_absolute() else default


def _env_seconds(env: Mapping[str, str], name: str, default: int, maximum: int) -> int:
    """Whole seconds. A fraction would truncate to 0 and silently disable the
    cache the caller asked to shorten, so it is rejected instead."""
    value = _env_value(env, name)
    if value is None:
        return default
    try:
        seconds = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be whole seconds, got {value!r}") from exc
    if not 0 < seconds <= maximum:
        raise ConfigError(f"{name} must be in (0, {maximum}], got {seconds}")
    return seconds


def _cursor_config_root(env: Mapping[str, str], home: Path) -> Path:
    if sys.platform == "darwin":
        return home / "Library" / "Application Support"
    if os.name == "nt":
        appdata = env.get("APPDATA")
        return Path(appdata) if appdata else home / "AppData" / "Roaming"
    return _xdg_dir(env, "XDG_CONFIG_HOME", home / ".config")


def _cursor_state_db(env: Mapping[str, str], home: Path) -> Path:
    return (
        _cursor_config_root(env, home)
        / "Cursor"
        / "User"
        / "globalStorage"
        / "state.vscdb"
    )


_CONFIG: Config | None = None
# Guards the lazy load in config(). main() publishes the config before the
# provider pool starts, so a poll reads it and takes nothing; a caller that
# reaches a provider without that would otherwise have every provider thread
# build and publish the module global at the same time.
_CONFIG_LOCK = threading.Lock()
# Same shape for the account-salt key: the four provider threads each digest
# an account, and only one of them may create the key.
_ACCOUNT_SALT: bytes | None = None
_SALT_LOCK = threading.Lock()


def _home(env: Mapping[str, str]) -> Path:
    """The home directory, or ConfigError when the environment has none.

    Path.home() raises RuntimeError, which is not a ConfigError, so it would
    abort the run before main() could emit a payload and the panel would go
    blank instead of showing "config".
    """
    try:
        return Path.home()
    except RuntimeError as exc:
        raise ConfigError(f"cannot resolve the home directory: {exc}") from exc


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Read and validate the environment. Raises ConfigError on bad values."""
    values = os.environ if env is None else env
    unknown = _unknown_env(values)
    if unknown is not None:
        raise ConfigError(
            f"{unknown} is not a knob this fetcher reads; "
            f"known: {', '.join(sorted(KNOWN_ENV))}"
        )
    home = _env_path(values, ENV_HOME, _home(values))
    cache_base = _xdg_dir(values, "XDG_CACHE_HOME", home / ".cache")
    timeout = _env_number(
        values, ENV_HTTP_TIMEOUT, DEFAULT_HTTP_TIMEOUT_S, MAX_HTTP_TIMEOUT_S
    )
    max_age = _env_seconds(
        values, ENV_CACHE_MAX_AGE_S, DEFAULT_CACHE_MAX_AGE_S, MAX_CACHE_MAX_AGE_S
    )
    # A malformed clock override would otherwise raise from now_ms() in the
    # middle of the poll, after the panel has already been told nothing.
    if NOW_MS_ENV in values:
        _pinned_ms(values[NOW_MS_ENV])
    global _CONFIG
    _CONFIG = Config(
        home=home,
        claude_cred=_env_path(
            values,
            ENV_CLAUDE_CREDENTIALS,
            home / ".claude" / ".credentials.json",
        ),
        codex_auth=_env_path(values, ENV_CODEX_AUTH, home / ".codex" / "auth.json"),
        grok_auth=_env_path(values, ENV_GROK_AUTH, home / ".grok" / "auth.json"),
        cursor_auth=_env_path(
            values,
            ENV_CURSOR_AUTH,
            _cursor_config_root(values, home) / "cursor" / "auth.json",
        ),
        cursor_state_db=_env_path(
            values, ENV_CURSOR_STATE_DB, _cursor_state_db(values, home)
        ),
        cache_dir=_env_path(values, ENV_CACHE, cache_base / "quota-widget"),
        http_timeout_s=timeout,
        cache_max_age_s=max_age,
        account_salt=_env_salt(values, ENV_ACCOUNT_SALT),
    )
    return _CONFIG


def config() -> Config:
    """The validated configuration. main() loads it before any provider runs."""
    loaded = _CONFIG
    if loaded is None:
        with _CONFIG_LOCK:
            loaded = _CONFIG if _CONFIG is not None else load_config()
    return loaded


def _use_utf8_streams() -> None:
    """Write the payload and the journal as UTF-8 whatever the locale says.

    plasmashell hands the fetcher the session environment, and a session that
    exported no LANG (a user unit, a nested Plasma session) leaves the streams
    on ASCII where the runtime cannot coerce C to C.UTF-8. The payload is
    ASCII, since json.dumps escapes what it cannot spell, but a warning
    carries a credential path and vendor text, and the UnicodeEncodeError
    printing one raises aborts the poll before emit() runs, so the panel is
    left with no payload at all. A captured stream (the test suite) has no
    reconfigure; it needs no fixing.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError):  # stream already detached
                reconfigure(encoding=JSON_ENCODING)


def emit(obj: JsonDict) -> None:
    """Print the one payload plasmashell reads and exit.

    stdout is the panel's only channel and it is parsed as a whole, so every
    exit path goes through here rather than falling off the end of main.
    """
    print(json.dumps(obj, separators=(",", ":")))
    raise SystemExit(0)


def _redacted_homes() -> list[str]:
    """The home directories a printed line must not spell out.

    The configured home is read from the module global rather than through
    config(), which is still mid-load on the path that reports a bad value and
    would raise a second time inside the reporting.
    """
    homes: list[str] = []
    with contextlib.suppress(RuntimeError):
        homes.append(str(Path.home()))
    loaded = _CONFIG
    if loaded is not None:
        homes.append(str(loaded.home))
    return [home for home in homes if home not in ("", os.sep)]


def _redact(text: str) -> str:
    """A printed line with the home directory spelled `~`.

    Every path a warning, a config error, or an exception text carries is
    under the home directory, and that path's first component is the account
    name. The journal keeps the line long after the poll wrote it, and the
    panel shows a config error until the next poll, so the name outlives the
    run and reaches whoever reads either. `~` still says which file failed,
    which is the part an operator acts on.
    """
    for home in _redacted_homes():
        text = text.replace(home + os.sep, "~" + os.sep).replace(home, "~")
    return text


def warn(message: str) -> None:
    """Report a condition the JSON payload cannot carry.

    stdout is the panel's only channel, so anything that needs an operator's
    attention (a dropped credential write, a swallowed provider crash) goes to
    stderr and lands in the journal next to the plasmashell run that caused it.
    """
    print(f"fetch_quota: {_redact(message)}", file=sys.stderr)


def warn_traceback(exc: BaseException) -> None:
    """Print a crash's traceback with the home directory spelled `~`.

    traceback.print_exc writes to stderr itself, past warn() and its
    redaction, and every frame names a file under the checkout, which sits
    under the home directory on every install path. The journal keeps the line
    long after the poll wrote it, so the account name the path's first
    component carries would outlive the run in it.
    """
    print(
        _redact("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))),
        file=sys.stderr,
        end="",
    )


def iso_to_utc(value: str) -> dt.datetime | None:
    """Parse an ISO 8601 timestamp to an aware UTC datetime, or None.

    A payload timestamp without an offset is UTC: that is what the providers
    write. fromisoformat returns it naive, and .timestamp() on a naive value
    resolves it in the host's zone, so the same reading lands hours off on a
    plasmashell running anywhere west of Greenwich.
    """
    # 3.11 reads a trailing "Z" itself; the rewrite keeps the value parseable
    # when a vendor sends a shape fromisoformat rejects.
    stamp = value.strip().replace("Z", "+00:00")
    try:
        when = dt.datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        return when.replace(tzinfo=dt.UTC)
    return when.astimezone(dt.UTC)


def epoch_ms(value: float) -> int:
    """Epoch-ms from a number the vendor sent in seconds or in milliseconds.

    The units are not one across the providers (Claude writes `expiresAt` in
    milliseconds, Codex `reset_at` in seconds), so a value past the cutoff is
    read as milliseconds. That cutoff is 10^10: 2286-11-20 in seconds, and
    1970-01-01T02:46 in milliseconds, so no instant this widget shows falls
    on the wrong side of it.
    """
    return int(value) if value > MS_EPOCH_CUTOFF else ms_from_seconds(value)


def iso_to_ms(value: object) -> int | None:
    """Epoch-ms from a vendor timestamp, or None.

    A payload carries the instant either as an ISO-8601 string or as a bare
    epoch number, and reading only the string form dropped the reset of every
    response that sent a number, which reads as an absent date rather than as
    a wrong one.

    None covers a missing, malformed, or out-of-range value alike: a reset the
    widget cannot read is shown as absent, never as a bogus date.
    """
    if isinstance(value, str):
        if not value.strip():
            return None
        when = iso_to_utc(value)
        if when is None:
            return None
        try:
            return ms_from_seconds(when.timestamp())
        except (OSError, OverflowError, ValueError):
            return None
    number = _finite_number(value)
    return None if number is None else epoch_ms(number)


def plan_label(subscription: str | None, tier: str | None) -> str:
    """Map Claude credential fields to the website plan name."""
    sub = (subscription or "").lower()
    tier = (tier or "").lower()

    m = TIER_MULTIPLIER.search(tier)
    mult = m.group(1) if m else None

    if "max" in sub or "max" in tier:
        return f"Max ({mult}x)" if mult else "Max"
    if "pro" in sub or "pro" in tier:
        return "Pro"
    if "team" in sub:
        return "Team"
    if "enterprise" in sub:
        return "Enterprise"
    if sub:
        return _label_text(sub).replace("_", " ").title() or "Claude"
    return "Claude"


def parse_retry_after(value: str | None) -> float | None:
    """Seconds to wait from a Retry-After header (delta-seconds or HTTP-date).

    A header is the vendor's to shape, so anything that is not a plain
    string, and any value that is not a finite number of seconds, reads as
    no wait at all. The result feeds sleep(), where an infinity or a raise
    would cost the poll far more than the retry it was meant to cover.
    """
    if not isinstance(value, str) or not value:
        return None
    s = value.strip()
    if not s:
        return None
    try:
        number = float(s)
    except ValueError:
        pass  # not delta-seconds; try HTTP-date next
    else:
        # float() reads "inf" and "nan", and a wait on either is a wait that
        # never ends (time.sleep raises on an infinity) or no wait at all,
        # neither of which is a number of seconds a header can name.
        return max(0.0, number) if math.isfinite(number) else None
    try:
        when = email.utils.parsedate_to_datetime(s)
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.UTC)
        wait = (when - now_utc()).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return None  # HTTP-date present but not parseable
    return max(0.0, wait) if math.isfinite(wait) else None


def _fsync_dir(path: Path) -> None:
    """Flush a directory entry so a completed rename survives a crash."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return  # directory not openable (Windows, permissions); rename still ordered
    try:
        os.fsync(fd)
    except OSError:
        pass  # some filesystems reject fsync on a directory
    finally:
        os.close(fd)


def _atomic_write_json(path: Path, obj: Any) -> None:
    """Replace path with obj as JSON, durably.

    Content is fsynced before the rename and the directory after it, so a crash
    can leave the old file or the new one, never a truncated token store.
    """
    fd, tmp = tempfile.mkstemp(
        prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        # UTF-8, not the locale encoding: a non-ASCII plan label would fail
        # mid-write on a C locale and take the whole poll with it. newline
        # pins LF so the file reads the same on every platform.
        with os.fdopen(fd, "w", encoding=JSON_ENCODING, newline="\n") as f:
            json.dump(obj, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        Path(tmp).chmod(FILE_MODE_PRIVATE)
        Path(tmp).replace(path)
    except BaseException:
        # Any failure, not only OSError: an encoding or serialization error
        # would otherwise leave a temp file in the token store for every poll.
        with contextlib.suppress(OSError):
            Path(tmp).unlink()
        raise
    _fsync_dir(path.parent)


def _merge_write_json(
    path: Path, update: MergeUpdate, base: JsonDict | None = None
) -> None:
    """Apply update to the JSON object at path without losing a concurrent write.

    The token stores are shared with the vendor CLIs, so a read-modify-write can
    land on top of a refresh another process just committed. Re-reading until our
    value survives keeps that refresh instead of dropping it on the floor, which
    would sign the user out of the CLI as well as the widget. base is the store the
    caller already holds, used when the file itself cannot be read.

    An attempt that cannot read the store back and one whose value was replaced
    are different failures with different fixes, so the last reason is kept and
    named: "another writer" sends the operator looking at a race that is not
    there, and a store that cannot be read at all is the reason the value did
    not survive.
    """
    reason = "another writer replaced the value each time"
    for _ in range(MERGE_WRITE_ATTEMPTS):
        try:
            current = json.loads(_read_text(path))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            current = dict(base) if base is not None else {}
        if not isinstance(current, dict):
            current = dict(base) if base is not None else {}
        key, value = update(current)
        _atomic_write_json(path, current)
        try:
            after = json.loads(_read_text(path))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            reason = f"the store could not be read back: {exc}"
            continue
        if isinstance(after, dict) and after.get(key) == value:
            return
    # A writer kept winning the race; it holds the rotated token itself, so the
    # tokens in memory stay usable for this poll. The write is lost either way,
    # so name the file and the reason: the next poll otherwise repeats the
    # refresh with no hint at what is going wrong.
    warn(f"gave up writing {path} after {MERGE_WRITE_ATTEMPTS} attempts: {reason}")


def _write_rotated_tokens(
    provider: str, path: Path, update: MergeUpdate, base: JsonDict
) -> None:
    """Persist a rotated token store, naming the file when the write fails.

    The poll still runs on the in-memory token, but a store left holding one
    the provider has already retired makes the next poll refresh again, and
    signs the user out of the vendor CLI along with the widget.
    """
    try:
        _merge_write_json(path, update, base)
    except OSError as exc:
        warn(f"{provider} token rotated but {path} was not written: {exc}")


def _salt_on_disk(path: Path) -> bytes | None:
    store = _read_json_dict(path)
    raw = store.get("salt") if store else None
    if not isinstance(raw, str):
        return None
    try:
        value = bytes.fromhex(raw)
    except ValueError:
        return None
    return value if len(value) == ACCOUNT_SALT_BYTES else None


def _install_salt(path: Path, salt: bytes) -> None:
    """Put salt at path unless another run installed one first.

    The exclusive create is the claim: a second run cannot create the file, so
    it never overwrites the key the first one installed, and the first one
    never has to notice a second. That is the whole property, since the key is
    the one every entry on disk was scoped under and a run that minted its own
    would strand every entry written by the run it raced with.

    A run killed between the create and the write leaves a file no entry was
    scoped under, so a file that holds nothing readable is replaced here.
    """
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, FILE_MODE_PRIVATE)
    except FileExistsError:
        if _salt_on_disk(path) is None:
            _atomic_write_json(path, {"salt": salt.hex()})
        return
    try:
        with os.fdopen(fd, "w", encoding=JSON_ENCODING, newline="\n") as f:
            json.dump({"salt": salt.hex()}, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        # A key file nobody can read is no better than none, and the next run
        # replaces it; leaving this one would pin every digest to a key the
        # file never states.
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    _fsync_dir(path.parent)


def _orphan_entries(folder: Path) -> bool:
    """Whether the cache holds an entry that no key on disk can scope.

    A restore that brought the entries out of the directory and left the key
    behind, or a key lost on its own, leaves readings whose digest was taken
    under a key this run does not have. `_read_provider_cache` reports those
    exactly as it reports another account's entry, so this is the only place
    the difference is visible.
    """
    try:
        names = [path.name for path in folder.iterdir()]
    except OSError:
        return False
    return any(
        name.endswith(ENTRY_SUFFIX) and not _is_lock_file(name) for name in names
    )


def _load_or_create_salt() -> bytes:
    folder = config().cache_dir
    path = folder / ACCOUNT_SALT_NAME
    on_disk = _salt_on_disk(path)
    if on_disk is not None:
        return on_disk
    pinned = config().account_salt
    if pinned is not None:
        # A key the run names is an input to it, not a key of the machine, so
        # it is used and not written. The file, when one exists, still wins:
        # the entries on disk were taken under that key, and a poll that
        # renamed them would orphan every one of them.
        return pinned
    fresh = os.urandom(ACCOUNT_SALT_BYTES)
    try:
        _private_dir(folder)
        if _orphan_entries(folder):
            warn(
                f"{folder} holds cache entries but no {ACCOUNT_SALT_NAME}, so they "
                "were taken under a key that is not here and this run cannot read "
                "them; the panel falls back to a live poll with no other sign of it"
            )
        _install_salt(path, fresh)
    except OSError as exc:
        # A cache directory that cannot be written holds no entries to scope
        # either, so a key that lives only in this process costs no cache hit
        # and no scoping. It is still a key: the digest never leaves the run
        # that took it, and the next poll with a writable cache reads the
        # file's instead. The consequence is a cache no later poll can read
        # back, so it is named rather than passed over: the same condition on
        # the entry write is reported, and a poll that silently never keeps a
        # reading looks exactly like four providers failing every time.
        warn(
            f"could not write the account key at {path}: {exc}; the digest is "
            "drawn again on every poll, so cached readings are never read back"
        )
        return fresh
    # Two polls can reach this together. Whatever is on disk when we look is
    # what the entries already written were taken under, so the file decides
    # and the loser of the race adopts the winner's key.
    return _salt_on_disk(path) or fresh


def _account_salt() -> bytes:
    """The per-installation key every account digest is taken under."""
    global _ACCOUNT_SALT
    salt = _ACCOUNT_SALT
    if salt is not None:
        return salt
    with _SALT_LOCK:
        if _ACCOUNT_SALT is None:
            _ACCOUNT_SALT = _load_or_create_salt()
        return _ACCOUNT_SALT


def _digest(value: str | None) -> str | None:
    """Stable 16-hex id for one account, or None if the value names no account.

    The value is a `sub` claim or a vendor account id, so it is text off the
    wire: it is normalized (an NFD spelling must key the same cache scope as
    its NFC twin), encoded by name, and a value that cannot be encoded at all
    (a JSON "\\ud800" escape decodes to a lone surrogate) reads as no id. A
    crash here would be caught as a provider failure and reported to the panel
    as a network error, costing the user the whole card over a digest.

    The digest is taken under the per-installation key. Unsalted, it would be
    only as private as the id space behind it: a vendor account id is a short
    enumerable value, and 16 hex of SHA-256 over a guessable space is a
    lookup rather than a hash, so a copy of this cache directory would give up
    the WorkOS user id behind every entry in it. Scoping only ever compares
    two digests taken under the same key, so the key changes nothing about
    who reads what, and the panel compares them the same way.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        normalized = unicodedata.normalize(NORMALIZATION_FORM, value)
        keyed = _account_salt() + b"\x00" + normalized.encode(JSON_ENCODING)
        return hashlib.sha256(keyed).hexdigest()[:16]
    except UnicodeEncodeError:
        return None


def _account_id(access_token: str | None, fallback: str | None = None) -> str | None:
    """Account id the provider cache is scoped to, or None if unknowable.

    The token's `sub` claim survives access-token rotation, so a refresh does
    not orphan the entry; a digest of the token itself would. `fallback` is
    only for providers that publish an account id outside the token.
    """
    sub = _jwt_claim(access_token, "sub") if isinstance(access_token, str) else None
    return _digest(sub if isinstance(sub, str) and sub else fallback)


def _private_dir(folder: Path) -> None:
    """Create the cache folder, and close it up when it already existed.

    mkdir's mode is the mode of the directory it creates and nothing else, so
    a cache folder left behind by an earlier run, or created by whatever else
    owns XDG_CACHE_HOME, keeps a mode that lets every local account read the
    readings and the account digests in it.
    """
    folder.mkdir(parents=True, mode=CACHE_DIR_MODE, exist_ok=True)
    with contextlib.suppress(OSError):
        if folder.stat().st_mode & 0o777 != CACHE_DIR_MODE:
            folder.chmod(CACHE_DIR_MODE)


def _discard_provider_cache(path: Path) -> None:
    """Delete one cache file. Best-effort: a leftover file is unreadable anyway."""
    with contextlib.suppress(OSError):
        path.unlink()


def _reading(payload: JsonDict) -> JsonDict:
    """Stamp a payload with the instant the reading was taken. Without it a
    consumer can only age a value by when it arrived, which resets on every
    replay from the cache and stacks a second stale window on the first."""
    payload["fetched_ms"] = now_ms()
    return payload


def _read_provider_cache(name: str, account: str | None) -> JsonDict | None:
    path = config().cache_dir / f"{name}{ENTRY_SUFFIX}"
    with _entry_lock(path):
        obj = _read_json_dict(path)
        if obj is None:
            return None
        ts = _finite_number(obj.get("cached_ms"))
        payload = obj.get("payload")
        if ts is None or not isinstance(payload, dict):
            return None
        # An entry another release wrote is a payload this QML was not built
        # against, and the panel reads it field by field. It is deleted rather
        # than parsed, so an upgrade never serves the previous shape.
        if obj.get("schema") != PAYLOAD_SCHEMA:
            _discard_provider_cache(path)
            return None
        if not payload.get("ok"):
            return None
        # The window is a retention rule, not a serving rule, so it is applied
        # before the account check: an entry that is past it is deleted whoever
        # asks for it. A provider whose account changed, or whose credential went
        # away so no poll carries a digest at all, is exactly the entry that is
        # never read again under the account that wrote it, and leaving it to
        # sit on disk forever is how a 24 h reading outlives its 24 h window.
        # The unlink runs under the same lock as the write, so a poll that
        # renamed a fresh entry in while this read was deciding does not lose
        # the fresh one to a verdict computed against the old inode.
        if now_ms() - int(ts) > config().cache_max_age_s * 1000:
            _discard_provider_cache(path)
            return None
        # An entry belongs to the account whose credential produced it. Reading
        # another account's plan and usage is worse than showing nothing, so an
        # unidentifiable caller reads nothing.
        if account is None or obj.get("account") != account:
            return None
        # The reading is as old as the write, not as fresh as this read.
        return {**payload, "fetched_ms": int(ts)}


def _cache_holds_newer(path: Path, taken_ms: int, account: str) -> bool:
    """True when the entry on disk holds a reading no older than taken_ms.

    A run the panel dropped for outliving pollTimeoutMs can still land its
    write after the poll that replaced it, and its reading is the older one.
    An entry is stamped with the instant its reading was taken, so a stamp at
    or past taken_ms names a reading no older than the incoming one and
    writing again only rewinds it.
    """
    obj = _read_json_dict(path)
    if obj is None:
        return False
    ts = _finite_number(obj.get("cached_ms"))
    if (
        ts is None
        or obj.get("account") != account
        or obj.get("schema") != PAYLOAD_SCHEMA
    ):
        return False
    return int(ts) >= taken_ms


def _write_provider_cache(name: str, payload: JsonDict, account: str | None) -> None:
    if not payload.get("ok") or account is None:
        return
    folder = config().cache_dir
    path = folder / f"{name}{ENTRY_SUFFIX}"
    try:
        _private_dir(folder)
        # The stamp is the instant the reading was taken, not the one the write
        # happened at: the entry is read back under this number, so stamping it
        # later would age the reading by its own arrival and let it outlive the
        # retention window.
        taken = _finite_number(payload.get("fetched_ms"))
        stamp = int(taken) if taken is not None else now_ms()
        with _entry_lock(path):
            if _cache_holds_newer(path, stamp, account):
                return
            _atomic_write_json(
                path,
                {
                    "schema": PAYLOAD_SCHEMA,
                    "cached_ms": stamp,
                    "account": account,
                    "payload": payload,
                },
            )
    except OSError as exc:
        # Cache is best-effort: a full disk must not fail the poll. It is also
        # the only fallback a later poll has when the vendor API fails, so the
        # operator needs to know it is not being written.
        warn(f"could not write the {name} cache entry: {exc}")


def _stale_cache(name: str, account: str | None) -> JsonDict | None:
    cached = _read_provider_cache(name, account)
    if not cached:
        return None
    out = dict(cached)
    out["stale"] = True
    return out


def _fail_or_cached(name: str, account: str | None, status: int) -> JsonDict:
    """The payload for a provider call that did not return a reading.

    A transient failure serves the last good reading from the cache, so a
    machine that is offline keeps the card the panel would otherwise blank. A
    401 or 403 is a vendor decision and is reported as one, cache or no cache.
    """
    if _transient_failure(status):
        cached = _stale_cache(name, account)
        if cached:
            return cached
    return _http_error(status, account)


def _is_lock_file(name: str) -> bool:
    """True for the advisory locks beside the cache, which hold no reading.

    refresh.lock and every <entry>.json.lock end in the same suffix, so one
    test names the whole set.
    """
    return name.endswith(ENTRY_LOCK_SUFFIX)


def _flock_wait(fd: int, wait_s: float, poll_s: float) -> bool:
    """Take an exclusive flock on fd, or give up and say so.

    Only "somebody else holds it" is worth waiting out. A filesystem that
    cannot lock (a network mount, an overlay without ENOLCK support) raises for
    every try, so polling that one burns the whole deadline on every call
    instead of falling through to the unguarded work below.
    """
    if fcntl is None:
        return False  # no flock on this platform: the body runs unguarded
    deadline = monotonic() + wait_s
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            if exc.errno not in LOCK_BUSY_ERRNOS:
                return False
            if monotonic() >= deadline:
                return False
            sleep(poll_s)


@contextlib.contextmanager
def _flock_file(path: Path, wait_s: float, poll_s: float) -> Iterator[bool]:
    """Hold an exclusive flock on path for the body, yielding whether it is held.

    flock is per open file description, so two threads in one process contend
    here exactly as two processes do. A platform without fcntl, and a directory
    that cannot be written, both yield False: the body runs unguarded, not
    never. The lock lives in its own file because the entry it guards is
    replaced by a rename, and a lock on the old inode guards nothing.
    """
    if fcntl is None:
        yield False
        return
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, FILE_MODE_PRIVATE)
    except OSError:
        yield False
        return
    try:
        held = _flock_wait(fd, wait_s, poll_s)
        try:
            yield held
        finally:
            if held:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.contextmanager
def _refresh_lock() -> Iterator[None]:
    """Serialize OAuth refreshes between concurrent runs of the fetcher.

    Every widget instance, the install smoke run, and any manual invocation
    share one credential file per provider, and refreshes rotate the refresh
    token. Two runs refreshing at once leave the loser holding a token the
    provider already retired. The lock spans the credential re-read too, so
    the second run sees the rotated state and skips the round trip.
    """
    try:
        folder = config().cache_dir
        _private_dir(folder)
        lock_path: Path | None = folder / REFRESH_LOCK_NAME
    except OSError:
        lock_path = None
    if lock_path is None:
        yield  # unwritable cache dir: refresh unguarded, not never
        return
    with _flock_file(lock_path, REFRESH_LOCK_WAIT_S, REFRESH_LOCK_POLL_S):
        yield


@contextlib.contextmanager
def _entry_lock(entry: Path) -> Iterator[None]:
    """Serialize the read-check-write on one provider cache entry.

    The entry only ever moves forward, but that is a comparison and a write
    with nothing between them: two runs that both read the older stamp before
    either renames, then write in the order they happen to reach the rename,
    leave the older reading on disk. The second widget instance, an install
    smoke run beside a poll, or a poll whose answer the panel dropped for
    outliving pollTimeoutMs are all two runs at once, and the last of them is
    the one whose reading is stale.
    """
    with _flock_file(
        entry.with_name(entry.name + ENTRY_LOCK_SUFFIX),
        ENTRY_LOCK_WAIT_S,
        ENTRY_LOCK_POLL_S,
    ):
        yield


def _origin(url: str) -> tuple[str, str, int | None] | None:
    """(scheme, host, port) a request is aimed at, or None if it has no host."""
    # Reading .port raises ValueError on a port that is not a number or is out
    # of range, and both URLs here come off the wire: a Location header names
    # the port, and so does an OIDC discovery document's token_endpoint. An
    # unparsable port is a URL with no usable origin, not an exception the
    # caller has to catch around every comparison.
    try:
        parts = urllib.parse.urlsplit(url)
        if not parts.hostname:
            return None
        return (parts.scheme.lower(), parts.hostname.lower(), parts.port)
    except ValueError:
        return None


class _RefusedRedirect(urllib.error.HTTPError):
    """A redirect that would have carried a credential off its origin.

    An HTTPError because urllib raises and unwinds through this handler's own
    machinery to abandon the request, and the transport reports the refusal as
    a status like any other. Its own type is what fetch_http catches ahead of
    the vendor's own HTTPError, so the refusal is not mistaken for the 3xx the
    vendor sent.
    """


class _OriginBoundRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect that would carry a credential to another origin.

    Every call this module makes is to a fixed vendor endpoint with the user's
    token in a header: a Bearer for Claude, Codex, and Grok, a session cookie
    for Cursor. urllib's default handler copies the request headers onto the
    redirected request, so whichever host a 30x names is handed that token, and
    the vendor's own infrastructure does not have to be compromised for that to
    happen. A redirect that stays on the origin is followed as before; one that
    leaves it is reported as the failure it is, and the token stays where it
    was sent.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        carries_credential = any(
            name.lower() in CREDENTIAL_HEADERS for name in req.headers
        )
        if carries_credential and _origin(req.full_url) != _origin(newurl):
            raise _RefusedRedirect(newurl, code, msg, headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# urlopen() builds a default global opener on first use, so the handler above is
# only the one that runs if the opener carrying it is installed.
urllib.request.install_opener(urllib.request.build_opener(_OriginBoundRedirect()))


def fetch_http(
    url: str,
    headers: dict[str, str],
    *,
    data: bytes | None = None,
    method: str | None = None,
) -> tuple[int, object, Message | None]:
    """Return (status, decoded JSON or None, response headers).

    A transport failure is status 0. A body that arrived but is not a readable
    reading is UNREADABLE_BODY_STATUS, never the vendor's own 200: a 200 with
    no payload in it is not a success, and reporting it as one leaves the
    panel reading a status no HTTP client sends. The cause reaches the journal,
    so an offline panel is diagnosable without rerunning the fetcher by hand;
    it never reaches stdout, which the panel parses as the only payload.
    """
    # Every caller passes either an https vendor constant or a token endpoint
    # _is_grok_token_url has already checked, so the scheme is settled before
    # here and the URL is never user input.
    req = urllib.request.Request(  # noqa: S310 (https URL, never user input)
        url, data=data, headers=headers, method=method
    )
    attempts = 1 if data is not None or method not in (None, "GET") else 2
    timeout = config().http_timeout_s
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(  # noqa: S310 (https URL, never user input)
                req, timeout=timeout
            ) as resp:
                # One byte past the cap: reading exactly the cap cannot tell a
                # body that fits from one that is only just over it.
                body = resp.read(MAX_RESPONSE_BYTES + 1)
                hdrs = resp.headers
                if len(body) > MAX_RESPONSE_BYTES:
                    warn(
                        f"{url} returned over {MAX_RESPONSE_BYTES} bytes; "
                        "the body was not read past the cap"
                    )
                    return UNREADABLE_BODY_STATUS, None, hdrs
                if not body:
                    return UNREADABLE_BODY_STATUS, None, hdrs
                try:
                    return resp.status, json.loads(body.decode("utf-8")), hdrs
                except ValueError:
                    # ValueError, not JSONDecodeError: an integer literal past
                    # sys.get_int_max_str_digits() makes json.loads raise the
                    # plain one, and it left the provider with an unhandled
                    # exception and a "net" card where the vendor answered 200.
                    warn(f"{url} returned {resp.status} with a non-JSON body")
                    return UNREADABLE_BODY_STATUS, None, hdrs
        except _RefusedRedirect:
            # Ahead of the HTTPError clause below, which is a subclass of: the
            # vendor's 3xx is a verdict about the endpoint and this is a verdict
            # about the credential, and only this fetcher decides the second.
            warn(f"{url} redirected off its origin; the request was not sent")
            return REFUSED_STATUS, None, None
        except urllib.error.HTTPError as e:
            # The error body can carry account identifiers (email, user id)
            # echoed back by the vendor. No caller reads it, so the body is
            # drained and discarded rather than returned or logged.
            hdrs = e.headers if e.headers is not None else Message()
            # The error response owns a socket; reading it is not closing it.
            # Bounded like the success path: a 429 or a 5xx is the response a
            # peer chooses to send at length, and this body is thrown away.
            try:
                with contextlib.closing(e):
                    e.read(MAX_RESPONSE_BYTES + 1)
            except OSError as exc:
                warn(
                    f"{url} returned {e.code} and its error body was unreadable: {exc}"
                )
            return e.code, None, hdrs
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            if attempt + 1 < attempts:
                sleep(NETWORK_RETRY_BACKOFF_S)
                continue
            warn(f"{url} failed after {attempts} attempt(s): {reason!r}")
            return 0, None, None
    raise AssertionError("unreachable: the loop returns or sleeps on every path")


def fetch_json(
    url: str,
    headers: dict[str, str],
    *,
    data: bytes | None = None,
    method: str | None = None,
) -> tuple[int, object]:
    status, body, _hdrs = fetch_http(url, headers, data=data, method=method)
    return status, body


# ── Claude ──────────────────────────────────────────────────────────────────


def _claude_expired(oauth: JsonDict) -> bool:
    exp = _finite_number(oauth.get("expiresAt"))
    if exp is None:
        return False
    if exp > MS_EPOCH_CUTOFF:
        ts_ms: int | None = int(exp)
    else:
        ts_ms = ms_from_seconds_or_none(exp)
    if ts_ms is None:
        return False
    return ts_ms <= now_ms() + TOKEN_SKEW_MS


def _refresh_claude(cred: JsonDict) -> tuple[JsonDict | None, bool]:
    """Refresh Claude Code OAuth and write the rotated tokens back.

    Returns the store to poll with, and whether the provider rate-limited the
    refresh. The caller needs both: a usage call that comes back 401 after a
    refresh the provider throttled is not a sign-out, while one that follows a
    refresh the provider rejected is.
    """
    with _refresh_lock():
        latest = _read_json_dict(config().claude_cred)
        if latest is not None:
            cred = latest
        oauth = cred.get("claudeAiOauth")
        if not isinstance(oauth, dict):
            return None, False
        refresh = oauth.get("refreshToken")
        if not isinstance(refresh, str) or not refresh:
            return None, False
        if not _claude_expired(oauth):
            return cred, False  # a concurrent run rotated it while we waited

        body = json.dumps(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": CLAUDE_CLIENT_ID,
            }
        ).encode(JSON_ENCODING)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        tok: Any = None  # OAuth token JSON; fields vary by host
        rate_limited = False
        last_status = 0
        for url in CLAUDE_TOKEN_URLS:
            status, tok = fetch_json(url, headers, data=body, method="POST")
            last_status = status
            if status == 200 and isinstance(tok, dict) and tok.get("access_token"):
                break
            if status in (400, 401):
                warn(f"claude rejected the refresh at {url} with {status}")
                return None, False
            rate_limited = rate_limited or status == 429
            tok = None
        if not isinstance(tok, dict) or not tok.get("access_token"):
            warn(
                "claude answered the refresh with "
                f"{last_status or 'no response'} and no access token"
            )
            return None, rate_limited

        new_oauth = dict(oauth)
        new_oauth["accessToken"] = tok["access_token"]
        if tok.get("refresh_token"):
            new_oauth["refreshToken"] = tok["refresh_token"]
        expires_in = _finite_number(tok.get("expires_in"))
        expires_at = ms_from_seconds_or_none(expires_in) if expires_in else None
        if expires_at is not None:
            new_oauth["expiresAt"] = now_ms() + expires_at
        new_cred = dict(cred)
        new_cred["claudeAiOauth"] = new_oauth

        def put_oauth(store: JsonDict) -> tuple[str, Any]:
            store["claudeAiOauth"] = new_oauth
            return "claudeAiOauth", new_oauth

        _write_rotated_tokens("claude", config().claude_cred, put_oauth, new_cred)
        return new_cred, rate_limited


def _claude_is_session(item: JsonDict) -> bool:
    return item.get("kind") == "session" or item.get("group") == "session"


def _claude_weekly(data: JsonDict) -> list[JsonDict]:
    """Weekly meters, preferring the structured `limits` array over legacy keys."""
    limits = data.get("limits")
    weekly: list[JsonDict] = []
    if isinstance(limits, list) and limits:
        # The structured list matches the website list, including scoped bars.
        for item in limits:
            if not isinstance(item, dict) or _claude_is_session(item):
                continue
            kind = str(item.get("kind") or "")
            scope = _as_dict(item.get("scope"))
            label = "All models"
            if scope.get("surface"):
                label = _label_text(scope["surface"]) or "All models"
            elif _as_dict(scope.get("model")).get("display_name"):
                label = (
                    _label_text(_as_dict(scope["model"])["display_name"])
                    or "All models"
                )
            if kind == "weekly_all":
                label = "All models"
            weekly.append(
                {
                    "label": label,
                    "util": _finite_number(item.get("percent")),
                    "resets_ms": iso_to_ms(item.get("resets_at")),
                }
            )
            if len(weekly) >= MAX_WEEKLY_LIMITS:
                break
        return weekly
    for key, label in (
        ("seven_day", "All models"),
        ("seven_day_opus", "Opus"),
        ("seven_day_sonnet", "Sonnet"),
        ("seven_day_cowork", "Cowork"),
    ):
        block = _as_dict(data.get(key))
        if not block:
            continue
        weekly.append(
            {
                "label": label,
                "util": _finite_number(block.get("utilization")),
                "resets_ms": iso_to_ms(block.get("resets_at")),
            }
        )
    return weekly


def _claude_session(data: JsonDict) -> tuple[float | None, int | None]:
    """(util percent, reset ms) for the 5-hour window; `limits` wins when present."""
    five = _as_dict(data.get("five_hour"))
    util = _finite_number(five.get("utilization"))
    resets_ms = iso_to_ms(five.get("resets_at"))
    limits = data.get("limits")
    if not isinstance(limits, list):
        return util, resets_ms
    for item in limits:
        if not isinstance(item, dict) or not _claude_is_session(item):
            continue
        if item.get("percent") is not None:
            util = _finite_number(item.get("percent"))
        if item.get("resets_at"):
            resets_ms = iso_to_ms(item.get("resets_at"))
        break
    return util, resets_ms


def fetch_claude() -> JsonDict:
    """One Claude reading, or a failure the panel can label.

    `error` is "no-token" when the credential store holds no usable access
    token, "http-429" when a throttled refresh left the session unproven, and
    otherwise the status _http_error names. A transient status serves the
    cached reading instead, marked "stale"; a 401 does so only while a
    throttled refresh is what the call was made on top of, since a token the
    provider rejected is a sign-out the card has to show.
    """
    if not config().claude_cred.is_file():
        return _failure("no-token")

    cred = _read_json_dict(config().claude_cred) or {}
    oauth = _as_dict(cred.get("claudeAiOauth"))
    token = oauth.get("accessToken")
    if token is None:
        return _failure("no-token")

    refreshed_already = False
    rate_limited = False
    if _claude_expired(oauth):
        refreshed, rate_limited = _refresh_claude(cred)
        refreshed_already = True
        if refreshed:
            cred = refreshed
            oauth = _as_dict(cred.get("claudeAiOauth"))
            token = oauth.get("accessToken")
            if not isinstance(token, str) or not token:
                return _failure("no-token")

    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "anthropic-version": "2023-06-01",
        "User-Agent": CLAUDE_USER_AGENT,
        "Accept": "application/json",
    }
    status, data, hdrs = fetch_http(CLAUDE_URL, headers)
    if status == 401 and not refreshed_already:
        refreshed, rate_limited = _refresh_claude(cred)
        if refreshed:
            oauth = _as_dict(refreshed.get("claudeAiOauth"))
            token = oauth.get("accessToken")
            if not isinstance(token, str) or not token:
                return _failure("no-token")
            headers["Authorization"] = f"Bearer {token}"
            status, data, hdrs = fetch_http(CLAUDE_URL, headers)
    if status in (429, 503):
        wait = parse_retry_after(hdrs.get("Retry-After") if hdrs else None)
        if wait is not None and RETRY_AFTER_MIN_S <= wait <= RETRY_AFTER_MAX_S:
            sleep(wait)
            status, data, hdrs = fetch_http(CLAUDE_URL, headers)
    account = _account_id(token)
    if status == 401:
        # Only a throttled refresh earns the cached reading. A 401 that
        # followed a refresh the provider answered is the vendor rejecting a
        # token it just minted, and the cached entry carries the same `sub`
        # and so matches the account: serving it there would keep a revoked
        # session looking healthy for the whole retention window, and the card
        # would never ask the user to log in again.
        if rate_limited:
            cached = _stale_cache("claude", account)
            if cached:
                return cached
            # A refresh the provider throttled is not a sign-out, and one it
            # rejected is: only the card subtitle tells those apart, and a user
            # whose session was revoked has to be told to log in again.
            return _failure("http-429", account, transient=True)
        return _failure("http-401", account)
    if status != 200 or not isinstance(data, dict):
        return _fail_or_cached("claude", account, status)

    plan = plan_label(oauth.get("subscriptionType"), oauth.get("rateLimitTier"))
    weekly = _claude_weekly(data)
    session_util, session_reset = _claude_session(data)

    extra = _as_dict(data.get("extra_usage"))
    spend = _as_dict(data.get("spend"))
    spend_used = _as_dict(spend.get("used"))
    # The QML scales spend.used_minor by 10^exponent, so a wire value that is
    # not a number cannot be passed on as-is: an Infinity there scales every
    # extra-usage amount to Infinity in the card. A whole number outside the
    # decimal places an amount is counted in scales it just as wrongly, so it is
    # read as cents rather than passed to a Math.pow no card can render.
    exponent = _finite_number(spend_used.get("exponent"))
    if (
        exponent is None
        or not exponent.is_integer()
        or not MIN_SPEND_EXPONENT <= exponent <= MAX_SPEND_EXPONENT
    ):
        exponent = float(DEFAULT_SPEND_EXPONENT)

    result = _reading(
        {
            "ok": True,
            "account": account,
            "plan": plan,
            "session": {
                "util": session_util,
                "resets_ms": session_reset,
            },
            "weekly": weekly,
            "extra_usage": {
                "enabled": bool(extra.get("is_enabled")),
                "used_credits": _finite_number(extra.get("used_credits")),
                "currency": _as_text(extra.get("currency")),
                "monthly_limit": _finite_number(extra.get("monthly_limit")),
            },
            "spend": {
                "used_minor": _finite_number(spend_used.get("amount_minor")),
                "currency": _as_text(
                    spend_used.get("currency") or extra.get("currency")
                ),
                "exponent": exponent,
            },
        }
    )
    _write_provider_cache("claude", result, account)
    return result


# ── Grok ────────────────────────────────────────────────────────────────────


def _load_grok_auth() -> tuple[str, JsonDict] | None:
    if not config().grok_auth.is_file():
        return None
    store = _read_json_dict(config().grok_auth)
    if not store:
        return None
    # Prefer the entry whose token lives longest, by the instant it expires.
    best_key: str | None = None
    best_entry: JsonDict | None = None
    best_exp: dt.datetime | None = None
    for key, entry in store.items():
        if not isinstance(entry, dict) or "key" not in entry:
            continue
        # Compare instants, not text: entries written at different times carry
        # different offsets and fractional widths, and "2026-01-01T09:00:00+01:00"
        # sorts after "2026-01-01T08:30:00Z" as a string while it expires earlier.
        exp = iso_to_utc(str(entry.get("expires_at") or ""))
        if exp is None and best_entry is not None:
            continue
        if best_entry is None or (
            exp is not None and (best_exp is None or exp > best_exp)
        ):
            best_key, best_entry, best_exp = key, entry, exp
    if best_key is None or best_entry is None:
        return None
    return best_key, best_entry


def _token_expired(entry: JsonDict) -> bool:
    exp = entry.get("expires_at")
    if not exp:
        return False
    when = iso_to_utc(str(exp))
    if when is None:
        return False
    return when <= _shifted(TOKEN_SKEW_S)


def _post_refresh(url: str, refresh: str, client_id: str) -> JsonDict | None:
    """Exchange a refresh token at a token endpoint, or None on any failure.

    Every failure here reads the same to the caller, and the caller turns it
    into a "no-token" or a 401 on the card, so the status that ended the
    exchange is named here, once, or a throttled refresh and a revoked
    credential are the same line in the journal. The endpoint is a vendor
    constant or one _is_grok_token_url has constrained to https on the vendor
    host, so it names no credential; the response body is never read.
    """
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": client_id,
        }
    ).encode(JSON_ENCODING)
    status, tok = fetch_json(
        url,
        {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        data=body,
        method="POST",
    )
    if status != 200 or not isinstance(tok, dict) or not tok.get("access_token"):
        warn(f"the token exchange at {url} answered {status or 'no response'}")
        return None
    return tok


def _is_grok_token_url(url: str) -> bool:
    """Whether the OIDC document's token endpoint is one we will talk to.

    The endpoint is named by a document that came off the network, and the
    value it carries decides where a long-lived refresh token is POSTed. A
    document that names another host, or the same host over plain http, is not
    trusted with it: the exchange fails and the CLI's own refresh covers the
    user until the vendor is reachable again.
    """
    parts = _origin(url)
    return parts is not None and parts[0] == "https" and parts[1] == GROK_OIDC_HOST


def _refresh_grok(auth_key: str, entry: JsonDict) -> JsonDict | None:
    """Refresh OIDC access token and persist the new tokens atomically."""
    with _refresh_lock():
        store = _read_json_dict(config().grok_auth) or {}
        current = _as_dict(store.get(auth_key))
        if current and not _token_expired(current):
            return current  # a concurrent run rotated the token while we waited
        if current:
            entry = current

        client_id = entry.get("oidc_client_id")
        if not client_id and "::" in auth_key:
            client_id = auth_key.split("::", 1)[1]
        refresh = entry.get("refresh_token")
        if not client_id or not refresh:
            return None

        _, discovery = fetch_json(
            GROK_OIDC_DISCOVERY,
            {"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        if not isinstance(discovery, dict):
            warn(f"{GROK_OIDC_DISCOVERY} answered with no discovery document")
            return None
        token_url = discovery.get("token_endpoint")
        if not isinstance(token_url, str) or not _is_grok_token_url(token_url):
            if token_url is not None:
                warn(
                    "grok discovery named a token endpoint that is not https on "
                    f"{GROK_OIDC_HOST}; the refresh token was not sent to it"
                )
            return None

        tok = _post_refresh(token_url, refresh, client_id)
        if tok is None:
            return None

        new_entry = dict(entry)
        new_entry["key"] = tok["access_token"]
        if tok.get("refresh_token"):
            new_entry["refresh_token"] = tok["refresh_token"]
        expires_in = _finite_number(tok.get("expires_in"))
        expires_ms = ms_from_seconds_or_none(expires_in) if expires_in else None
        if expires_ms is not None:
            exp = _shifted(expires_ms / 1000)
            new_entry["expires_at"] = exp.isoformat().replace("+00:00", "Z")

        # Persist so subsequent polls (and the Grok CLI) keep working.
        def put_entry(store: JsonDict) -> tuple[str, Any]:
            store[auth_key] = new_entry
            return auth_key, new_entry

        _write_rotated_tokens(
            "grok", config().grok_auth, put_entry, {auth_key: new_entry}
        )
        return new_entry


def _money_val(obj: Any) -> int | None:  # JSON number or {val: int}
    """A cent amount, or None. The billing API sends dollars as a JSON
    double, which the widget renders as a minor-unit amount, so int()
    truncation lands a cent under every value; rounding reconciles it with
    the dollars the vendor billed."""
    if isinstance(obj, dict) and "val" in obj:
        obj = obj["val"]
    number = _finite_number(obj)
    return None if number is None else round(number)


def _first_present(cfg: JsonDict, *keys: str) -> Any:
    """The first key the payload carries, by presence and not by truth.

    A cap of 0 is a real cap, so `cfg.get(a) or cfg.get(b)` would drop it for
    the fallback key and report the plan as uncapped.
    """
    for key in keys:
        if key in cfg:
            return cfg[key]
    return None


def _parse_grok_period(cfg: JsonDict) -> JsonDict:
    """Parse one billing config into a period dict (weekly or monthly shape)."""
    on_demand = _money_val(_first_present(cfg, "onDemandCap", "on_demand_cap"))
    period = _as_dict(cfg.get("currentPeriod"))
    ptype = str(period.get("type") or "")
    label = (
        "Weekly" if "WEEKLY" in ptype else "Monthly" if "MONTHLY" in ptype else "Usage"
    )

    # Unified-billing users: a single percent for the current period, no $
    # figures. creditUsagePercent is omitted when 0% used, so a credits-shaped
    # response (has currentPeriod / isUnifiedBillingUser) means 0 when absent.
    is_credits = (
        "creditUsagePercent" in cfg
        or bool(cfg.get("currentPeriod"))
        or bool(cfg.get("isUnifiedBillingUser"))
    )
    util: float | None
    if is_credits:
        credit_raw = cfg.get("creditUsagePercent")
        if credit_raw is None:
            util = 0.0
        else:
            credit_pct = _finite_number(credit_raw)
            if credit_pct is None and isinstance(credit_raw, str):
                with contextlib.suppress(TypeError, ValueError):
                    credit_pct = _finite_number(float(credit_raw))
            util = round(credit_pct, 1) if credit_pct is not None else None
        used = limit = None
        end_ms = iso_to_ms(period.get("end") or cfg.get("billingPeriodEnd"))
    else:
        # Legacy monthly shape: $ used of $ limit (dollars on the wire, which
        # _money_val turns into the cents the payload carries).
        used = _money_val(cfg.get("used"))
        limit = _money_val(_first_present(cfg, "monthlyLimit", "monthly_limit"))
        # A limit of zero or less is no limit; dividing by it would blow up or
        # flip the meter negative. The ratio itself can still overflow on a
        # large used amount (100 * 1.7e308 is Infinity), and json.dumps writes
        # that as bare Infinity, which is not JSON and which plasmashell
        # rejects, so the computed percent is a finite check like every other
        # number the payload carries.
        util = (
            _finite_number(round(100.0 * used / limit, 1))
            if used is not None and limit is not None and limit > 0
            else None
        )
        end_ms = iso_to_ms(cfg.get("billingPeriodEnd") or cfg.get("billing_period_end"))
        if label == "Usage":
            label = "Monthly"

    return {
        "label": label,
        "util": util,
        "used": used,
        "limit": limit,
        "on_demand_cap": on_demand,
        "resets_ms": end_ms,
        "unit": "cents",
        "currency": "USD",
    }


def fetch_grok() -> JsonDict:
    """One Grok reading, or a failure the panel can label.

    `error` is "no-token" with no auth store, "http-401" when the session is
    rejected, and otherwise the first non-200 status of the two billing calls.
    A transient status serves the cached reading instead, marked "stale".
    """
    loaded = _load_grok_auth()
    if not loaded:
        return _failure("no-token")
    auth_key, entry = loaded

    if _token_expired(entry):
        refreshed = _refresh_grok(auth_key, entry)
        if refreshed:
            entry = refreshed

    def get_cfg(url: str, entry: JsonDict) -> tuple[int, JsonDict | None, JsonDict]:
        """(status, period config, the entry to poll the next call with).

        The entry comes back rather than being assigned in place, so two calls
        can run at once: each refreshes off its own copy and hands the token it
        ended up holding back, and the two never write to the same name.
        """

        def call(token: str) -> tuple[int, object]:
            return fetch_json(
                url,
                {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )

        token = entry.get("key")
        if not isinstance(token, str) or not token:
            return 0, None, entry
        status, data = call(token)
        if status == 401:
            # The refresh lock spans the credential re-read, so whichever call
            # gets there second finds the rotated token and skips the round
            # trip; both then hold the same one.
            refreshed = _refresh_grok(auth_key, entry)
            if not refreshed:
                return 401, None, entry
            entry = refreshed
            token = entry.get("key")
            if not isinstance(token, str) or not token:
                return 401, None, entry
            status, data = call(token)
        if status != 200 or not isinstance(data, dict):
            return status, None, entry
        cfg = data.get("config")
        return status, cfg if isinstance(cfg, dict) else data, entry

    # Weekly (unified credits) + monthly ($ limit) are separate meters; show both.
    # Two round trips to one host, so they overlap instead of summing: the poll
    # waits the slower of the two rather than the sum of both.
    with ThreadPoolExecutor(max_workers=2) as pool:
        week = pool.submit(get_cfg, GROK_BILLING_URL + "?format=credits", entry)
        month = pool.submit(get_cfg, GROK_BILLING_URL, entry)
        st_week, week_cfg, week_entry = week.result()
        st_month, month_cfg, month_entry = month.result()
    # Either call may be the one that rotated the token; a refresh either one
    # saw is the entry the reading belongs to.
    entry = month_entry if week_entry is entry else week_entry

    periods: list[JsonDict] = []
    seen: set[tuple[str, int | None]] = set()
    for cfg in (week_cfg, month_cfg):
        if not cfg:
            continue
        p = _parse_grok_period(cfg)
        key = (p["label"], p["resets_ms"])
        if p["util"] is None or key in seen:
            continue
        seen.add(key)
        periods.append(p)

    account = _account_id(entry.get("key"), auth_key)
    if not periods:
        # Neither call yielded a meter; report the failure, not the call that
        # happened to answer 200 with a payload that had no period in it.
        status = next((s for s in (st_week, st_month) if s != 200), 0)
        if status == 401:
            return _failure("http-401", account)
        return _fail_or_cached("grok", account, status)

    result = _reading(
        {"ok": True, "account": account, "plan": "Grok", "periods": periods}
    )
    _write_provider_cache("grok", result, account)
    return result


# ── Codex ───────────────────────────────────────────────────────────────────


# One poll reads three claims off the same Codex access token (expiry, account
# id, `sub`) and two off a Cursor one, and each read was a base64 pass plus a
# JSON parse of a kilobyte or two. The memo makes each token decode once. A
# poll is a fresh process every two minutes, so the bound only has to cover
# the tokens of one run, and the result is shared: the sole reader below never
# mutates it.
JWT_MEMO_SIZE = 8


@lru_cache(maxsize=JWT_MEMO_SIZE)
def _jwt_payload(token: str) -> JsonDict | None:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        pad = "=" * ((4 - len(parts[1]) % 4) % 4)
        # RFC 7519 spells the payload UTF-8, and it is decoded as such:
        # json.loads on bytes sniffs a UTF-16 or UTF-32 payload instead, and a
        # token that is not UTF-8 is no token.
        payload = json.loads(
            base64.urlsafe_b64decode(parts[1] + pad).decode(JSON_ENCODING)
        )
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _jwt_claim(token: str, *path: str) -> Any:
    """Nested JWT payload value; claim types are not a closed set."""
    cur: Any = _jwt_payload(token)
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _jwt_exp_ms(token: str) -> int | None:
    exp = _finite_number(_jwt_claim(token, "exp"))
    return ms_from_seconds_or_none(exp) if exp is not None else None


def _codex_window_label(window_seconds: int | None, name: str) -> str:
    """Map primary/secondary window duration to a human label."""
    if not window_seconds:
        return name.replace("_", " ").title()
    if window_seconds <= CODEX_SESSION_MAX_S:
        return "Current session"
    if window_seconds <= CODEX_TWO_DAY_S:
        hours = max(1, round(window_seconds / SECONDS_PER_HOUR))
        return f"{hours}-hour"
    if CODEX_WEEK_MIN_S <= window_seconds <= CODEX_WEEK_MAX_S:
        return "Weekly"
    if CODEX_MONTH_MIN_S <= window_seconds <= CODEX_MONTH_MAX_S:
        return "Monthly"
    days = max(1, round(window_seconds / SECONDS_PER_DAY))
    return f"{days}-day"


def _codex_window(block: JsonDict | None, name: str) -> JsonDict | None:
    if not isinstance(block, dict):
        return None
    raw_used = block.get("used_percent")
    if raw_used is None:
        return None
    if isinstance(raw_used, str):
        with contextlib.suppress(TypeError, ValueError):
            raw_used = float(raw_used)
    number = _finite_number(raw_used)
    if number is None:
        return None  # NaN or Infinity is no reading; it is not 0% and not 100%
    util = min(100.0, max(0.0, number))
    window_s = block.get("limit_window_seconds")
    window_number = _finite_number(window_s)
    window_s_i = int(window_number) if window_number is not None else None

    resets_ms = None
    reset_at = _finite_number(block.get("reset_at"))
    after = _finite_number(block.get("reset_after_seconds"))
    if reset_at is not None:
        resets_ms = ms_from_seconds_or_none(reset_at)
    elif after is not None:
        after_ms = ms_from_seconds_or_none(after)
        resets_ms = None if after_ms is None else now_ms() + after_ms

    return {
        "label": _codex_window_label(window_s_i, name),
        "util": util,
        "resets_ms": resets_ms,
    }


def _codex_reset_credits(data: JsonDict) -> JsonDict:
    """Preserve a reported empty reset-credit balance as an explicit zero.

    A count that is present but is not a finite number is not a balance, so
    it reads as absent: a NaN here would reach the emitted document and the
    panel's JSON parser would reject the whole payload.
    """
    reported = "rate_limit_reset_credits" in data
    raw = data.get("rate_limit_reset_credits")
    resets = raw if isinstance(raw, dict) else {}
    available = _emittable(resets.get("available_count"))
    applicable = _emittable(resets.get("applicable_available_count"))
    return {
        "reported": reported,
        "available": 0 if reported and available is None else available,
        "applicable": 0 if reported and applicable is None else applicable,
    }


def _codex_token_expired(tokens: JsonDict) -> bool:
    access = tokens.get("access_token")
    if not isinstance(access, str) or not access:
        return True
    exp_ms = _jwt_exp_ms(access)
    if exp_ms is None:
        return False  # opaque token: the usage call is the only truth
    return exp_ms <= now_ms() + TOKEN_SKEW_MS


def _refresh_codex(auth: JsonDict) -> JsonDict | None:
    with _refresh_lock():
        latest = _read_json_dict(config().codex_auth)
        if latest is not None:
            auth = latest
        tokens = _as_dict(auth.get("tokens"))
        if not _codex_token_expired(tokens):
            return auth  # a concurrent run rotated the token while we waited
        refresh = tokens.get("refresh_token")
        if not refresh:
            return None

        tok = _post_refresh(CODEX_TOKEN_URL, refresh, CODEX_CLIENT_ID)
        if tok is None:
            return None

        new_tokens = dict(tokens)
        new_tokens["access_token"] = tok["access_token"]
        if tok.get("refresh_token"):
            new_tokens["refresh_token"] = tok["refresh_token"]
        if tok.get("id_token"):
            new_tokens["id_token"] = tok["id_token"]

        new_auth = dict(auth)
        new_auth["tokens"] = new_tokens
        new_auth["last_refresh"] = now_utc().isoformat().replace("+00:00", "Z")

        def put_tokens(store: JsonDict) -> tuple[str, Any]:
            store["tokens"] = new_tokens
            store["last_refresh"] = new_auth["last_refresh"]
            return "tokens", new_tokens

        _write_rotated_tokens("codex", config().codex_auth, put_tokens, new_auth)
        return new_auth


def fetch_codex() -> JsonDict:
    """One Codex reading, or a failure the panel can label.

    `error` is "no-token" when the auth file holds no usable access token, and
    otherwise the status _http_error names. The "http-401" a rejected refresh
    returns names an account too: the digest is taken from the token the call
    was made with, before the call, and the `sub` claim survives rotation. A
    transient status serves the cached reading instead, marked "stale".
    """
    if not config().codex_auth.is_file():
        return _failure("no-token")

    auth = _read_json_dict(config().codex_auth)
    if auth is None:
        return _failure("no-token")

    tokens = _as_dict(auth.get("tokens"))
    access = tokens.get("access_token")
    if not isinstance(access, str) or not access:
        return _failure("no-token")

    account_id = tokens.get("account_id") or _jwt_claim(
        access, "https://api.openai.com/auth", "chatgpt_account_id"
    )

    exp_ms = _jwt_exp_ms(access)
    if exp_ms is not None and exp_ms <= now_ms() + TOKEN_SKEW_MS:
        refreshed = _refresh_codex(auth)
        if refreshed:
            auth = refreshed
            tokens = _as_dict(auth.get("tokens"))
            access = tokens.get("access_token")
            account_id = tokens.get("account_id") or account_id
            if not isinstance(access, str) or not access:
                return _failure("no-token")

    # The digest is taken from the token in hand, before the call, so a
    # failure names its account the way every other provider's does. The `sub`
    # claim survives rotation, so a refresh yields the same scope.
    account = _account_id(access, str(account_id) if account_id else None)

    def call(token: str) -> tuple[int, object]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if account_id:
            headers["ChatGPT-Account-Id"] = str(account_id)
        return fetch_json(CODEX_USAGE_URL, headers)

    status, data = call(access)
    if status == 401:
        refreshed = _refresh_codex(auth)
        if not refreshed:
            return _failure("http-401", account)
        tokens = _as_dict(refreshed.get("tokens"))
        access = tokens.get("access_token")
        if not isinstance(access, str) or not access:
            return _failure("http-401", account)
        account_id = tokens.get("account_id") or account_id
        account = _account_id(access, str(account_id) if account_id else None)
        status, data = call(access)

    if status != 200 or not isinstance(data, dict):
        return _fail_or_cached("codex", account, status)

    plan_type = data.get("plan_type")
    plan = _label_text(plan_type).replace("_", " ").title() or "Codex"

    rate = _as_dict(data.get("rate_limit"))
    windows: list[JsonDict] = []
    for key in ("primary_window", "secondary_window"):
        w = _codex_window(rate.get(key), key)
        if w:
            windows.append(w)

    # code_review_rate_limit may mirror the same shape
    cr = data.get("code_review_rate_limit")
    if isinstance(cr, dict):
        if "primary_window" in cr or "secondary_window" in cr:
            for key in ("primary_window", "secondary_window"):
                w = _codex_window(cr.get(key), f"code_review_{key}")
                if w:
                    w["label"] = "Code review · " + w["label"]
                    windows.append(w)
        elif cr.get("used_percent") is not None:
            w = _codex_window(cr, "code_review")
            if w:
                w["label"] = "Code review"
                windows.append(w)

    credits_block = _as_dict(data.get("credits"))
    reset_credits = _codex_reset_credits(data)

    result = _reading(
        {
            "ok": True,
            "account": account,
            "plan": plan,
            "allowed": bool(rate.get("allowed")),
            "limit_reached": bool(rate.get("limit_reached")),
            "windows": windows,
            "credits": {
                "has_credits": bool(credits_block.get("has_credits")),
                "balance": _amount(credits_block.get("balance")),
                "unlimited": bool(credits_block.get("unlimited")),
            },
            "reset_credits": reset_credits,
        }
    )
    _write_provider_cache("codex", result, account)
    return result


# ── Cursor ──────────────────────────────────────────────────────────────────


def _vscdb_str(value: Any) -> str | None:
    """ItemTable cell: raw str, bytes, or JSON-quoted str."""
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode(JSON_ENCODING)
        except UnicodeDecodeError:
            # A token is an identity: a mangled one authenticates as nobody.
            # Drop the cell so the caller falls through to the next source.
            return None
    s = str(value).strip()
    if not s:
        return None
    if s.startswith('"'):
        try:
            decoded = json.loads(s)
            if isinstance(decoded, str):
                s = decoded.strip()
                if not s:
                    return None  # a quoted blank cell is no token either
        except json.JSONDecodeError:
            pass  # keep the raw cell text
    if not _utf8_encodable(s):
        # A JSON escape can spell a lone surrogate ("\ud800"): it decodes, it
        # does not encode, and it would fail the header quote or the cache
        # write. Treat it like any other undecodable cell.
        return None
    return s


def _workos_user_id(sub: str) -> str:
    """WorkOS user id from a JWT sub (`auth0|user_01abc` -> `user_01abc`)."""
    if "|" in sub:
        return sub.rsplit("|", 1)[-1]
    return sub


def _jwt_sub(token: str) -> str | None:
    """The WorkOS user id a Cursor token claims, or None if it names none.

    A claim that cannot be encoded names no usable session: it is
    percent-encoded into the Cookie header below, and quoting it raises where
    a dropped claim only costs the Cursor card.
    """
    sub = _jwt_claim(token, "sub")
    if not isinstance(sub, str) or not sub or not _utf8_encodable(sub):
        return None
    return _workos_user_id(sub)


def cursor_plan_label(membership: str | None) -> str:
    """Map a Cursor membershipType to the dashboard's plan name.

    An unknown value is title-cased rather than dropped, so a plan the
    fetcher has not seen still reads as a name rather than as "Cursor".
    """
    # The membership arrives from a remote body and a credential file, so it
    # can be any JSON value. A non-string names no plan.
    m = (membership if isinstance(membership, str) else "").strip().lower()
    m = m.replace("-", "_").replace(" ", "_")
    names = {
        "free": "Free",
        "hobby": "Hobby",
        "pro": "Pro",
        "pro_plus": "Pro+",
        "proplus": "Pro+",
        "ultra": "Ultra",
        "business": "Business",
        "team": "Team",
        "teams": "Team",
        "enterprise": "Enterprise",
    }
    if m in names:
        return names[m]
    if m:
        return _label_text(m).replace("_", " ").title() or "Cursor"
    return "Cursor"


def _read_cursor_auth_json(path: Path) -> tuple[str, str] | None:
    try:
        store = json.loads(_read_text(path))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(store, dict):
        return None
    token = store.get("accessToken")
    if not isinstance(token, str) or not token:
        return None
    return token, ""


def _sqlite_is_routine(exc: sqlite3.Error) -> bool:
    """A state.vscdb failure that is ordinary rather than a fault.

    The IDE holds a write lock while it saves, and a db that has never held a
    Cursor session has no ItemTable. Both read as "no token", which is correct.
    """
    text = str(exc).lower()
    return "locked" in text or "no such table" in text


def _vscdb_text(raw: bytes) -> str:
    """sqlite text_factory: decode a TEXT cell without losing the query.

    sqlite's default text_factory raises on a cell that is not valid UTF-8,
    and it raises while the result set is being built, so one undecodable
    membership cell discards a perfectly good accessToken in the same row set
    and the user loses the provider. surrogateescape keeps valid text exact
    and hands the undecodable cell on as lone surrogates, which _vscdb_str
    drops like any other cell it cannot re-encode.
    """
    return raw.decode(JSON_ENCODING, "surrogateescape")


def _read_cursor_state_db(path: Path) -> tuple[str, str] | None:
    if not path.is_file():
        return None
    uri = "file:" + pathname2url(str(path)) + "?mode=ro"
    con = None
    try:
        try:
            con = sqlite3.connect(uri, uri=True, timeout=1.0)
        except sqlite3.OperationalError:
            con = sqlite3.connect(uri + "&immutable=1", uri=True, timeout=1.0)
        con.text_factory = _vscdb_text
        rows = con.execute(
            "SELECT key, value FROM ItemTable WHERE key IN (?, ?)",
            ("cursorAuth/accessToken", "cursorAuth/stripeMembershipType"),
        ).fetchall()
    except sqlite3.Error as exc:
        # Without this the panel says "no token" and the operator signs the
        # Cursor CLI out and back in over a db that was never readable.
        if not _sqlite_is_routine(exc):
            warn(f"{path} could not be read: {exc}")
        return None
    finally:
        if con is not None:
            # A close failure is never actionable here: the read is already over
            # and the connection is dropped with the process either way.
            with contextlib.suppress(sqlite3.Error):
                con.close()
    m = {k: _vscdb_str(v) for k, v in rows}
    token = m.get("cursorAuth/accessToken")
    if not token:
        return None
    return token, (m.get("cursorAuth/stripeMembershipType") or "")


def _load_cursor_auth() -> dict[str, str] | None:
    """Return {token, sub, plan} from the cursor-agent auth.json, or the IDE DB.

    An entry whose token names no account carries no `sub` and is skipped, so
    the next source is read rather than scoped to a blank account.
    """
    cfg = config()
    candidates: list[tuple[Path, Callable[[Path], tuple[str, str] | None]]] = [
        (cfg.cursor_auth, _read_cursor_auth_json),
        (cfg.cursor_state_db, _read_cursor_state_db),
    ]

    for path, read in candidates:
        try:
            loaded = read(path)
        except PermissionError:
            continue
        if not loaded:
            continue
        token, plan = loaded
        sub = _jwt_sub(token)
        if not sub:
            continue
        return {"token": token, "sub": sub, "plan": plan}
    return None


def _cursor_meter(
    block: Any,  # usage-summary meter object; keys vary by plan
    label: str,
    unit: str,
    resets_ms: int | None,
) -> JsonDict | None:
    if not isinstance(block, dict) or not block.get("enabled", True):
        return None
    used = _finite_number(block.get("used"))
    limit = _finite_number(block.get("limit"))
    util = _finite_number(block.get("totalPercentUsed"))
    if util is None and used is not None and limit is not None and limit > 0:
        # Finite inputs can still overflow the ratio (1e308 / 1e-308), and
        # json.dumps writes an Infinity plasmashell's parser rejects. A
        # negative limit is not a limit either, and would flip the meter.
        util = _finite_number(round(100.0 * used / limit, 1))
    if util is None and used is None and limit is None:
        return None
    return {
        "label": label,
        "util": util,
        "used": used,
        "limit": limit,
        "resets_ms": resets_ms,
        "unit": unit,
    }


def _cursor_split_percent_meters(
    plan_u: Any, included: JsonDict | None, cycle_end: int | None
) -> list[JsonDict]:
    """The Auto and API meters a plan block carries, or none.

    A block that reports both splits them out of the included block, so the
    split is a second gauge only when it says something the included meter
    does not: an auto and an API percentage that agree with each other, or with
    the included meter, restate a number the card already shows.
    """
    if not isinstance(plan_u, dict):
        return []
    auto_pct = _finite_number(plan_u.get("autoPercentUsed"))
    api_pct = _finite_number(plan_u.get("apiPercentUsed"))
    if auto_pct is None or api_pct is None:
        return []
    if auto_pct == api_pct:
        return []
    util = included["util"] if included else None
    if (
        util is not None
        and abs(auto_pct - float(util)) <= CURSOR_METER_AGREEMENT_PCT
        and abs(api_pct - float(util)) <= CURSOR_METER_AGREEMENT_PCT
    ):
        return []
    return [
        {
            "label": label,
            "util": pct,
            "used": None,
            "limit": None,
            "resets_ms": cycle_end,
            "unit": "percent",
        }
        for label, pct in (("Auto + Composer", auto_pct), ("API", api_pct))
    ]


def parse_cursor_summary(data: JsonDict, plan_hint: str | None = None) -> JsonDict:
    """Turn /api/usage-summary JSON into widget periods."""
    plan = cursor_plan_label(_as_text(data.get("membershipType")) or plan_hint)
    cycle_end = iso_to_ms(data.get("billingCycleEnd"))
    # Only a real true says the plan has no included block. A dict or a list
    # is a type error, and reading it as unlimited would hide the meters.
    unlimited = data.get("isUnlimited") is True
    periods: list[JsonDict] = []

    iu = _as_dict(data.get("individualUsage"))
    plan_u = iu.get("plan")
    overall = iu.get("overall")

    if not unlimited:
        included = (
            _cursor_meter(plan_u, "Included", "count", cycle_end) if plan_u else None
        )
        if included is None:
            included = _cursor_meter(overall, "Included", "cents", cycle_end)
        if included:
            periods.append(included)
        periods.extend(_cursor_split_percent_meters(plan_u, included, cycle_end))

    on_demand = _cursor_meter(iu.get("onDemand"), "On-demand", "cents", cycle_end)
    team = _as_dict(data.get("teamUsage"))
    team_od = _cursor_meter(
        team.get("onDemand"),
        "Team on-demand" if on_demand else "On-demand",
        "cents",
        cycle_end,
    )
    if on_demand:
        periods.append(on_demand)
    if team_od and (not on_demand or team_od.get("used") != on_demand.get("used")):
        periods.append(team_od)

    return {
        "ok": True,
        "plan": plan,
        "unlimited": unlimited,
        "periods": periods,
        "resets_ms": cycle_end,
    }


def fetch_cursor() -> JsonDict:
    """One Cursor reading, or a failure the panel can label.

    `error` is "no-token" when neither credential source yields a session, and
    otherwise the status the vendor sent, so a 403 stays a 403 and is not
    reported as a signed-out session. A transient status serves the cached
    reading instead, marked "stale".
    """
    auth = _load_cursor_auth()
    if not auth:
        return _failure("no-token")

    cookie = "WorkosCursorSessionToken=" + urllib.parse.quote(
        auth["sub"] + "::" + auth["token"], safe="", encoding=JSON_ENCODING
    )
    headers = {
        "Cookie": cookie,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "Origin": "https://cursor.com",
        "Referer": "https://cursor.com/dashboard/usage",
    }
    status, data = fetch_json(CURSOR_SUMMARY_URL, headers)
    account = _account_id(auth["token"], auth["sub"])
    if status in (401, 403):
        # Report the status the vendor sent. A 403 is an edge rejection of the
        # request, not a signed-out session, and labelling it 401 sends the
        # user to re-authenticate for nothing.
        return _failure(f"http-{status}", account)
    if status != 200 or not isinstance(data, dict):
        return _fail_or_cached("cursor", account, status)

    summary = parse_cursor_summary(data, auth.get("plan"))
    summary["account"] = account
    result = _reading(summary)
    _write_provider_cache("cursor", result, account)
    return result


# ── main ────────────────────────────────────────────────────────────────────

USAGE_LINE = "usage: fetch_quota.py [--print-config | --clear-cache] [--help]"

# The env table above is the single list of knobs, so --help cannot drift from
# what load_config accepts. The column is padded, not truncated, and is wide
# enough for the longest name, so no name is cut; the help is monospace-ish
# prose, not a table.
ENV_HELP = "\n".join(f"  {name:<30} {help_text}" for name, help_text in ENV_DOCS)

HELP = f"""{USAGE_LINE}

Poll each configured provider's usage endpoint and print one JSON object on
stdout. Plasmashell polls this script every 2 minutes, so stdout stays pure
JSON: diagnostics go to stderr. The exit code is 0 whenever JSON was printed,
including a config error (the JSON then carries "error": "config"). An
argument this help does not list is a usage error: it exits 2, prints nothing
on stdout, and names the offending argument on stderr.

options:
  --print-config  print the resolved config (paths, knobs, key source) and exit
  --clear-cache   delete every cached reading and the account key, then exit
  -h, --help      print this help and exit

exit codes:
  0  a payload was printed, including a config error (the JSON then carries
     "error": "config", so a poll that cannot run is still a poll the panel
     can read) and a provider that raised (that one prints "error": "net")
  2  an argument the fetcher does not take, named on stderr

environment:
{ENV_HELP}

Any other QUOTA_WIDGET_* name is rejected as a typo rather than ignored.
Plasmashell does not read shell rc files: export a variable into the user
session (systemctl --user import-environment NAME) or the widget never sees it.
"""


def _safe_fetch(name: str, fetch: Callable[[], JsonDict]) -> JsonDict:
    try:
        return fetch()
    except Exception as exc:  # noqa: BLE001 (catch-all by design)
        # Plasmashell needs JSON every poll; one provider must not abort the rest.
        # A provider that crashed is a transport failure as far as the panel is
        # concerned, so the card it holds is kept, but the cause belongs in the
        # journal: a bug here otherwise looks exactly like a dropped connection
        # on the display.
        warn(f"provider {name} raised {type(exc).__name__}: {exc}")
        warn_traceback(exc)
        return _failure("net", transient=True)


def _poll_stamp() -> int:
    """Now for the emitted envelope. The config-error path reports a value the
    clock itself rejected, so it falls back to the real one instead of raising
    a second time and leaving plasmashell with no JSON at all."""
    try:
        return now_ms()
    except ConfigError:
        return ms_from_seconds(time.time())


def _clear_cache() -> JsonDict:
    """Erase everything this fetcher keeps about an account, and name what went.

    The provider entries hold the last reading each account produced, and the
    account key holds the digest scope they were taken under, so both go: the
    key outlives the entries it scopes, and an entry restored from a backup
    would still be readable under a key that never left the machine. The
    vendor token files are the CLIs' own and are not touched.

    The locks stay: they carry nothing, and unlinking a file another poll has
    flocked would leave that poll holding a lock no later one can see.

    A directory that cannot be listed is reported rather than raised out of
    main(): the run's whole output is the list of what went, and an exception
    here would leave plasmashell with no payload at all over a run the
    operator started by hand and can retry.
    """
    folder = config().cache_dir
    removed: list[str] = []
    if folder.is_dir():
        try:
            entries = sorted(folder.iterdir())
        except OSError as exc:
            warn(f"could not list {folder}: {exc}")
            return {"cache_dir": str(folder), "removed": removed, "error": str(exc)}
        for path in entries:
            if not path.is_file() or _is_lock_file(path.name):
                continue
            try:
                path.unlink()
            except OSError as exc:
                warn(f"could not remove the {path.name} cache entry: {exc}")
                continue
            removed.append(path.name)
    return {"cache_dir": str(folder), "removed": removed}


def main(argv: list[str] | None = None) -> None:
    """Validate the environment, run every provider, print one JSON payload.

    `--print-config` and `--clear-cache` stop after the config check, before
    any provider runs: one prints the resolved config, the other erases the
    cache. Any other argument is a usage error. Every run that got as far as a
    provider exits through emit(), so one that crashed on its way there still
    leaves the panel a payload it can read.
    """
    _use_utf8_streams()
    args = sys.argv[1:] if argv is None else argv
    if args in (["--help"], ["-h"]):
        # Answered before load_config: help must work on a broken environment.
        print(HELP, end="")
        raise SystemExit(0)
    if len(args) > 1 or (args and args[0] not in ("--print-config", "--clear-cache")):
        # A usage error is the operator's, not the panel's, so it is reported
        # before load_config: a typo on a machine with a broken environment
        # would otherwise print the config payload and exit 0, and a script
        # reading stdout would never learn its argument was wrong. The name
        # printed is the offending one, so `--print-config extra` does not
        # report a valid flag as unknown. The closing hint is the one the
        # installer and print_smoke print, so a mistyped flag in this project
        # says the same thing wherever it was typed.
        unexpected = args[1] if len(args) > 1 else args[0]
        print(
            f"fetch_quota: unexpected argument {unexpected!r}\n{USAGE_LINE}\n"
            "try 'fetch_quota.py --help' for more information.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        cfg = load_config()
    except ConfigError as exc:
        # No provider runs on a bad value; the panel shows "config" and the
        # detail lands on stderr for anyone running the fetcher by hand.
        warn(str(exc))
        emit(
            {
                **_failure("config"),
                "config_error": _redact(str(exc)),
                "claude": _failure("config"),
                "cursor": _failure("config"),
                "grok": _failure("config"),
                "codex": _failure("config"),
                "fetched_ms": _poll_stamp(),
            }
        )
    if args == ["--print-config"]:
        emit({"ok": True, "config": cfg.describe()})
    if args == ["--clear-cache"]:
        cleared = _clear_cache()
        # An unlistable directory is a failed erasure, not an empty one, and
        # "ok" is what a script reading stdout keys its exit on.
        emit({"ok": "error" not in cleared, **cleared})

    # A poll waits on network, not CPU: each provider is one or more HTTPS round
    # trips, so running them in turn made the panel wait the sum of every
    # provider's latency. One thread per provider overlaps them. The refresh
    # flock still serializes token rotation (flock is per open file
    # description, so two threads conflict exactly as two processes do), and
    # the results are keyed by name, so the payload keeps its field order.
    providers: dict[str, Callable[[], JsonDict]] = {
        "claude": fetch_claude,
        "cursor": fetch_cursor,
        "grok": fetch_grok,
        "codex": fetch_codex,
    }
    with ThreadPoolExecutor(max_workers=len(providers)) as pool:
        futures = {
            name: pool.submit(_safe_fetch, name, fetch)
            for name, fetch in providers.items()
        }
    results: dict[str, JsonDict] = {
        name: future.result() for name, future in futures.items()
    }

    emit(
        {
            "ok": any(r.get("ok") for r in results.values()),
            **results,
            # The panel ages a kept reading against this window, so an override
            # of QUOTA_WIDGET_CACHE_MAX_AGE_S reaches it instead of being
            # answered by the 24 h default compiled into the QML.
            "cache_max_age_s": cfg.cache_max_age_s,
            # And it sizes its poll watchdog from this, so a
            # QUOTA_WIDGET_HTTP_TIMEOUT the panel's own constant could not hold
            # is not answered by dropping a run that was still entitled to
            # answer.
            "poll_timeout_s": cfg.poll_timeout_s,
            "fetched_ms": now_ms(),
        }
    )


if __name__ == "__main__":
    main()
