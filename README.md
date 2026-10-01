# AI Quota

KDE Plasma 6 desktop and panel widget for live Claude, Cursor, Codex, and Grok usage quotas.

Four vendor dashboards. Numbers match the same endpoints the CLIs and websites already use, with credentials that are already on disk. The header toggle next to Refresh switches between bars and wrapping circular gauges.

## What it shows

**Claude** (claude.ai Settings, Usage): plan, 5-hour session %, weekly bars, extra credits when enabled.

**Cursor** (cursor.com/dashboard, Usage): plan, included usage for the billing cycle, Auto vs API bars when present, on-demand spend.

**Codex** (chatgpt.com/codex/settings/usage): plan, session and weekly windows, credit balance and limit resets when present.

**Grok**: weekly % (the CLI "Weekly limit left" line), monthly $ used / remaining, reset time. One bar when the API returns one period.

The fetcher also supplies **OpenCode Go** session, weekly and monthly percentages and reset times to the Quickshell widget in `../dotfiles/quickshell/Quota.qml`. Connect Go with OpenCode's `/connect` command; the fetcher reads the saved `opencode-go` API key. The Plasma UI still displays the four providers above.

## Requirements

- KDE Plasma 6
- Python 3.11 or newer on `PATH` as `python3` (plasmashell runs the fetcher with it; `install.sh` refuses an older one)
- Claude Code logged in (`~/.claude/.credentials.json`)
- Cursor logged in (`$XDG_CONFIG_HOME/Cursor/User/globalStorage/state.vscdb`, or `$XDG_CONFIG_HOME/cursor/auth.json` from cursor-agent; `~/.config` when that variable is unset or relative)
- Codex CLI logged in (`~/.codex/auth.json`)
- Grok CLI logged in (`~/.grok/auth.json`)

A provider with no token shows a sign-in line; the others still update.

## Install

```bash
./install.sh
```

Right-click the desktop or a panel, Add Widgets, search **AI Quota**.

The install links `package/` into `~/.local/share/plasma/plasmoids/com.maci.quota-widget`
(honouring `XDG_DATA_HOME`). Re-running it upgrades in place. The package carries
`package/metainfo.xml` as well as `package/metadata.json`, so Plasma Discover and
KNewStuff can list and update it; a release bumps its version in both plus
`pyproject.toml`. To remove the widget
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

Tokens leave the machine only for those HTTPS calls, and for the OAuth token endpoints used to refresh them: `https://platform.claude.com/v1/oauth/token` (with `https://console.anthropic.com/v1/oauth/token` as fallback), `https://auth.openai.com/oauth/token`, and the token endpoint Grok's `https://auth.x.ai/.well-known/openid-configuration` document names. Grok, Codex, and Claude OAuth tokens are refreshed in place when near expiry. Each of those three rotates the refresh token it hands out (Cursor has no refresh path, only a session token), so refreshes run under an advisory lock in `~/.cache/quota-widget` and re-read the credential file once they hold it: a second poll (a second widget instance, a manual run, the install smoke test) reuses the token the first run already wrote instead of rotating it a second time and invalidating it.

Claude's usage API 429s unknown User-Agents. The fetcher sends Claude Code's User-Agent on that request, waits only for a short `Retry-After`, and reuses `~/.cache/quota-widget` when a usage call still 429s, 5xxs, or never reaches the vendor. Grok, Codex, and Cursor use that cache too. Each entry is stamped with a digest of the account that produced it and is read only by that account, for at most 24 hours; a credential that yields no account id caches nothing. The cache is served when a usage call is rate-limited, fails with a 5xx, or never reaches the vendor at all. An expired Claude token whose refresh is also 429 is shown as rate-limited, not signed-out. A poll that runs long is dropped by the panel but can still answer after the poll that replaced it, and an entry is never replaced by a reading taken earlier: the cache and the cards only move forward, so a late run cannot rewind them.

Smoke-test without Plasma:

```bash
mkdir -p .scratch
python3 package/contents/code/fetch_quota.py > .scratch/smoke.json
python3 scripts/print_smoke.py .scratch/smoke.json
```

