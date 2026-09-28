# AI Quota

KDE Plasma 6 desktop and panel widget for live Claude, Cursor, Codex, and Grok usage quotas.

Four vendor dashboards. Numbers match the same endpoints the CLIs and websites already use, with credentials that are already on disk. The header toggle next to Refresh switches between bars and wrapping circular gauges.

## What it shows

**Claude** (claude.ai Settings, Usage): plan, 5-hour session %, weekly bars, extra credits when enabled.

**Cursor** (cursor.com/dashboard, Usage): plan, included usage for the billing cycle, Auto vs API bars when present, on-demand spend.

**Codex** (chatgpt.com/codex/settings/usage): plan, session and weekly windows, credit balance and limit resets when present.

**Grok**: weekly % (the CLI "Weekly limit left" line), monthly $ used / remaining, reset time. One bar when the API returns one period.

## Requirements

- KDE Plasma 6
- Python 3.11 or newer on `PATH` as `python3` (plasmashell runs the fetcher with it; `install.sh` refuses an older one)
- Claude Code logged in (`~/.claude/.credentials.json`)
- Cursor logged in (`~/.config/Cursor/User/globalStorage/state.vscdb`, or `~/.config/cursor/auth.json` from cursor-agent)
- Codex CLI logged in (`~/.codex/auth.json`)
- Grok CLI logged in (`~/.grok/auth.json`)

A provider with no token shows a sign-in line; the others still update.

## Install

```bash
./install.sh
```

Right-click the desktop or a panel, Add Widgets, search **AI Quota**.

The install links `package/` into `~/.local/share/plasma/plasmoids/com.maci.quota-widget`
(honouring `XDG_DATA_HOME`). Re-running it upgrades in place. To remove the widget
and keep your cache and settings:

```bash
./install.sh --uninstall
```

After QML edits, re-run `./install.sh` (it clears the plasmashell QML cache)
and restart plasmashell:

```bash
systemctl --user restart plasma-plasmashell.service
```

## Fetching

`package/contents/code/fetch_quota.py` is polled every 2 minutes (change it in the widget settings, see below). Only one run is in flight at a time: a poll that fires while the previous one is still going is dropped, and a run still going after 10 minutes is disconnected so the next tick starts a fresh one.

| Provider | Endpoint | Credentials |
| --- | --- | --- |
| Claude | `GET https://api.anthropic.com/api/oauth/usage` | Claude Code OAuth |
| Cursor | `GET https://cursor.com/api/usage-summary` | Cursor IDE or cursor-agent session |
| Codex | `GET https://chatgpt.com/backend-api/wham/usage` | Codex ChatGPT OAuth |
| Grok | `GET https://cli-chat-proxy.grok.com/v1/billing` | Grok OIDC |

Tokens leave the machine only for those HTTPS calls, and for the OAuth token endpoints used to refresh them: `https://platform.claude.com/v1/oauth/token` (with `https://console.anthropic.com/v1/oauth/token` as fallback), `https://auth.openai.com/oauth/token`, and the token endpoint Grok's `https://auth.x.ai/.well-known/openid-configuration` document names. Grok, Codex, and Claude OAuth tokens are refreshed in place when near expiry. Every provider rotates the refresh token it hands out, so refreshes run under an advisory lock in `~/.cache/quota-widget` and re-read the credential file once they hold it: a second poll (a second widget instance, a manual run, the install smoke test) reuses the token the first run already wrote instead of rotating it a second time and invalidating it.

Claude's usage API 429s unknown User-Agents. The fetcher sends Claude Code's User-Agent on that request, waits only for a short `Retry-After`, and reuses `~/.cache/quota-widget` when a usage call still 429s or 5xxs. Grok, Codex, and Cursor use that cache too. Each entry is stamped with a digest of the account that produced it and is read only by that account, for at most 24 hours; a credential that yields no account id caches nothing. An expired Claude token whose refresh is also 429 is shown as rate-limited, not signed-out.

Smoke-test without Plasma:

