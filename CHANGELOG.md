# Changelog

All notable changes to the AI Quota plasmoid, newest first. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/spec/v2.0.0.html). `package/metadata.json` holds the
version Plasma displays and is the single source of truth.

The widget's own contract with a running desktop is the JSON its fetcher prints
and the `main.xml` config keys. Changes to either are listed as breaking, and a
plasmoid installed from an older package can only read what its own fetcher
prints, so a fetcher and UI shipped together never break each other.

## [Unreleased]

Next release: 2.0.0. `CONTRIBUTING.md` classes a change to the fetcher JSON or
to a `main.xml` key that an older installed widget cannot read as a major, and
this window holds two of those, under Breaking below. `tests/test_release.py`
fails if a Breaking entry lands in this section without a matching next version.

### Breaking

- A `QUOTA_WIDGET_*` variable the fetcher does not read is now a configuration
  error instead of an ignored name. Before, a typo such as
  `QUOTA_WIDGET_CASH` left the poll succeeding with the intended setting
  quietly doing nothing; now the run aborts with `error: "config"` before any
  provider is called, naming the variable, and the panel shows the
  configuration failure. Every name this version reads is listed by
  `fetch_quota.py --help` and in the README table, and the two lists share one
  table in the source. To upgrade: remove or rename any `QUOTA_WIDGET_*`
  variable that is not in that list. A variable left over from a build older
  than the one that dropped it is the usual cause; the fetcher names it.
- A Cursor `403` is reported as `http-403`. Before, a rejected request was
  reported as `http-401`, which `errText()` renders as "Sign in to Cursor"; now
  it renders as "Unavailable", and a signed-out Cursor is still `http-401`. A
  consumer matching Cursor on `http-401` to mean a rejection matches
  `http-403` now.

### Added

- `QUOTA_WIDGET_HOME` relocates the home the other paths resolve against, and
  `QUOTA_WIDGET_CLAUDE_CREDENTIALS`, `QUOTA_WIDGET_CURSOR_AUTH`,
  `QUOTA_WIDGET_CURSOR_STATE_DB`, `QUOTA_WIDGET_CODEX_AUTH`, and
  `QUOTA_WIDGET_GROK_AUTH` name a credential file each. Every one is read
  through the same validated config as the rest and defaults to the path the
  vendor CLI writes, so an unset variable changes nothing. Documented in the
  README.
- `QUOTA_WIDGET_HTTP_TIMEOUT` sets the per-request timeout, 0 < s <= 300,
  default 12.
- Three `main.xml` config keys, all with a default, so a config file written by
  an older package reads them without a migration: `pollSeconds` (default 120,
  clamped to 30 to 3600), `utilWarnAt` (default 70, 1 to 99), and
  `utilCritAt` (default 90, 1 to 100). Documented in the README.
- Seeded randomized fuzzing for the parsers that read untrusted input: the
  Cursor usage-summary body, the Cursor `ItemTable` cells, and vendor JWTs
  (`tests/test_fuzz_parsers.py`). A failure prints the seed that reproduces it.
- `QUOTA_WIDGET_NOW_MS` pins the fetcher clock, so the same HTTP responses print
  byte-identical output on every run. Documented in the README, for tests and
  smoke runs.
- `docs/THREAT_MODEL.md`: entry points, trust boundaries, assets, and the
  threats that apply to each, with file references.
- `--help` on the fetcher and on `print_smoke.py`, listing the flags, the
  environment variables, and the exit codes. Help is answered before the
  environment is read, so it works on a broken config.
- `install.sh --uninstall` removes the installed widget and leaves the provider
  cache and the plasmoid settings in place.

### Fixed

- A `QUOTA_WIDGET_NOW_MS` past the year 9999 passed the config check and then
  raised `OverflowError` inside every provider, after the Codex refresh POST had
  already retired the stored refresh token. The pin is now bounded to the range
  `now_utc()` can represent, so an impossible clock is a config error like every
  other knob.
- A Claude session whose refresh token the provider rejected was reported as
  `http-429`, and the card read "Rate-limited" until the user cleared the sign-in
  line that never came. The 429 label now depends on the refresh being
  throttled, not on a refresh token merely being present, so a revoked session
  asks for a fresh sign-in.
- `spend.exponent`, `spend.currency`, `extra_usage.currency`, Codex
  `rate_limit.allowed`, and the Codex credit balance reached the widget JSON with
  whatever type and value the wire held. A `NaN`, an `Infinity`, or a non-string
  currency there printed JSON plasmashell's parser rejects, blanking the view.
  Each is checked like every other number the fetcher passes on.
- A stalled fetcher run is dropped by a one-shot QML timer instead of by
  subtracting two `Date.now()` readings. The wall clock steps backwards on an
  NTP correction or a manual set, and a negative elapsed time never reached
  `pollTimeoutMs`, so the source stayed connected and no later poll ever
  started.