All three scripts take `--help`, and `install.sh` also takes `--version`, which
prints the released version from `package/metainfo.xml`. The fetcher prints one
JSON object on stdout and keeps diagnostics on stderr, so
`fetch_quota.py > dump.json` is safe; `--print-config`
shows the paths and knobs it resolved, without reading a token, and
`--clear-cache` deletes the cached readings and the key they are scoped by (see
[Data and privacy](#data-and-privacy)). An argument none of them lists is a
usage error: exit 2, the error and the usage line on stderr, and nothing on
stdout.

## Development

Requires [`uv`](https://docs.astral.sh/uv/) 0.12.13 or newer (enforced by
`[tool.uv] required-version` in `pyproject.toml`, so an older one stops at the
first command with a version message), Python 3.11+ (`uv` installs it), and
[`shellcheck`](https://www.shellcheck.net/) for the `install.sh` leg of the gate.
Everything else the gate needs comes from `pyproject.toml`.

Three env vars make a run reproducible. `QUOTA_WIDGET_CACHE` relocates the payload cache, `QUOTA_WIDGET_NOW_MS` pins the clock to a fixed epoch-milliseconds value, and `QUOTA_WIDGET_ACCOUNT_SALT` names the key account digests are taken under, as 64 hex characters. With all three set, the same HTTP responses produce byte-identical output on every run, first run included. All three are for tests and smoke runs; production leaves them unset.

Dev gate (`uv`):

```bash
uv sync --extra dev --locked
```

`--locked` is what CI uses: it fails when `uv.lock` no longer matches
`pyproject.toml`, so a stale lock is caught here rather than after a push.
`.python-version` names the interpreter, at a full patch version, so the gate
runs the same Python on every machine.

The full gate, which is the same script CI runs:

```bash
./scripts/gate.sh
```

It syncs the locked dev environment, then black, ruff, mypy, pytest, and
shellcheck, with `TZ=UTC` and `LC_ALL=C.UTF-8` so a local run and a CI run
answer the same questions.

While iterating, one file or one test at a time:

```bash
uv run pytest tests/test_fetch_quota.py -k cursor
uv run pytest tests/test_fetch_quota.py::IsoToMsTest
```

Tests are hermetic: no network, no credentials, no home-directory state. `tests/conftest.py` points the fetcher at a temp home and cache before collection, and fails any test that leaves it configured against the real one. `uv run pytest` alone runs the whole suite; the seeded fuzzer is most of the minute.

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
| `utilWarnAt` | `70` | 1 to 99 | percent used before a meter is labelled "high" |
| `utilCritAt` | `90` | 1 to 100 | percent used before a meter is labelled "critical" |

Out-of-range values in that file are clamped to the range above, not rejected.
`utilCritAt` is also raised to `utilWarnAt` when it sits below it, so a reading
can never be labelled "critical" before it is labelled "high".

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
| `QUOTA_WIDGET_OPENCODE_AUTH` | `$XDG_DATA_HOME/opencode/auth.json` (`$HOME/.local/share` by default) |
| `QUOTA_WIDGET_CACHE` | `$XDG_CACHE_HOME/quota-widget` |
| `QUOTA_WIDGET_CACHE_MAX_AGE_S` | `86400` (0 < value <= 86400, whole seconds) |
| `QUOTA_WIDGET_ACCOUNT_SALT` | unset (64 hex characters; tests and smoke runs only) |
| `QUOTA_WIDGET_HTTP_TIMEOUT` | `12.0` (0 < value <= 300, seconds) |
| `QUOTA_WIDGET_NOW_MS` | unset (integer epoch milliseconds, from 0 to `253402300799999`; tests and smoke runs only) |

`XDG_CONFIG_HOME`, `XDG_DATA_HOME` and `XDG_CACHE_HOME` set to a relative path are ignored, per
the [base directory spec](https://specifications.freedesktop.org/basedir-spec/latest/).

A `QUOTA_WIDGET_*` name the fetcher does not read is a configuration error, not
an ignored variable: a typo such as `QUOTA_WIDGET_CASH` aborts the poll naming
the offender, instead of leaving the setting quietly doing nothing. `XDG_*` and
every other variable belong to the environment and are left alone.

`QUOTA_WIDGET_CACHE_MAX_AGE_S` sets the window for both caches. Each poll
reports the value it is using as `cache_max_age_s`, so the panel ages a kept
reading against the same number instead of a constant of its own.

`QUOTA_WIDGET_HTTP_TIMEOUT` has the same relationship with the panel's poll
watchdog. The panel drops a run that outlasts its watchdog, and at the top of
the accepted range a poll is entitled to take longer than the panel's own ten
minutes, so every such poll was dropped and reported as a failure with no
payload behind it. Each poll now reports the budget its timeout adds up to as
`poll_timeout_s`, and the panel waits for the longer of that and its own
default.

`QUOTA_WIDGET_ACCOUNT_SALT` supplies the account-salt key instead of letting
the run make one. Leave it unset in a session: the fetcher then draws 32 bytes
from `os.urandom` and keeps them in `account-salt` next to the cache entries
they scope. Set it and that key is used for the run and written nowhere, so a
replay reproduces the `account` digest in every card, and the next unset poll
keeps the random key it would have had. A key already in the cache directory
wins over a named one, since the entries beside it were taken under it.

Plasmashell does not read shell rc files, so a variable set in `.bashrc` never
reaches the widget. Export it into the user session before starting Plasma:

```bash
systemctl --user import-environment QUOTA_WIDGET_HTTP_TIMEOUT
```

Verify what the fetcher actually sees:

```bash
python3 package/contents/code/fetch_quota.py --print-config
```

That prints paths, the numeric knobs, and the poll budget the timeout adds up
to. No token is read or printed.

No secret belongs in these variables: the fetcher takes every token from the
files above, so a session export leaks nothing. Override a credential path only
when the CLI stores it somewhere else.

## Local state

Everything the widget writes to disk, and what it costs to lose each:

| Path | Contents | If lost |
| --- | --- | --- |
| `~/.cache/quota-widget/*.json` | Last successful usage payload per provider, used when an API call 429s, 5xxs, or does not reach the vendor | Nothing. Refills on the next successful poll. |
| `~/.cache/quota-widget/account-salt` | The per-installation key every entry's account digest is taken under | Nothing on its own. The next poll mints a new one, and every entry written under the old key stops matching, so the entries it scoped read as another account's and are ignored. |
| `~/.cache/quota-widget/refresh.lock`, `~/.cache/quota-widget/*.json.lock` | Empty lock files, held open with `flock` while a poll is in its critical section | Nothing. Created again on the next poll. Never copy a lock: a copy of one is a file two polls can hold at once. |
| `~/.config/plasmoids/org.kde.plasma.plasmoid/com.maci.quota-widget.json` | The widget settings above, written by plasmashell | Re-set them in Configure, or copy the file back. The widget cannot regenerate this one. |
| `~/.claude/.credentials.json`, `~/.grok/auth.json`, `~/.codex/auth.json`, `~/.config/cursor/auth.json` | Rotated OAuth tokens, written by the fetcher during a refresh | Re-run that CLI's login. The widget never creates these, and it cannot restore them. |

Token writes go through a temp file that is flushed and renamed, then the directory is flushed, so a crash leaves either the old tokens or the new ones. The token files are shared with the vendor CLIs: the fetcher re-reads and re-applies its refresh if a CLI writes the same file in between, so the two do not clobber each other's rotation. The cache files are disposable; deleting `~/.cache/quota-widget` costs one poll of 429 fallback.

The widget backs nothing up itself, and there is no restore procedure to run: nothing here is authoritative except the token files, and those belong to the CLIs. A restore, if you make one, is two rules. Take the settings file, since nothing in the widget rebuilds it. If you take the cache directory, take `account-salt` out of it in the same copy, because an entry whose key stayed behind is a reading the fetcher will not serve, and the panel falls back to a live poll with no way to tell you why. Leave the lock files behind. Do not put the token files in a backup: to undo a leak there, revoke the session from the vendor's account page instead. The panel holds its copy of a reading in memory only, so a plasmashell restart drops it and the next poll refills it.

`--clear-cache` is the one path that deletes in bulk, and it names what it removed on stdout. It takes the entries and `account-salt` together and leaves the locks; see the erasure section under [Data and privacy](#data-and-privacy).

## Data and privacy

The widget is local-only. It has no telemetry, no analytics, no crash reporting, and no network calls other than the usage endpoints, the OAuth token refreshes, and Grok's OIDC discovery document, all named in the fetching section above. It never sends a request to a server it does not already name there.

Go usage comes from `GET https://opencode.ai/zen/go/v1/usage` with the saved API key as a Bearer token. The auth file is read only. Its cache is scoped to a keyed digest of that key because the endpoint returns no account identifier; rotating the key invalidates the cached reading. The key itself is never included in the payload or cache. See the [endpoint source](https://github.com/anomalyco/opencode/blob/dev/packages/console/app/src/routes/zen/go/v1/usage.ts).

What the fetcher reads from your account is what a usage bar needs: plan name, period percentages, reset times, and credit balances. Account identifiers (the WorkOS user id in the Cursor session, the ChatGPT account id header) are used to authorize a request. The raw value is never written to the cache, the emitted JSON, or any log; the cache stores a 16-character digest of it taken under a per-installation key kept beside the cache entries, which is what scopes an entry to one account, and that digest (never the value) travels in each provider payload as `account` so the panel scopes the reading it keeps through a failed poll the same way. A poll whose account digest differs from the one a card is holding drops it instead of showing another account's numbers. The card shows a status such as `http-429` and nothing more. Two codes are the fetcher's own rather than a vendor's answer, and are named so the panel does not read them as a verdict about your account: `net` is a request that never got a response, and `refused` is a request the fetcher declined to send, because a redirect would have carried your credential to another host. Both hold the card you have, since nothing about the account or the vendor changed; a newer widget is what clears a `refused`, because the refusal is the fetcher's own and repeats. A third, `bad-body`, is a provider that answered with a body the fetcher could not read (over the 4 MiB cap, empty, or not JSON): the vendor did answer, so passing its own `200` on would report a reading that was never measured, and it carries `transient` like a `5xx` or a dropped connection, which the next poll usually clears. Every `http-<code>` is a status the vendor returned. Every failed provider also carries `transient`, the fetcher's own classification of whether a cached reading beats reporting the failure, which is what the panel acts on when it decides to hold a card; the cause behind a failed call goes to stderr, which is the journal under Plasma, and that line carries the URL and the error, never a response body: the body of a failed HTTP response is drained and discarded rather than captured. A token refresh that fails is named there too, since the card shows the same "no token" for a throttled exchange and for a revoked one. Every warning the fetcher prints spells a path under your home directory as `~`, so the account name in it does not outlive the poll in the journal or on the card. A provider that crashes instead of returning writes its exception text and a traceback to the same stream, the traceback through the same redaction: every frame of it names a file under the checkout, which sits under your home directory. That text is built from whatever the vendor sent, so the journal is still not a place to paste a value you care about. `QUOTA_WIDGET_NOW_MS` pins the fetcher's clock, and is for tests and one-off runs only; leave it unset in a normal session. `install.sh` writes one run's output to `.scratch/smoke.json` and the failure detail to `.scratch/smoke.err` in the checkout, both gitignored.

Retention:

| Data | Where | How long |
| --- | --- | --- |
| Usage payload cache | `~/.cache/quota-widget/*.json` | At most 24 hours (`DEFAULT_CACHE_MAX_AGE_S`, overridable with `QUOTA_WIDGET_CACHE_MAX_AGE_S`); an expired file is deleted when it is next read, whichever account asks |
| Account digest key | `~/.cache/quota-widget/account-salt` | Created on the first poll that reaches an account digest, so a machine with no CLI signed in has none yet, and never replaced, so every reading on the machine is scoped by one key; removed by `--clear-cache` |
| OAuth tokens | vendor token files above | Rotated by the vendor's own expiry, written back only on refresh |

Both are written `0600` under your home directory, and the cache directory is `0700`, tightened on every poll if it was created with a wider mode. To erase everything the widget keeps, run

```bash
python3 package/contents/code/fetch_quota.py --clear-cache
```

which deletes the cached readings and the key they were scoped by, and then revoke the sessions from each vendor's account page; the token files belong to the CLIs, which rewrite them on the next login. Deleting the cache directory yourself has the same effect on the readings.

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
