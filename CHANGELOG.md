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

### Added

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

### Changed

- `pyproject.toml` declares `[tool.uv] required-version = ">=0.12.13"`, the
  release CI pins, so a uv too old for the lockfile's revision 3 stops with a
  version message on the first command.
- The `error: "config"` payload no longer carries `fetched_ms`, since the clock
  override can be the value that failed. The panel falls back to its own clock.
- CI pins `actions/checkout` and `astral-sh/setup-uv` to the commit behind
  their version tag, and Dependabot (`.github/dependabot.yml`) opens the bump,
  so a moved tag can no longer change what the gate runs. The uv cache is keyed
  on `uv.lock` explicitly rather than by the action's default glob.

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