- A provider timestamp sent as a bare epoch number (`billingCycleEnd`,
  `resets_at`) is read in the unit it arrived in, seconds or milliseconds,
  instead of being dropped as unparsable and showing no reset at all.
- The gate was red on a clean tree: `ruff` reported an unparameterized table
  name and a needless comprehension in the Cursor `state.vscdb` fixture. The
  table name is now checked against the names the fixture builds, so `CI` is
  green again.
- `install.sh` fetched from every provider and wrote `.scratch/` before it
  decided whether the install could go ahead. A refused install now refuses
  first, leaving the checkout and the plasmoid directory untouched.
- `install.sh` ran the fetcher with whatever `python3` was on `PATH`. Below
  3.11 (`dt.UTC`, the floor in `pyproject.toml`) the widget showed a broken
  data source with no reason given; the install now stops with the version it
  found.
- The wait for the OAuth refresh lock reads its deadline and its poll interval
  through the same seams the retry backoff already used, instead of calling
  `time.monotonic` and `time.sleep` directly. A contended poll no longer spends
  real seconds in the suite, and a replay of one is driven by an injected
  clock. A deadline read through the pinned wall clock would have expired on
  the first contended poll, since a pinned clock never advances.
- `QUOTA_WIDGET_CACHE_MAX_AGE_S` shortened the fetcher's own cache but not the
  panel's, which kept a reading on screen for 24 h regardless. Each poll now
  reports the effective window as `cache_max_age_s` and the widget ages a kept
  reading against it.
- `QUOTA_WIDGET_NOW_MS` accepted a negative value, dating every reading before
  the epoch.
- A Grok `used`/`limit` pair whose percent overflows (`100 * 1.7e308`) printed
  a bare `Infinity` in the widget JSON, which plasmashell's parser rejects, and
  a negative Cursor `limit` flipped the meter negative instead of reading as no
  ratio.
- A Grok on-demand cap of `0` was dropped for the snake_case fallback key, so a
  plan forbidden from on-demand spend reported itself as uncapped.
- A Codex credits balance or reset-credit count, and a Claude currency or spend
  exponent, no longer reach the widget JSON with a non-finite number or an
  untyped value the UI cannot format.
- `uv run ruff check .` failed on the Cursor `ItemTable` fixture in
  `tests/test_fetch_quota.py` (`S608`, `C416`), so the gate was red for
  everyone before a first change.
- A Cursor response whose `membershipType` is not a string, or whose
  `billingCycleEnd` is not a date string, raised out of the parser and blanked
  the card instead of reading as unknown.
- A `used`/`limit` pair whose ratio overflows (`1e308` over `1e-308`) printed a
  bare `Infinity` in the widget JSON, which plasmashell's parser rejects.
- A Cursor `ItemTable` cell holding a lone surrogate (`"\ud800"`) reached the
  header quote and the cache write, where the encode fails. Such a cell is
  dropped, like any other undecodable one.
- `limitType` reached the widget JSON with whatever type the wire held; only a
  string is passed on now. `isUnlimited` likewise counts only a real `true`.
- The install stops instead of deleting whatever sits at the plasmoid directory
  when that is not this widget, so a copy from Plasma Discover, a distro
  package, or a hand-unpacked archive survives an upgrade.
- Every provider payload now carries `fetched_ms`, the instant the reading was
  taken. The plasmoid ages a kept reading by that stamp instead of by when it
  arrived, so replaying a cached payload no longer resets the 24 h stale window
  and stretches the oldest reading a widget can show to two windows.
- A malformed `QUOTA_WIDGET_NOW_MS` is reported as a configuration error before
  any provider runs, instead of raising out of the emit path and leaving
  plasmashell with no JSON for that poll.
- The Codex and Grok cards mark a reading served from the cache the way the
  Claude and Cursor cards already did.
- Non-finite readings (`NaN`, `1e400`) from a provider no longer reach the JSON
  the plasmoid parses, and no longer read as a clamped 0% or a full 100%.
- Seconds-to-milliseconds conversion rounds instead of truncating, so a reset
  timestamp in whole milliseconds is not reported one millisecond early.
- Claude and Grok token expiry is read through the pinnable clock, so a replay
  run sees the same expiry decision.
- Money arriving as a floating-point cent amount rounds to the nearest cent
  rather than losing one to truncation, and a zero or negative spend limit no
  longer feeds the percent division.
- A minor-unit amount with `exponent` 0 displays in whole units instead of
  cents.
- The documented dev gate and smoke path run as written: `uv run pytest` finds
  `scripts/` on the path, and `print_smoke.py` reports a missing or malformed
  dump instead of a traceback.
- A dropped connection is retried once on a usage read, and the URL with the
  underlying error reaches the journal. A token refresh is never retried.
- A provider that raises is reported with its traceback on stderr instead of
  being passed off as a network error.
