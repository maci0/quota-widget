#!/usr/bin/env python3
"""Fetch Claude + Cursor + Grok + Codex usage quotas for the Plasma widget.

Claude: GET https://api.anthropic.com/api/oauth/usage
  (same numbers as claude.ai Settings → Usage / Claude Code /usage)
  Auth: ~/.claude/.credentials.json → claudeAiOauth.accessToken

Cursor: GET https://cursor.com/api/usage-summary
  (same numbers as cursor.com/dashboard → Usage)
  Auth: Cursor IDE session in state.vscdb, or cursor-agent ~/.config/cursor/auth.json

Grok:   GET https://cli-chat-proxy.grok.com/v1/billing
  Auth: ~/.grok/auth.json OIDC access token (auto-refreshed)

Codex:  GET https://chatgpt.com/backend-api/wham/usage
  (same numbers as chatgpt.com/codex/settings/usage and Codex /status)
  Auth: ~/.codex/auth.json ChatGPT OAuth tokens (auto-refreshed)

Configuration is read once at startup from QUOTA_WIDGET_* environment
variables and validated before any request; run with --print-config to see the
active values. See README "Configuration".
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import email.utils
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Any, TypeAlias
from urllib.request import pathname2url

# Unversioned HTTP JSON: keys and nesting change by plan, host, and API revision.
JsonDict: TypeAlias = dict[str, Any]

# Overrides every wall-clock read in this module; see now_ms().
NOW_MS_ENV = "QUOTA_WIDGET_NOW_MS"

# Every credential, cache, and state file is UTF-8 JSON, including the ones the
# vendor CLIs write. Naming it beats open()'s locale default, which is ASCII
# under a C locale (a plasmashell started without LANG) and would decode a
# store holding a non-ASCII account name into a read error.
JSON_ENCODING = "utf-8"

# update(obj) mutates obj and returns the (key, value) pair that must survive.
MergeUpdate: TypeAlias = Callable[[JsonDict], "tuple[str, Any]"]


def _as_dict(value: object) -> JsonDict:
    return value if isinstance(value, dict) else {}


def _read_text(path: Path) -> str:
    """Read a JSON state file as UTF-8, whatever the process locale says."""
    return path.read_text(encoding=JSON_ENCODING)


def _http_retryable(status: int) -> bool:
    return status in (429, 503) or status >= 500


def now_ms() -> int:
    """Epoch milliseconds. QUOTA_WIDGET_NOW_MS pins the clock to a fixed value,
    so a whole poll replays byte-for-byte; unset in production, real clock."""
    override = os.environ.get(NOW_MS_ENV)
    if override is None:
        return int(time.time() * 1000)
    try:
        return int(override)
    except ValueError:
        raise ValueError(
            f"{NOW_MS_ENV}={override!r} is not an integer epoch-ms value"
        ) from None


def now_utc() -> dt.datetime:
    return dt.datetime.fromtimestamp(now_ms() / 1000, dt.UTC)


def sleep(seconds: float) -> None:
    time.sleep(seconds)


CLAUDE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_TOKEN_URLS = (
    "https://platform.claude.com/v1/oauth/token",
    "https://console.anthropic.com/v1/oauth/token",
)

GROK_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing"
GROK_OIDC_DISCOVERY = "https://auth.x.ai/.well-known/openid-configuration"

CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
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
# One poll holds the lock for at most a token round trip; a longer wait means
# the holder died, and the caller refreshes anyway rather than never.
REFRESH_LOCK_WAIT_S = 20.0
REFRESH_LOCK_POLL_S = 0.25
# Longest a cached reading may be shown after the vendor API fails, unless
# QUOTA_WIDGET_CACHE_MAX_AGE_S says otherwise. The plasmoid keeps its own copy
# for the same window; see staleKeepMs in package/contents/ui/main.qml.
DEFAULT_CACHE_MAX_AGE_S = 24 * 3600
DEFAULT_HTTP_TIMEOUT_S = 12.0
MAX_HTTP_TIMEOUT_S = 300.0
CACHE_DIR_MODE = 0o700
FILE_MODE_PRIVATE = 0o600
# Re-read-after-write retries before a token store is left to the racing writer.
MERGE_WRITE_ATTEMPTS = 3
TOKEN_SKEW_S = 120
TOKEN_SKEW_MS = TOKEN_SKEW_S * 1000
RETRY_AFTER_MIN_S = 0.5
RETRY_AFTER_MAX_S = 10.0
# Unix seconds vs milliseconds: values above this are treated as ms.
MS_EPOCH_CUTOFF = 10_000_000_000
ERROR_BODY_PREVIEW = 200
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400
CODEX_SESSION_MAX_S = 6 * SECONDS_PER_HOUR
CODEX_TWO_DAY_S = 2 * SECONDS_PER_DAY
CODEX_WEEK_MIN_S = 6 * SECONDS_PER_DAY
CODEX_WEEK_MAX_S = 8 * SECONDS_PER_DAY
CODEX_MONTH_MIN_S = 28 * SECONDS_PER_DAY
CODEX_MONTH_MAX_S = 32 * SECONDS_PER_DAY


# ── configuration ───────────────────────────────────────────────────────────
# Every knob is an environment variable read once at startup and validated
# before any request. See README "Configuration" for the documented set.


class ConfigError(Exception):
    """A configuration value is unset-but-empty, unparsable, or out of range."""


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

    def describe(self) -> JsonDict:
        """Active values for `--print-config`. Paths only; no token is read here."""
        return {
            "home": str(self.home),
            "claude_cred": str(self.claude_cred),
            "codex_auth": str(self.codex_auth),
            "grok_auth": str(self.grok_auth),
            "cursor_auth": str(self.cursor_auth),
            "cursor_state_db": str(self.cursor_state_db),
            "cache_dir": str(self.cache_dir),
            "http_timeout_s": self.http_timeout_s,
            "cache_max_age_s": self.cache_max_age_s,
        }


def _env_path(env: Mapping[str, str], name: str, default: Path) -> Path:
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip()
    if not value:
        raise ConfigError(f"{name} is set but empty")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ConfigError(f"{name} must be an absolute path, got {value!r}")
    return path


def _env_number(
    env: Mapping[str, str], name: str, default: float, maximum: float
) -> float:
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip()
    if not value:
        raise ConfigError(f"{name} is set but empty")
    try:
        number = float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc
    if not 0 < number <= maximum:
        raise ConfigError(f"{name} must be in (0, {maximum:g}], got {number:g}")
    return number


def _cursor_config_root(env: Mapping[str, str], home: Path) -> Path:
    if sys.platform == "darwin":
        return home / "Library" / "Application Support"
    if os.name == "nt":
        appdata = env.get("APPDATA")
        return Path(appdata) if appdata else home / "AppData" / "Roaming"
    xdg = env.get("XDG_CONFIG_HOME")
    return Path(xdg) if xdg else home / ".config"


def _cursor_state_db(env: Mapping[str, str], home: Path) -> Path:
    return (
        _cursor_config_root(env, home)
        / "Cursor"
        / "User"
        / "globalStorage"
        / "state.vscdb"
    )


_CONFIG: Config | None = None


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Read and validate the environment. Raises ConfigError on bad values."""
    values = os.environ if env is None else env
    home = _env_path(values, "QUOTA_WIDGET_HOME", Path.home())
    xdg_cache = values.get("XDG_CACHE_HOME")
    cache_base = Path(xdg_cache) if xdg_cache else home / ".cache"
    timeout = _env_number(
        values, "QUOTA_WIDGET_HTTP_TIMEOUT", DEFAULT_HTTP_TIMEOUT_S, MAX_HTTP_TIMEOUT_S
    )
    max_age = _env_number(
        values, "QUOTA_WIDGET_CACHE_MAX_AGE_S", float(DEFAULT_CACHE_MAX_AGE_S), 86400.0
    )
    global _CONFIG
    _CONFIG = Config(
        home=home,
        claude_cred=_env_path(
            values,
            "QUOTA_WIDGET_CLAUDE_CREDENTIALS",
            home / ".claude" / ".credentials.json",
        ),
        codex_auth=_env_path(
            values, "QUOTA_WIDGET_CODEX_AUTH", home / ".codex" / "auth.json"
        ),
        grok_auth=_env_path(
            values, "QUOTA_WIDGET_GROK_AUTH", home / ".grok" / "auth.json"
        ),
        cursor_auth=_env_path(
            values,
            "QUOTA_WIDGET_CURSOR_AUTH",
            _cursor_config_root(values, home) / "cursor" / "auth.json",
        ),
        cursor_state_db=_env_path(
            values, "QUOTA_WIDGET_CURSOR_STATE_DB", _cursor_state_db(values, home)
        ),
        cache_dir=_env_path(values, "QUOTA_WIDGET_CACHE", cache_base / "quota-widget"),
        http_timeout_s=timeout,
        cache_max_age_s=int(max_age),
    )
    return _CONFIG


