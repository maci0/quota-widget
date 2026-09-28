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

- `QUOTA_WIDGET_NOW_MS` pins the fetcher clock, so the same HTTP responses print
  byte-identical output on every run. Documented in the README, for tests and
  smoke runs.
- `docs/THREAT_MODEL.md`: entry points, trust boundaries, assets, and the
  threats that apply to each, with file references.
- `--help` on the fetcher and on `print_smoke.py`, listing the flags, the
  environment variables, and the exit codes. Help is answered before the
  environment is read, so it works on a broken config.

### Fixed

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
- An unknown argument now prints the usage line next to the error, and a
  `print_smoke.py` config error goes to stderr so the provider lines on stdout
  stay clean when piped.
- The fetcher tests bind credential paths through the environment again, so
  `uv run pytest` is green.

### Changed

- The `error: "config"` payload no longer carries `fetched_ms`, since the clock
  override can be the value that failed. The panel falls back to its own clock.

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
