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
- Python 3 (plasmashell runs the fetcher with `python3`)
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

After QML edits:

```bash
rm -rf ~/.cache/plasmashell/qmlcache
systemctl --user restart plasma-plasmashell.service
```

## Fetching

`package/contents/code/fetch_quota.py` is polled every 2 minutes.

| Provider | Endpoint | Credentials |
| --- | --- | --- |
| Claude | `GET https://api.anthropic.com/api/oauth/usage` | Claude Code OAuth |
| Cursor | `GET https://cursor.com/api/usage-summary` | Cursor IDE or cursor-agent session |
| Codex | `GET https://chatgpt.com/backend-api/wham/usage` | Codex ChatGPT OAuth |
| Grok | `GET https://cli-chat-proxy.grok.com/v1/billing` | Grok OIDC |

Tokens leave the machine only for those HTTPS calls. Grok, Codex, and Claude OAuth tokens are refreshed in place when near expiry.

Claude's usage API 429s unknown User-Agents. The fetcher sends Claude Code's User-Agent on that request, waits only for a short `Retry-After`, and reuses `~/.cache/quota-widget` when a usage call still 429s or 5xxs. Grok and Codex use that cache too. An expired Claude token whose refresh is also 429 is shown as rate-limited, not signed-out.

Smoke-test without Plasma:

```bash
python3 package/contents/code/fetch_quota.py > .scratch/smoke.json
python3 scripts/print_smoke.py .scratch/smoke.json
```

## Development

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.11+ (`uv` installs it).

```bash
uv sync --extra dev --frozen
```

`--frozen` is what CI uses, so a stale `uv.lock` fails here rather than after a push.

The full gate, same order as CI:

```bash
uv run black --check . && uv run ruff check . && uv run mypy && uv run pytest
```

While iterating, one file or one test at a time:

```bash
uv run pytest tests/test_fetch_quota.py -k cursor
uv run pytest tests/test_fetch_quota.py::IsoToMsTest
```

Tests are hermetic: no network, no credentials, no home-directory state. `uv run pytest` alone is a sub-second loop.

Conventions, branching, and how to add a test or a dependency: [CONTRIBUTING.md](CONTRIBUTING.md).

## Layout

```
package/metadata.json
package/contents/config/main.xml
package/contents/ui/main.qml
package/contents/code/fetch_quota.py
scripts/print_smoke.py
install.sh
```

## Disclaimer

Unofficial. Not affiliated with the vendors. Usage endpoints are reverse-engineered from their apps and can change. Use only with your own accounts.

## License

MIT