- An OAuth token that rotates but cannot be written back to disk is reported.
  Left silent, the next poll refreshed again and could sign the user out of the
  vendor CLI.
- `QUOTA_WIDGET_NOW_MS` is validated at startup: a malformed value aborts with
  `error: "config"` instead of raising mid-poll, and the Codex refresh and
  expiry checks read the pinned clock, so a replayed poll makes the same
  refresh decision on every run.
- The refresh-race, replay, and error-body tests point the fetcher's
  configuration at their temp files instead of module constants that no longer
  exist.
- The README no longer claims four network calls (there are also the OAuth token
  refreshes), a two-hour cache (the window is 24 hours), or that the account id
  is absent from the cache (it is stored as a digest).
- A fetcher run that never returns no longer blocks every later poll: the
  widget drops a run older than `pollTimeoutMs` and starts a fresh one.
- A token or cache write that fails for any reason other than an OS error
  (unserializable value, encoding error) no longer leaves a temp file in the
  token store, and the write is UTF-8 rather than the locale encoding.
- An HTTP error response is closed after its body is discarded, instead of
  relying on the process exit to release the socket.
- `ruff` passes again: a dead `point_credential` helper in the test module
  shadowed the one above it, so the gate and CI were red on a clean tree.
- An unknown argument now prints the usage line next to the error, and a
  `print_smoke.py` config error goes to stderr so the provider lines on stdout
  stay clean when piped.
- The fetcher tests bind credential paths through the environment again, so
  `uv run pytest` is green.
- A token merge-write that loses every attempt to a concurrent writer is
  reported on stderr. The write is dropped either way, so the journal is the
  only place that can say the store is being contested.
- A provider cache entry that cannot be written (a full disk, a read-only cache
  directory) is reported on stderr. It is the only fallback a later poll has
  when the vendor API fails.
- A Cursor `state.vscdb` that cannot be read for any reason other than the IDE
  holding its write lock is reported on stderr. It otherwise read as a signed
  out Cursor, and the fix was to sign in again over a db that was never
  readable.
- An unresolvable home directory is a configuration error, so the run prints
  `error: "config"` and leaves the panel a payload to read instead of raising
  out of the config load.
- `print_smoke.py` reports a dump it cannot read as a message rather than a
  traceback.

### Changed

- The three text sizes the widget draws (the panel reading, a card title, the
  number inside a gauge) are named steps in the `main.qml` token block instead
  of three unrelated factors typed at their call sites, and the panel reading is
  now the largest of the three. The panel rule and the tightest gap in a meter
  row are named there too, beside `markThickness`.
- A poll that produced no payload reads "Quota poll did not run" rather than
  "Offline": a missing `python3`, an unreadable fetcher, and a crash are none of
  them a network condition.
- The panel reads "n/a" (the word a meter already uses for a missing value)
  instead of a bare "!", and drops the label under it when no provider answered,
  so it no longer claims a reading it does not have.
- `pyproject.toml` declares `[tool.uv] required-version = ">=0.12.13"`, the
  release CI pins, so a uv too old for the lockfile's revision 3 stops with a
  version message on the first command.
- The `error: "config"` payload no longer carries `fetched_ms`, since the clock
  override can be the value that failed. The panel falls back to its own clock.
  No release carried the field on that payload, so this narrows a shape added
  in this same window rather than removing one a user depends on.
- CI pins `actions/checkout` and `astral-sh/setup-uv` to the commit behind
  their version tag, and Dependabot (`.github/dependabot.yml`) opens the bump,
  so a moved tag can no longer change what the gate runs. The uv cache is keyed
  on `uv.lock` explicitly rather than by the action's default glob.
- The dev tools are floored at the release the tree was verified against
  (`black>=26.5,<27`, `mypy>=2.3,<3`, `pytest>=9.1,<10`, `ruff>=0.16,<1`). The
  old ranges admitted black 24 and 25, mypy 1.x, and pytest 8, so a re-lock
  could swap the formatter, type checker, or test runner under a green gate.
  Dependabot now watches the `uv` ecosystem too, and a test fails when a
  requirement loses its floor or its cap.

## [1.1.0] - 2026-09-28

### Added

- Cursor usage from the Cursor IDE session or the cursor-agent token, shown
  beside the other providers.
- A header toggle switching between bars and wrapping circular gauges.
- A payload cache in `~/.cache/quota-widget` (override with
  `QUOTA_WIDGET_CACHE`), so a provider that rate-limits keeps the last good
  reading instead of an error.

### Fixed

- The README named a 60s poll; the timer has run every 2 minutes since 1.0.0.

## [1.0.0] - 2026-09-28

### Added

- Claude, Codex, and Grok usage on the desktop and in a panel, read from each
  vendor's own endpoint with credentials already on disk.
- Per-provider sign-in line when a token is missing; the other providers keep
  updating.