def config() -> Config:
    """The validated configuration. main() loads it before any provider runs."""
    if _CONFIG is None:
        return load_config()
    return _CONFIG


def emit(obj: JsonDict) -> None:
    print(json.dumps(obj, separators=(",", ":")))
    raise SystemExit(0)


def iso_to_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        s = value.replace("Z", "+00:00")
        return int(dt.datetime.fromisoformat(s).timestamp() * 1000)
    except (TypeError, ValueError, OSError):
        return None


def plan_label(subscription: str | None, tier: str | None) -> str:
    """Map Claude credential fields to the website plan name."""
    sub = (subscription or "").lower()
    tier = (tier or "").lower()

    m = re.search(r"(\d+)x", tier)
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
        return sub.replace("_", " ").title()
    return "Claude"


def parse_retry_after(value: str | None) -> float | None:
    """Seconds to wait from a Retry-After header (delta-seconds or HTTP-date)."""
    if not value:
        return None
    s = value.strip()
    try:
        return max(0.0, float(s))
    except ValueError:
        pass  # not delta-seconds; try HTTP-date next
    try:
        when = email.utils.parsedate_to_datetime(s)
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.UTC)
        return max(0.0, (when - now_utc()).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None  # HTTP-date present but not parseable


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
        with os.fdopen(fd, "w", encoding=JSON_ENCODING) as f:
            json.dump(obj, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, FILE_MODE_PRIVATE)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass  # tmp already gone
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
    """
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
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(after, dict) and after.get(key) == value:
            return
    # A writer kept winning the race; it holds the rotated token itself, so the
    # tokens in memory stay usable for this poll.


def _digest(value: str | None) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _account_id(access_token: str | None, fallback: str | None = None) -> str | None:
    """Account id the provider cache is scoped to, or None if unknowable.

    The token's `sub` claim survives access-token rotation, so a refresh does
    not orphan the entry; a digest of the token itself would. `fallback` is
    only for providers that publish an account id outside the token.
    """
    sub = _jwt_claim(access_token, "sub") if isinstance(access_token, str) else None
    return _digest(sub if isinstance(sub, str) and sub else fallback)


def _discard_provider_cache(path: Path) -> None:
    """Delete one cache file. Best-effort: a leftover file is unreadable anyway."""
    try:
        path.unlink()
    except OSError:
        pass  # already gone, or not ours to remove


def _read_provider_cache(
    name: str, account: str | None, max_age_s: int | None = None
) -> JsonDict | None:
    path = config().cache_dir / f"{name}.json"
    limit_s = config().cache_max_age_s if max_age_s is None else max_age_s
    try:
        obj = json.loads(_read_text(path))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    ts = obj.get("cached_ms")
    payload = obj.get("payload")
    if not isinstance(ts, (int, float)) or not isinstance(payload, dict):
        return None
    if not payload.get("ok"):
        return None
    # An entry belongs to the account whose credential produced it. Reading
    # another account's plan and usage is worse than showing nothing, so an
    # unidentifiable caller reads nothing.
    if account is None or obj.get("account") != account:
        return None
    now = now_ms()
    if now - int(ts) > limit_s * 1000:
        # Past the retention window, so drop it rather than leave it on disk.
        _discard_provider_cache(path)
        return None
    return payload


def _write_provider_cache(name: str, payload: JsonDict, account: str | None) -> None:
    if not payload.get("ok") or account is None:
        return
    folder = config().cache_dir
    try:
        folder.mkdir(parents=True, mode=CACHE_DIR_MODE, exist_ok=True)
        _atomic_write_json(
            folder / f"{name}.json",
            {
                "cached_ms": now_ms(),
                "account": account,
                "payload": payload,
            },
        )
    except OSError:
        pass  # cache is best-effort; a full disk must not fail the poll


def _stale_cache(name: str, account: str | None) -> JsonDict | None:
    cached = _read_provider_cache(name, account)
    if not cached:
        return None
    out = dict(cached)
    out["stale"] = True
    return out


def _read_json_dict(path: Path) -> JsonDict | None:
    try:
        obj = json.loads(_read_text(path))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


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
        folder.mkdir(parents=True, mode=CACHE_DIR_MODE, exist_ok=True)
        fd = os.open(
            str(folder / REFRESH_LOCK_NAME),
            os.O_CREAT | os.O_RDWR,
            FILE_MODE_PRIVATE,
        )
    except OSError:
        yield  # unwritable cache dir: refresh unguarded, not never
        return
    try:
        deadline = time.monotonic() + REFRESH_LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(REFRESH_LOCK_POLL_S)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def fetch_http(
    url: str,
    headers: dict[str, str],
    *,
    timeout: float | None = None,
    data: bytes | None = None,
    method: str | None = None,
) -> tuple[int, object, Message | None]:
    """Return (status, decoded JSON or None, response headers)."""
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    req_timeout = config().http_timeout_s if timeout is None else timeout
    try:
        with urllib.request.urlopen(req, timeout=req_timeout) as resp:
            body = resp.read()
            hdrs = resp.headers
            if not body:
                return resp.status, None, hdrs
            try:
                return resp.status, json.loads(body.decode("utf-8")), hdrs
            except (json.JSONDecodeError, UnicodeDecodeError):
                return resp.status, None, hdrs
    except urllib.error.HTTPError as e:
        # The error body can carry account identifiers (email, user id) echoed
        # back by the vendor. No caller reads it, so the body is not retained.
        hdrs = e.headers if e.headers is not None else Message()
        try:
            e.read()
        except OSError:
            pass  # body unreadable; the status is all the callers use
        return e.code, None, hdrs
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, None, None


def fetch_json(
    url: str,
    headers: dict[str, str],
    *,
    timeout: float | None = None,
    data: bytes | None = None,
    method: str | None = None,
) -> tuple[int, object]:
    status, body, _hdrs = fetch_http(
        url, headers, timeout=timeout, data=data, method=method
    )
    return status, body


# ── Claude ──────────────────────────────────────────────────────────────────


def _claude_expired(oauth: JsonDict, skew_ms: int = TOKEN_SKEW_MS) -> bool:
    exp = oauth.get("expiresAt")
    if not isinstance(exp, (int, float)):
        return False
    ts_ms = int(exp if exp > MS_EPOCH_CUTOFF else exp * 1000)
    return ts_ms <= now_ms() + skew_ms


def _refresh_claude(cred: JsonDict) -> JsonDict | None:
    """Refresh Claude Code OAuth and write the rotated tokens back."""
    with _refresh_lock():
        latest = _read_json_dict(config().claude_cred)
        if latest is not None:
            cred = latest
        oauth = cred.get("claudeAiOauth")
        if not isinstance(oauth, dict):
            return None
        refresh = oauth.get("refreshToken")
        if not isinstance(refresh, str) or not refresh:
            return None
        if not _claude_expired(oauth):
            return cred  # a concurrent run rotated the token while we waited

        body = json.dumps(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": CLAUDE_CLIENT_ID,
            }
        ).encode()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        tok: Any = None  # OAuth token JSON; fields vary by host
        for url in CLAUDE_TOKEN_URLS:
            status, tok = fetch_json(url, headers, data=body, method="POST")
            if status == 200 and isinstance(tok, dict) and tok.get("access_token"):
                break
            if status in (400, 401):
                return None
            tok = None
        if not isinstance(tok, dict) or not tok.get("access_token"):
            return None

        new_oauth = dict(oauth)
        new_oauth["accessToken"] = tok["access_token"]
        if tok.get("refresh_token"):
            new_oauth["refreshToken"] = tok["refresh_token"]
        expires_in = tok.get("expires_in")
        if isinstance(expires_in, (int, float)):
            new_oauth["expiresAt"] = now_ms() + int(expires_in) * 1000
        new_cred = dict(cred)
        new_cred["claudeAiOauth"] = new_oauth

        def put_oauth(store: JsonDict) -> tuple[str, Any]:
            store["claudeAiOauth"] = new_oauth
            return "claudeAiOauth", new_oauth

        try:
            _merge_write_json(config().claude_cred, put_oauth, new_cred)
        except OSError:
            pass  # still return in-memory tokens so this poll can proceed
        return new_cred


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
                label = str(scope["surface"])
            elif _as_dict(scope.get("model")).get("display_name"):
                label = str(_as_dict(scope["model"])["display_name"])
            if kind == "weekly_all":
                label = "All models"
            weekly.append(
                {
                    "label": label,
                    "util": item.get("percent"),
                    "resets_ms": iso_to_ms(item.get("resets_at")),
                    "kind": kind,
                }
            )
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
                "util": block.get("utilization"),
                "resets_ms": iso_to_ms(block.get("resets_at")),
                "kind": key,
            }
        )
    return weekly


def _claude_session(data: JsonDict) -> tuple[Any, int | None]:
    """(util percent, reset ms) for the 5-hour window; `limits` wins when present."""
    five = _as_dict(data.get("five_hour"))
    util: Any = five.get("utilization")
    resets_ms = iso_to_ms(five.get("resets_at"))
    limits = data.get("limits")
    if not isinstance(limits, list):
        return util, resets_ms
    for item in limits:
        if not isinstance(item, dict) or not _claude_is_session(item):
            continue
        if item.get("percent") is not None:
            util = item.get("percent")
        if item.get("resets_at"):
            resets_ms = iso_to_ms(item.get("resets_at"))
        break
    return util, resets_ms


def fetch_claude() -> JsonDict:
    if not config().claude_cred.is_file():
        return {"ok": False, "error": "no-token"}

    try:
        cred = json.loads(_read_text(config().claude_cred))
        oauth = cred["claudeAiOauth"]
        token = oauth["accessToken"]
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError):
        return {"ok": False, "error": "no-token"}

    refreshed_already = False
    if _claude_expired(oauth):
        refreshed = _refresh_claude(cred)
        refreshed_already = True
        if refreshed:
            cred = refreshed
            oauth = _as_dict(cred.get("claudeAiOauth"))
            token = oauth.get("accessToken")
            if not isinstance(token, str) or not token:
                return {"ok": False, "error": "no-token"}

    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "anthropic-version": "2023-06-01",
        "User-Agent": CLAUDE_USER_AGENT,
        "Accept": "application/json",
    }
    status, data, hdrs = fetch_http(CLAUDE_URL, headers)
    if status == 401 and not refreshed_already:
        refreshed = _refresh_claude(cred)
        if refreshed:
            oauth = _as_dict(refreshed.get("claudeAiOauth"))
            token = oauth.get("accessToken")
            if not isinstance(token, str) or not token:
                return {"ok": False, "error": "no-token"}
            headers["Authorization"] = f"Bearer {token}"
            status, data, hdrs = fetch_http(CLAUDE_URL, headers)
    if status in (429, 503):
        wait = parse_retry_after(hdrs.get("Retry-After") if hdrs else None)
        if wait is not None and RETRY_AFTER_MIN_S <= wait <= RETRY_AFTER_MAX_S:
            sleep(wait)
            status, data, hdrs = fetch_http(CLAUDE_URL, headers)
    account = _account_id(token)
    if status == 401:
        cached = _stale_cache("claude", account)
        if cached:
            return cached
        # Refresh 429 with a still-valid refresh token is not a sign-out.
        if oauth.get("refreshToken"):
            return {"ok": False, "error": "http-429"}
        return {"ok": False, "error": "http-401"}
    if status != 200 or not isinstance(data, dict):
        if _http_retryable(status):
            cached = _stale_cache("claude", account)
            if cached:
                return cached
        return {"ok": False, "error": f"http-{status}" if status else "net"}

    plan = plan_label(oauth.get("subscriptionType"), oauth.get("rateLimitTier"))
    weekly = _claude_weekly(data)
    session_util, session_reset = _claude_session(data)

    extra = _as_dict(data.get("extra_usage"))
    spend = _as_dict(data.get("spend"))
    spend_used = _as_dict(spend.get("used"))

    result = {
        "ok": True,
        "plan": plan,
        "session": {
            "util": session_util,
            "resets_ms": session_reset,
        },
        "weekly": weekly,
        "extra_usage": {
            "enabled": bool(extra.get("is_enabled")),
            "used_credits": extra.get("used_credits"),
            "currency": extra.get("currency"),
            "monthly_limit": extra.get("monthly_limit"),
        },
        "spend": {
            "enabled": bool(spend.get("enabled")),
            "percent": spend.get("percent"),
            "used_minor": spend_used.get("amount_minor"),
            "currency": spend_used.get("currency") or extra.get("currency"),
            "exponent": spend_used.get("exponent", 2),
        },
    }
    _write_provider_cache("claude", result, account)
    return result


# ── Grok ────────────────────────────────────────────────────────────────────


def _load_grok_auth() -> tuple[str, JsonDict] | None:
    if not config().grok_auth.is_file():
        return None
    try:
        store = json.loads(_read_text(config().grok_auth))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(store, dict) or not store:
        return None
    # Prefer the entry with the latest expires_at
    best_key = None
    best_entry: JsonDict | None = None
    best_exp = ""
    for key, entry in store.items():
        if not isinstance(entry, dict) or "key" not in entry:
            continue
        exp = str(entry.get("expires_at") or "")
        if best_entry is None or exp > best_exp:
            best_key, best_entry, best_exp = key, entry, exp
    if best_key is None or best_entry is None:
        return None
    return best_key, best_entry


def _token_expired(entry: JsonDict, skew_s: int = TOKEN_SKEW_S) -> bool:
    exp = entry.get("expires_at")
    if not exp:
        return False
    try:
        when = dt.datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
        return when <= now_utc() + dt.timedelta(seconds=skew_s)
    except (TypeError, ValueError, OSError):
        return False


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
            return None
        token_url = discovery.get("token_endpoint")
        if not isinstance(token_url, str) or not token_url:
            return None

        body = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": client_id,
            }
        ).encode()
        status, tok = fetch_json(
            token_url,
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            data=body,
            method="POST",
        )
        if status != 200 or not isinstance(tok, dict) or not tok.get("access_token"):
            return None

        new_entry = dict(entry)
        new_entry["key"] = tok["access_token"]
        if tok.get("refresh_token"):
            new_entry["refresh_token"] = tok["refresh_token"]
        expires_in = tok.get("expires_in")
        if isinstance(expires_in, (int, float)):
            exp = now_utc() + dt.timedelta(seconds=int(expires_in))
            new_entry["expires_at"] = exp.isoformat().replace("+00:00", "Z")

        # Persist so subsequent polls (and the Grok CLI) keep working.
        def put_entry(store: JsonDict) -> tuple[str, Any]:
            store[auth_key] = new_entry
            return auth_key, new_entry

        try:
            _merge_write_json(config().grok_auth, put_entry, {auth_key: new_entry})
        except OSError:
            pass  # return the live token; writing auth.json failed

        return new_entry


def _money_val(obj: Any) -> int | None:  # JSON number or {val: int}
    if obj is None:
        return None
    if isinstance(obj, dict) and "val" in obj:
        try:
            return int(obj["val"])
        except (TypeError, ValueError, OverflowError):
            return None
    if isinstance(obj, (int, float)):
        return int(obj)
    return None


def _parse_grok_period(cfg: JsonDict) -> JsonDict:
    """Parse one billing config into a period dict (weekly or monthly shape)."""
    on_demand = _money_val(cfg.get("onDemandCap") or cfg.get("on_demand_cap"))
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
    if is_credits:
        credit_pct = cfg.get("creditUsagePercent")
        try:
            util = round(float(credit_pct), 1) if credit_pct is not None else 0.0
        except (TypeError, ValueError):
            util = None
        used = limit = None
        start_ms = iso_to_ms(period.get("start") or cfg.get("billingPeriodStart"))
        end_ms = iso_to_ms(period.get("end") or cfg.get("billingPeriodEnd"))
    else:
        # Legacy monthly shape: $ used of $ limit (values in cents).
        used = _money_val(cfg.get("used"))
        limit = _money_val(cfg.get("monthlyLimit") or cfg.get("monthly_limit"))
        util = round(100.0 * used / limit, 1) if used is not None and limit else None
        start_ms = iso_to_ms(
            cfg.get("billingPeriodStart") or cfg.get("billing_period_start")
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
        "period_start_ms": start_ms,
        "resets_ms": end_ms,
        "unit": "cents",
        "currency": "USD",
    }


def fetch_grok() -> JsonDict:
    loaded = _load_grok_auth()
    if not loaded:
        return {"ok": False, "error": "no-token"}
    auth_key, entry = loaded

    if _token_expired(entry):
        refreshed = _refresh_grok(auth_key, entry)
        if refreshed:
            entry = refreshed

    state = {"entry": entry}

    def get_cfg(url: str) -> tuple[int, JsonDict | None]:
        def call(token: str) -> tuple[int, object]:
            return fetch_json(
                url,
                {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )

        token = state["entry"].get("key")
        if not isinstance(token, str) or not token:
            return 0, None
        status, data = call(token)
        if status == 401:
            refreshed = _refresh_grok(auth_key, state["entry"])
            if not refreshed:
                return 401, None
            state["entry"] = refreshed
            token = state["entry"].get("key")
            if not isinstance(token, str) or not token:
                return 401, None
            status, data = call(token)
        if status != 200 or not isinstance(data, dict):
            return status, None
        cfg = data.get("config")
        return status, cfg if isinstance(cfg, dict) else data

    # Weekly (unified credits) + monthly ($ limit) are separate meters; show both.
    st_week, week_cfg = get_cfg(GROK_BILLING_URL + "?format=credits")
    st_month, month_cfg = get_cfg(GROK_BILLING_URL)

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

    account = _account_id(state["entry"].get("key"), auth_key)
    if not periods:
        status = st_week or st_month or 0
        if status == 401:
            return {"ok": False, "error": "http-401"}
        if _http_retryable(status):
            cached = _stale_cache("grok", account)
            if cached:
                return cached
        return {"ok": False, "error": f"http-{status}" if status else "net"}

    result = {"ok": True, "plan": "Grok", "periods": periods}
    _write_provider_cache("grok", result, account)
    return result


# ── Codex ───────────────────────────────────────────────────────────────────


def _jwt_payload(token: str) -> JsonDict | None:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        pad = "=" * ((4 - len(parts[1]) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
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
    exp = _jwt_claim(token, "exp")
    return int(exp) * 1000 if isinstance(exp, (int, float)) else None


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
    try:
        util = min(100.0, max(0.0, float(raw_used)))
    except (TypeError, ValueError):
        return None
    window_s = block.get("limit_window_seconds")
    try:
        window_s_i = int(window_s) if window_s is not None else None
    except (TypeError, ValueError):
        window_s_i = None

    resets_ms = None
    reset_at = block.get("reset_at")
    if isinstance(reset_at, (int, float)):
        resets_ms = int(reset_at * 1000)
    else:
        after = block.get("reset_after_seconds")
        if isinstance(after, (int, float)):
            resets_ms = now_ms() + int(float(after) * 1000)

    return {
        "label": _codex_window_label(window_s_i, name),
        "util": util,
        "resets_ms": resets_ms,
        "window_seconds": window_s_i,
        "kind": name,
    }


def _codex_reset_credits(data: JsonDict) -> JsonDict:
    """Preserve a reported empty reset-credit balance as an explicit zero."""
    reported = "rate_limit_reset_credits" in data
    raw = data.get("rate_limit_reset_credits")
    resets = raw if isinstance(raw, dict) else {}
    available = resets.get("available_count")
    applicable = resets.get("applicable_available_count")
    return {
        "reported": reported,
        "available": 0 if reported and available is None else available,
        "applicable": 0 if reported and applicable is None else applicable,
    }


def _codex_token_expired(tokens: JsonDict, skew_ms: int = TOKEN_SKEW_MS) -> bool:
    access = tokens.get("access_token")
    if not isinstance(access, str) or not access:
        return True
    exp_ms = _jwt_exp_ms(access)
    if exp_ms is None:
        return False  # opaque token: the usage call is the only truth
    now_ms = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    return exp_ms <= now_ms + skew_ms


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

        body = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": CODEX_CLIENT_ID,
            }
        ).encode()
        status, tok = fetch_json(
            CODEX_TOKEN_URL,
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            data=body,
            method="POST",
        )
        if status != 200 or not isinstance(tok, dict) or not tok.get("access_token"):
            return None

        new_tokens = dict(tokens)
        new_tokens["access_token"] = tok["access_token"]
        if tok.get("refresh_token"):
            new_tokens["refresh_token"] = tok["refresh_token"]
        if tok.get("id_token"):
            new_tokens["id_token"] = tok["id_token"]

        new_auth = dict(auth)
        new_auth["tokens"] = new_tokens
        new_auth["last_refresh"] = now_utc().isoformat()

        def put_tokens(store: JsonDict) -> tuple[str, Any]:
            store["tokens"] = new_tokens
            store["last_refresh"] = new_auth["last_refresh"]
            return "tokens", new_tokens

        try:
            _merge_write_json(config().codex_auth, put_tokens, new_auth)
        except OSError:
            pass  # return live tokens; writing auth.json failed
        return new_auth


def fetch_codex() -> JsonDict:
    if not config().codex_auth.is_file():
        return {"ok": False, "error": "no-token"}

    try:
        auth = json.loads(_read_text(config().codex_auth))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {"ok": False, "error": "no-token"}
    if not isinstance(auth, dict):
        return {"ok": False, "error": "no-token"}

    tokens = _as_dict(auth.get("tokens"))
    access = tokens.get("access_token")
    if not isinstance(access, str) or not access:
        return {"ok": False, "error": "no-token"}

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
                return {"ok": False, "error": "no-token"}

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
            return {"ok": False, "error": "http-401"}
        tokens = _as_dict(refreshed.get("tokens"))
        access = tokens.get("access_token")
        if not isinstance(access, str):
            return {"ok": False, "error": "http-401"}
        status, data = call(access)

    account = _account_id(access, str(account_id) if account_id else None)
    if status != 200 or not isinstance(data, dict):
        if _http_retryable(status):
            cached = _stale_cache("codex", account)
            if cached:
                return cached
        return {"ok": False, "error": f"http-{status}" if status else "net"}

    plan_type = data.get("plan_type") or "Codex"
    plan = str(plan_type).replace("_", " ").title()

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

    credits = _as_dict(data.get("credits"))
    reset_credits = _codex_reset_credits(data)

    result = {
        "ok": True,
        "plan": plan,
        "allowed": rate.get("allowed"),
        "limit_reached": bool(rate.get("limit_reached")),
        "windows": windows,
        "credits": {
            "has_credits": bool(credits.get("has_credits")),
            "balance": credits.get("balance"),
            "unlimited": bool(credits.get("unlimited")),
            "overage_limit_reached": bool(credits.get("overage_limit_reached")),
        },
        "reset_credits": reset_credits,
    }
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
                return decoded
        except json.JSONDecodeError:
            pass  # keep the raw cell text
    return s


def _workos_user_id(sub: str) -> str:
    """WorkOS user id from a JWT sub (`auth0|user_01abc` -> `user_01abc`)."""
    if "|" in sub:
        return sub.rsplit("|", 1)[-1]
    return sub


def _jwt_sub(token: str) -> str | None:
    sub = _jwt_claim(token, "sub")
    if not isinstance(sub, str) or not sub:
        return None
    return _workos_user_id(sub)


def cursor_plan_label(membership: str | None) -> str:
    m = (membership or "").strip().lower().replace("-", "_").replace(" ", "_")
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
        return m.replace("_", " ").title()
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
        rows = con.execute(
            "SELECT key, value FROM ItemTable WHERE key IN (?, ?)",
            ("cursorAuth/accessToken", "cursorAuth/stripeMembershipType"),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        if con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass  # connection already closed or unusable
    m = {k: _vscdb_str(v) for k, v in rows}
    token = m.get("cursorAuth/accessToken")
    if not token:
        return None
    return token, (m.get("cursorAuth/stripeMembershipType") or "")


def _load_cursor_auth() -> dict[str, str] | None:
    """Return {token, plan} from the cursor-agent auth.json, or the IDE DB."""
    cfg = config()
    candidates: list[tuple[Path, str]] = [
        (cfg.cursor_auth, "json"),
        (cfg.cursor_state_db, "vscdb"),
    ]

    for path, kind in candidates:
        try:
            if not path.is_file():
                continue
            loaded = (
                _read_cursor_auth_json(path)
                if kind == "json"
                else _read_cursor_state_db(path)
            )
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
    used = block.get("used")
    limit = block.get("limit")
    util = block.get("totalPercentUsed")
    if (
        util is None
        and isinstance(used, (int, float))
        and isinstance(limit, (int, float))
        and limit
    ):
        util = round(100.0 * float(used) / float(limit), 1)
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


def parse_cursor_summary(data: JsonDict, plan_hint: str | None = None) -> JsonDict:
    """Turn /api/usage-summary JSON into widget periods."""
    plan = cursor_plan_label(data.get("membershipType") or plan_hint)
    cycle_end = iso_to_ms(data.get("billingCycleEnd"))
    unlimited = bool(data.get("isUnlimited"))
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
        if isinstance(plan_u, dict):
            auto_pct = plan_u.get("autoPercentUsed")
            api_pct = plan_u.get("apiPercentUsed")
            util = included["util"] if included else None
            if (
                isinstance(auto_pct, (int, float))
                and isinstance(api_pct, (int, float))
                and (auto_pct != api_pct)
                and (
                    util is None
                    or abs(float(auto_pct) - float(util)) > 0.5
                    or abs(float(api_pct) - float(util)) > 0.5
                )
            ):
                periods.append(
                    {
                        "label": "Auto + Composer",
                        "util": auto_pct,
                        "used": None,
                        "limit": None,
                        "resets_ms": cycle_end,
                        "unit": "percent",
                    }
                )
                periods.append(
                    {
                        "label": "API",
                        "util": api_pct,
                        "used": None,
                        "limit": None,
                        "resets_ms": cycle_end,
                        "unit": "percent",
                    }
                )

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
        "limit_type": data.get("limitType"),
        "periods": periods,
        "resets_ms": cycle_end,
    }


def fetch_cursor() -> JsonDict:
    auth = _load_cursor_auth()
    if not auth:
        return {"ok": False, "error": "no-token"}

    cookie = "WorkosCursorSessionToken=" + urllib.parse.quote(
        auth["sub"] + "::" + auth["token"], safe=""
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
        return {"ok": False, "error": "http-401"}
    if _http_retryable(status):
        cached = _stale_cache("cursor", account)
        if cached:
            return cached
        return {"ok": False, "error": f"http-{status}"}
    if status != 200 or not isinstance(data, dict):
        return {"ok": False, "error": f"http-{status}" if status else "net"}

    result = parse_cursor_summary(data, auth.get("plan"))
    _write_provider_cache("cursor", result, account)
    return result


# ── main ────────────────────────────────────────────────────────────────────


def _safe_fetch(fetch: Callable[[], JsonDict]) -> JsonDict:
    try:
        return fetch()
    except Exception:
        # Plasmashell needs JSON every poll; one provider must not abort the rest.
        return {"ok": False, "error": "net"}


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    try:
        cfg = load_config()
    except ConfigError as exc:
        # No provider runs on a bad value; the panel shows "config" and the
        # detail lands on stderr for anyone running the fetcher by hand.
        print(f"fetch_quota: {exc}", file=sys.stderr)
        emit(
            {
                "ok": False,
                "error": "config",
                "config_error": str(exc),
                "claude": {"ok": False, "error": "config"},
                "cursor": {"ok": False, "error": "config"},
                "grok": {"ok": False, "error": "config"},
                "codex": {"ok": False, "error": "config"},
                "fetched_ms": now_ms(),
            }
        )
    if args == ["--print-config"]:
        emit({"ok": True, "config": cfg.describe()})
    if args:
        print(f"fetch_quota: unknown argument {args[0]!r}", file=sys.stderr)
        raise SystemExit(2)

    providers: dict[str, Callable[[], JsonDict]] = {
        "claude": fetch_claude,
        "cursor": fetch_cursor,
        "grok": fetch_grok,
        "codex": fetch_codex,
    }
    results: dict[str, JsonDict] = {
        name: _safe_fetch(fetch) for name, fetch in providers.items()
    }

    emit(
        {
            "ok": any(r.get("ok") for r in results.values()),
            **results,
            "fetched_ms": now_ms(),
        }
    )


if __name__ == "__main__":
    main()
