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

### Fixed

- The documented dev gate and smoke path run as written: `uv run pytest` finds
  `scripts/` on the path, and `print_smoke.py` reports a missing or malformed
  dump instead of a traceback.

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