```bash
mkdir -p .scratch
python3 package/contents/code/fetch_quota.py > .scratch/smoke.json
python3 scripts/print_smoke.py .scratch/smoke.json
```

Both take `--help`. The fetcher prints one JSON object on stdout and keeps
diagnostics on stderr, so `fetch_quota.py > dump.json` is safe; `--print-config`
shows the paths and knobs it resolved, without reading a token.

## Development

Requires [`uv`](https://docs.astral.sh/uv/) 0.12.13 or newer (enforced by
`[tool.uv] required-version` in `pyproject.toml`, so an older one stops at the
first command with a version message), Python 3.11+ (`uv` installs it), and
[`shellcheck`](https://www.shellcheck.net/) for the `install.sh` leg of the gate.
Everything else the gate needs comes from `pyproject.toml`.

Two env vars make a run reproducible. `QUOTA_WIDGET_CACHE` relocates the payload cache, and `QUOTA_WIDGET_NOW_MS` pins the clock to a fixed epoch-milliseconds value, so the same HTTP responses produce byte-identical output on every run. Both are for tests and smoke runs; production leaves them unset.

Dev gate (`uv`):

```bash
uv sync --extra dev --frozen
```

`--frozen` is what CI uses, so a stale `uv.lock` fails here rather than after a push.

The full gate, same order as CI:

```bash
uv run black --check . && uv run ruff check . && uv run mypy && uv run pytest && shellcheck install.sh
```

While iterating, one file or one test at a time:

```bash
uv run pytest tests/test_fetch_quota.py -k cursor
uv run pytest tests/test_fetch_quota.py::IsoToMsTest
```

Tests are hermetic: no network, no credentials, no home-directory state. `uv run pytest` alone runs the whole suite in a few seconds.

Conventions, branching, and how to add a test or a dependency: [CONTRIBUTING.md](CONTRIBUTING.md).
What changed in each release, and what breaks when upgrading: [CHANGELOG.md](CHANGELOG.md).

## Configuration

### Widget settings

Right-click the widget, Configure, General. Stored by plasmashell in
`~/.config/plasmoids/org.kde.plasma.plasmoid/com.maci.quota-widget.json`.

| Setting | Default | Range | Meaning |
| --- | --- | --- | --- |
| `gaugeView` | `false` | | draw circular gauges instead of bars |
| `pollSeconds` | `120` | 30 to 3600 | how often the fetcher runs |
| `utilWarnAt` | `70` | 1 to 99 | percent used before a meter turns amber |
| `utilCritAt` | `90` | 1 to 100 | percent used before a meter turns red |

Out-of-range values in that file are clamped to the range above, not rejected.

### Fetcher environment

Every knob is a `QUOTA_WIDGET_*` variable, read once at startup and validated
before the first request. A value that is empty, relative (for paths),
unparsable, or out of range aborts the poll with `error: "config"` plus the
reason on stderr; no provider runs with a half-applied config.

| Variable | Default |
| --- | --- |
| `QUOTA_WIDGET_HOME` | `$HOME` |
| `QUOTA_WIDGET_CLAUDE_CREDENTIALS` | `$HOME/.claude/.credentials.json` |
| `QUOTA_WIDGET_CURSOR_AUTH` | `$XDG_CONFIG_HOME/cursor/auth.json` |
| `QUOTA_WIDGET_CURSOR_STATE_DB` | `$XDG_CONFIG_HOME/Cursor/User/globalStorage/state.vscdb` |
| `QUOTA_WIDGET_CODEX_AUTH` | `$HOME/.codex/auth.json` |
| `QUOTA_WIDGET_GROK_AUTH` | `$HOME/.grok/auth.json` |
| `QUOTA_WIDGET_CACHE` | `$XDG_CACHE_HOME/quota-widget` |
| `QUOTA_WIDGET_CACHE_MAX_AGE_S` | `86400` (0 < value <= 86400, whole seconds) |
| `QUOTA_WIDGET_HTTP_TIMEOUT` | `12.0` (0 < value <= 300, seconds) |
| `QUOTA_WIDGET_NOW_MS` | unset (integer epoch milliseconds; tests and smoke runs only) |

`XDG_CONFIG_HOME` and `XDG_CACHE_HOME` set to a relative path are ignored, per
the [base directory spec](https://specifications.freedesktop.org/basedir-spec/latest/).

Plasmashell does not read shell rc files, so a variable set in `.bashrc` never
reaches the widget. Export it into the user session before starting Plasma:

```bash
systemctl --user import-environment QUOTA_WIDGET_HTTP_TIMEOUT
```

Verify what the fetcher actually sees:

```bash
python3 package/contents/code/fetch_quota.py --print-config
```

That prints paths and the two numeric knobs. No token is read or printed.

No secret belongs in these variables: the fetcher takes every token from the
files above, so a session export leaks nothing. Override a credential path only
when the CLI stores it somewhere else.

## Local state

The widget keeps two kinds of file on disk, both under your home directory:

| Path | Contents | If lost |
| --- | --- | --- |
| `~/.cache/quota-widget/*.json` | Last successful usage payload per provider, used when an API call 429s or 5xxs | Nothing. Refills on the next successful poll. |
| `~/.claude/.credentials.json`, `~/.grok/auth.json`, `~/.codex/auth.json` | Rotated OAuth tokens, written by the fetcher during a refresh | Re-run that CLI's login. The widget never creates these. |

Token writes go through a temp file that is flushed and renamed, then the directory is flushed, so a crash leaves either the old tokens or the new ones. The token files are shared with the vendor CLIs: the fetcher re-reads and re-applies its refresh if a CLI writes the same file in between, so the two do not clobber each other's rotation. The cache files are disposable; deleting `~/.cache/quota-widget` costs one poll of 429 fallback.

## Data and privacy

The widget is local-only. It has no telemetry, no analytics, no crash reporting, and no network calls other than the four usage endpoints and the OAuth token refreshes named in the fetching section above. It never sends a request to a server it does not already name there.

What the fetcher reads from your account is what a usage bar needs: plan name, period percentages, reset times, and credit balances. Account identifiers (the WorkOS user id in the Cursor session, the ChatGPT account id header) are used to authorize a request. The raw value is never written to the cache, the emitted JSON, or any log; the cache stores a 16-character SHA-256 digest of it, which is what scopes an entry to one account. The card shows a status such as `http-429` and nothing more; the cause behind a failed call goes to stderr, which is the journal under Plasma, and that line carries the URL and the error, never a payload or an identifier. The body of a failed HTTP response is discarded rather than captured. `install.sh` writes one run's output to `.scratch/smoke.json` and the failure detail to `.scratch/smoke.err` in the checkout, both gitignored.

Retention:

| Data | Where | How long |
| --- | --- | --- |
| Usage payload cache | `~/.cache/quota-widget/*.json` | At most 24 hours (`DEFAULT_CACHE_MAX_AGE_S`, overridable with `QUOTA_WIDGET_CACHE_MAX_AGE_S`); an expired file is deleted when it is next read |
| OAuth tokens | vendor token files above | Rotated by the vendor's own expiry, written back only on refresh |

Both are written `0600` under your home directory, and the cache directory is `0700`. To erase everything the widget keeps, remove the cache directory and revoke the sessions from each vendor's account page; the token files belong to the CLIs, which rewrite them on the next login.

The full boundary, asset, and threat map is in [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).

## Layout

```
package/metadata.json
package/contents/config/main.xml
package/contents/ui/main.qml
package/contents/code/fetch_quota.py
package/contents/icons/com.maci.quota-widget.svg
scripts/print_smoke.py
tests/
docs/THREAT_MODEL.md
install.sh
```

`package/` is the whole plasmoid and the only thing `install.sh` links into
`~/.local/share/plasma/plasmoids/`.

## Disclaimer

Unofficial. Not affiliated with the vendors. Usage endpoints are reverse-engineered from their apps and can change. Use only with your own accounts.

## License

MIT
