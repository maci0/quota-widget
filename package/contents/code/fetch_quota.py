#!/usr/bin/env python3
"""Fetch Claude + Grok + Codex usage quotas for the Plasma widget.

Claude: GET https://api.anthropic.com/api/oauth/usage
  (same numbers as claude.ai Settings → Usage / Claude Code /usage)
  Auth: ~/.claude/.credentials.json → claudeAiOauth.accessToken

Grok:   GET https://cli-chat-proxy.grok.com/v1/billing
  Auth: ~/.grok/auth.json OIDC access token (auto-refreshed)

Codex:  GET https://chatgpt.com/backend-api/wham/usage
  (same numbers as chatgpt.com/codex/settings/usage and Codex /status)
  Auth: ~/.codex/auth.json ChatGPT OAuth tokens (auto-refreshed)
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

CLAUDE_CRED = Path.home() / ".claude" / ".credentials.json"
CLAUDE_URL = "https://api.anthropic.com/api/oauth/usage"

GROK_AUTH = Path.home() / ".grok" / "auth.json"
# Base billing endpoint. "?format=credits" returns the current period the CLI
# shows ("Weekly limit left", creditUsagePercent); the bare URL returns the
# legacy monthly $ limit. We fetch both and show whichever meters exist.
GROK_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing"
GROK_OIDC_DISCOVERY = "https://auth.x.ai/.well-known/openid-configuration"

CODEX_AUTH = Path.home() / ".codex" / "auth.json"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
# Public client id embedded in the Codex CLI for ChatGPT login.
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

USER_AGENT = "quota-widget/1.0"


def emit(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, separators=(",", ":")))
    raise SystemExit(0)


def iso_to_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        # Handle both "…Z" and offset forms; fromisoformat needs +00:00 not Z.
        s = value.replace("Z", "+00:00")
        return int(dt.datetime.fromisoformat(s).timestamp() * 1000)
    except Exception:
        return None


def plan_label(subscription: str | None, tier: str | None) -> str:
    """Map Claude credential fields to the website plan name."""
    sub = (subscription or "").lower()
    tier = (tier or "").lower()

    mult = None
    m = re.search(r"max[_\s-]?(\d+)x", tier) or re.search(r"(\d+)x", tier)
    if m:
        mult = m.group(1)

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


def fetch_json(
    url: str,
    headers: dict[str, str],
    *,
    timeout: float = 12,
    data: bytes | None = None,
    method: str | None = None,
) -> tuple[int, Any]:
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            if not body:
                return resp.status, None
            return resp.status, json.loads(body.decode())
    except urllib.error.HTTPError as e:
        try:
            payload = e.read().decode()
            try:
                return e.code, json.loads(payload)
            except Exception:
                return e.code, {"raw": payload[:200]}
        except Exception:
            return e.code, None


# ── Claude ──────────────────────────────────────────────────────────────────


def fetch_claude() -> dict[str, Any]:
    if not CLAUDE_CRED.is_file():
        return {"ok": False, "error": "no-token"}

    try:
        cred = json.loads(CLAUDE_CRED.read_text())
        oauth = cred["claudeAiOauth"]
        token = oauth["accessToken"]
    except Exception:
        return {"ok": False, "error": "no-token"}

    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "anthropic-version": "2023-06-01",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    # The usage endpoint sporadically 429s; a short retry heals most blips
    # within one poll. ponytail: fixed 2s backoff, honor Retry-After when given.
    status, data = fetch_json(CLAUDE_URL, headers)
    if status in (429, 503):
        time.sleep(2)
        status, data = fetch_json(CLAUDE_URL, headers)
    if status == 401:
        return {"ok": False, "error": "http-401"}
    if status != 200 or not isinstance(data, dict):
        return {"ok": False, "error": f"http-{status}" if status else "net"}

    plan = plan_label(oauth.get("subscriptionType"), oauth.get("rateLimitTier"))

    # Prefer the structured `limits` array (matches the website list,
    # including scoped weekly bars like Fable). Fall back to legacy keys.
    weekly: list[dict[str, Any]] = []
    limits = data.get("limits")
    if isinstance(limits, list) and limits:
        for item in limits:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind") or ""
            group = item.get("group") or ""
            if group == "session" or kind == "session":
                # handled separately below
                continue
            label = "All models"
            scope = item.get("scope") or {}
            if isinstance(scope, dict):
                model = scope.get("model") or {}
                if isinstance(model, dict) and model.get("display_name"):
                    label = str(model["display_name"])
                surface = scope.get("surface")
                if surface:
                    label = str(surface)
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
    else:
        for key, label in (
            ("seven_day", "All models"),
            ("seven_day_opus", "Opus"),
            ("seven_day_sonnet", "Sonnet"),
            ("seven_day_cowork", "Cowork"),
        ):
            block = data.get(key)
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

    five = data.get("five_hour") or {}
    # Also pull session percent from limits if present
    session_util = five.get("utilization")
    session_reset = iso_to_ms(five.get("resets_at"))
    if isinstance(limits, list):
        for item in limits:
            if isinstance(item, dict) and (
                item.get("kind") == "session" or item.get("group") == "session"
            ):
                if item.get("percent") is not None:
                    session_util = item.get("percent")
                if item.get("resets_at"):
                    session_reset = iso_to_ms(item.get("resets_at"))
                break

    extra = data.get("extra_usage") or {}
    spend = data.get("spend") or {}

    return {
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
            "used_minor": (spend.get("used") or {}).get("amount_minor"),
            "currency": (spend.get("used") or {}).get("currency")
            or extra.get("currency"),
            "exponent": (spend.get("used") or {}).get("exponent", 2),
        },
    }


# ── Grok ────────────────────────────────────────────────────────────────────


def _load_grok_auth() -> tuple[str, dict[str, Any]] | None:
    if not GROK_AUTH.is_file():
        return None
    try:
        store = json.loads(GROK_AUTH.read_text())
    except Exception:
        return None
    if not isinstance(store, dict) or not store:
        return None
    # Prefer the entry with the latest expires_at
    best_key = None
    best_entry: dict[str, Any] | None = None
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


def _token_expired(entry: dict[str, Any], skew_s: int = 120) -> bool:
    exp = entry.get("expires_at")
    if not exp:
        return False
    try:
        when = dt.datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
        now = dt.datetime.now(dt.timezone.utc)
        return when <= now + dt.timedelta(seconds=skew_s)
    except Exception:
        return False


def _refresh_grok(auth_key: str, entry: dict[str, Any]) -> dict[str, Any] | None:
    """Refresh OIDC access token and persist the new tokens atomically."""
    client_id = entry.get("oidc_client_id")
    if not client_id and "::" in auth_key:
        client_id = auth_key.split("::", 1)[1]
    refresh = entry.get("refresh_token")
    if not client_id or not refresh:
        return None

    try:
        _, discovery = fetch_json(
            GROK_OIDC_DISCOVERY,
            {"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        token_url = (discovery or {}).get("token_endpoint")
        if not token_url:
            return None
    except Exception:
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
        exp = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=int(expires_in))
        new_entry["expires_at"] = exp.isoformat().replace("+00:00", "Z")

    # Persist so subsequent polls (and the Grok CLI) keep working.
    try:
        store = json.loads(GROK_AUTH.read_text()) if GROK_AUTH.is_file() else {}
        if not isinstance(store, dict):
            store = {}
        store[auth_key] = new_entry
        fd, tmp = tempfile.mkstemp(
            prefix=".auth.", suffix=".json", dir=str(GROK_AUTH.parent)
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(store, f, indent=2)
                f.write("\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, GROK_AUTH)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception:
        # Still return the live token even if we couldn't write.
        pass

    return new_entry


def _money_val(obj: Any) -> int | None:
    if obj is None:
        return None
    if isinstance(obj, dict) and "val" in obj:
        try:
            return int(obj["val"])
        except Exception:
            return None
    if isinstance(obj, (int, float)):
        return int(obj)
    return None


def _parse_grok_period(cfg: dict[str, Any]) -> dict[str, Any]:
    """Parse one billing config into a period dict (weekly or monthly shape)."""
    on_demand = _money_val(cfg.get("onDemandCap") or cfg.get("on_demand_cap"))
    period = cfg.get("currentPeriod") or {}
    ptype = str(period.get("type") or "")
    label = (
        "Weekly" if "WEEKLY" in ptype
        else "Monthly" if "MONTHLY" in ptype
        else "Usage"
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
        end_ms = iso_to_ms(
            cfg.get("billingPeriodEnd") or cfg.get("billing_period_end")
        )
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


def fetch_grok() -> dict[str, Any]:
    loaded = _load_grok_auth()
    if not loaded:
        return {"ok": False, "error": "no-token"}
    auth_key, entry = loaded

    if _token_expired(entry):
        refreshed = _refresh_grok(auth_key, entry)
        if refreshed:
            entry = refreshed

    state = {"entry": entry}

    def get_cfg(url: str) -> tuple[int | None, dict[str, Any] | None]:
        def call(token: str) -> tuple[int, Any]:
            return fetch_json(
                url,
                {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )

        status, data = call(state["entry"]["key"])
        if status == 401:
            refreshed = _refresh_grok(auth_key, state["entry"])
            if not refreshed:
                return 401, None
            state["entry"] = refreshed
            status, data = call(state["entry"]["key"])
        if status != 200 or not isinstance(data, dict):
            return status, None
        return status, (data.get("config") or data)

    # Weekly (unified credits) + monthly ($ limit) are separate meters; show both.
    st_week, week_cfg = get_cfg(GROK_BILLING_URL + "?format=credits")
    st_month, month_cfg = get_cfg(GROK_BILLING_URL)

    periods: list[dict[str, Any]] = []
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

    if not periods:
        status = st_week or st_month
        if status == 401:
            return {"ok": False, "error": "http-401"}
        return {"ok": False, "error": f"http-{status}" if status else "net"}

    return {"ok": True, "plan": "Grok", "periods": periods}


# ── Codex ───────────────────────────────────────────────────────────────────


def _jwt_exp_ms(token: str) -> int | None:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        pad = "=" * ((4 - len(parts[1]) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
        exp = payload.get("exp")
        if exp is None:
            return None
        return int(exp) * 1000
    except Exception:
        return None


def _jwt_claim(token: str, *path: str) -> Any:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        pad = "=" * ((4 - len(parts[1]) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
        cur: Any = payload
        for key in path:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(key)
        return cur
    except Exception:
        return None


def _atomic_write_json(path: Path, obj: Any) -> None:
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _codex_window_label(window_seconds: int | None, name: str) -> str:
    """Map primary/secondary window duration to a human label."""
    if not window_seconds:
        return name.replace("_", " ").title()
    # 5-hour session windows are typically 18000s; weekly is 604800s.
    if window_seconds <= 6 * 3600:
        return "Current session"
    if window_seconds <= 2 * 86400:
        hours = max(1, round(window_seconds / 3600))
        return f"{hours}-hour"
    if 6 * 86400 <= window_seconds <= 8 * 86400:
        return "Weekly"
    if 28 * 86400 <= window_seconds <= 32 * 86400:
        return "Monthly"
    days = max(1, round(window_seconds / 86400))
    return f"{days}-day"


def _codex_window(block: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
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
    except Exception:
        window_s_i = None

    resets_ms = None
    reset_at = block.get("reset_at")
    if isinstance(reset_at, (int, float)):
        resets_ms = int(reset_at * 1000)
    else:
        after = block.get("reset_after_seconds")
        if isinstance(after, (int, float)):
            resets_ms = int(
                (dt.datetime.now(dt.timezone.utc).timestamp() + float(after)) * 1000
            )

    return {
        "label": _codex_window_label(window_s_i, name),
        "util": util,
        "resets_ms": resets_ms,
        "window_seconds": window_s_i,
        "kind": name,
    }


def _codex_reset_credits(data: dict[str, Any]) -> dict[str, Any]:
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


def _refresh_codex(auth: dict[str, Any]) -> dict[str, Any] | None:
    tokens = auth.get("tokens") or {}
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
    new_auth["last_refresh"] = dt.datetime.now(dt.timezone.utc).isoformat()

    try:
        _atomic_write_json(CODEX_AUTH, new_auth)
    except Exception:
        pass
    return new_auth


def fetch_codex() -> dict[str, Any]:
    if not CODEX_AUTH.is_file():
        return {"ok": False, "error": "no-token"}

    try:
        auth = json.loads(CODEX_AUTH.read_text())
    except Exception:
        return {"ok": False, "error": "no-token"}

    tokens = auth.get("tokens") or {}
    access = tokens.get("access_token")
    if not access:
        return {"ok": False, "error": "no-token"}

    account_id = tokens.get("account_id") or _jwt_claim(
        access, "https://api.openai.com/auth", "chatgpt_account_id"
    )

    exp_ms = _jwt_exp_ms(access)
    now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    if exp_ms is not None and exp_ms <= now_ms + 120_000:
        refreshed = _refresh_codex(auth)
        if refreshed:
            auth = refreshed
            tokens = auth.get("tokens") or {}
            access = tokens.get("access_token")
            account_id = tokens.get("account_id") or account_id

    def call(token: str) -> tuple[int, Any]:
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
        tokens = refreshed.get("tokens") or {}
        access = tokens.get("access_token")
        status, data = call(access)

    if status != 200 or not isinstance(data, dict):
        return {"ok": False, "error": f"http-{status}" if status else "net"}

    plan_type = data.get("plan_type") or "Codex"
    plan = str(plan_type).replace("_", " ").title()

    rate = data.get("rate_limit") or {}
    windows: list[dict[str, Any]] = []
    for key in ("primary_window", "secondary_window"):
        w = _codex_window(rate.get(key), key)
        if w:
            windows.append(w)

    # code_review_rate_limit may mirror the same shape
    cr = data.get("code_review_rate_limit")
    if isinstance(cr, dict):
        # either nested windows or a single window-like object
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

    credits = data.get("credits") or {}
    reset_credits = _codex_reset_credits(data)

    return {
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


# ── main ────────────────────────────────────────────────────────────────────


def main() -> None:
    claude: dict[str, Any]
    grok: dict[str, Any]
    codex: dict[str, Any]
    try:
        claude = fetch_claude()
    except Exception as e:
        claude = {"ok": False, "error": "net", "detail": str(e)}
    try:
        grok = fetch_grok()
    except Exception as e:
        grok = {"ok": False, "error": "net", "detail": str(e)}
    try:
        codex = fetch_codex()
    except Exception as e:
        codex = {"ok": False, "error": "net", "detail": str(e)}

    emit(
        {
            "ok": bool(claude.get("ok") or grok.get("ok") or codex.get("ok")),
            "claude": claude,
            "grok": grok,
            "codex": codex,
            "fetched_ms": int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000),
        }
    )


if __name__ == "__main__":
    main()
