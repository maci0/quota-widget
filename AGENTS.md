# Agent Rules

Inherits the universal rules in `~/.agents/AGENTS.md`. Local notes below; they do not weaken parent rules.

## What this is

KDE Plasma 6 plasmoid (`com.maci.quota-widget`). QML UI in `package/contents/ui/main.qml`. Data source in `package/contents/code/fetch_quota.py`, polled by plasmashell's `executable` engine.

## Runtime Python

Plasmashell runs `python3 package/contents/code/fetch_quota.py`. That path is system Python on purpose: the widget has no venv at display time. Do not switch the QML command to `uv run`.

Every wall-clock read goes through `now_ms()` / `now_utc()`; `QUOTA_WIDGET_NOW_MS` pins the clock so a poll replays byte-for-byte. Call `time.time()` or `datetime.now()` directly anywhere else in the fetcher and the replay guarantee is gone. A wait that ends on a deadline (the refresh lock, currently) reads `monotonic()` and pauses through `sleep()` instead: a pinned wall clock never advances, so a deadline taken through `now_ms()` would expire on the first poll and a raw `time.sleep` would cost real seconds. Nothing in the fetcher calls `time.monotonic` or `time.sleep` directly.

A provider timestamp without an offset is UTC. Parse it with `iso_to_utc()`, never `datetime.fromisoformat(...).timestamp()`: that resolves a naive value in plasmashell's host zone, so the same reading lands hours off outside UTC and shifts again at every DST transition.

Every credential, cache, and state file is UTF-8 JSON, read through `fetch_quota._read_text()` and written by `_atomic_write_json()`. Never `Path.read_text()` bare: plasmashell can start under a C locale where `open()` defaults to ASCII, and the merge-write fallback would then rewrite a shared store without the fields it could not decode.

Dev and CI use `uv`. The gate is at the end of this file; run it after any edit.

## Polling

The QML owns the fetcher process. One run at a time: `exec.poll()` returns early while a source is connected, `onNewData` disconnects it, and a run older than `pollTimeoutMs` (10 min, in the token block with the other timing values) is disconnected so a stalled fetch cannot wedge polling. Keep the release on every path that starts a run.

## Layout

- `package/`: plasmoid (metadata, QML, fetcher, `contents/config/main.xml` defaults, `contents/icons/com.maci.quota-widget.svg`)
- `docs/THREAT_MODEL.md`: entry points, trust boundaries, assets, and the threats per boundary
- `tests/`: pytest. One file per module under test (`test_fetch_quota.py`,
  `test_main_qml.py`, `test_package_metadata.py`, `test_print_smoke.py`,
  `test_release.py`); `project_paths.py` is the only shared helper and holds
  `project_root()`.
- `tests/test_fuzz_parsers.py`: seeded randomized fuzzing of the parsers fed
  untrusted input (the Cursor usage-summary body, `ItemTable` cells, JWTs).
  Generators are seeded so a failure reproduces; raise `ITERATIONS` or move
  `BASE_SEED` to widen a run.
- `scripts/print_smoke.py`: prints a fetched JSON dump (`install.sh` writes `.scratch/smoke.json`)
- `install.sh`: root symlink installer
- `.scratch/`: gitignored local scratch (never `/tmp`)

Project marker: `package/metadata.json`. Scripts walk up to that file.

`install.sh` deletes `~/.local/share/plasma/plasmoids/com.maci.quota-widget` before
symlinking, but only when that path is a symlink or holds a `metadata.json` naming
`com.maci.quota-widget`; anything else there is left alone and the run stops.
`./install.sh --uninstall` removes the widget and keeps the cache and the plasmoid
config. Run the script only when the user asks to install, upgrade, or remove, never
as a build or test step.

## Look

`package/contents/ui/main.qml` holds the type scale, dimming steps, meter thickness, and provider marks in one token block at the top. Add a value there instead of an inline literal, so the panel, the popup, and the gauges stay on one scale.

Provider marks: Claude and Codex use their published brand color. Cursor and Grok are monochrome brands, so they use `Kirigami.Theme` neutrals. Do not invent a hue for a vendor without one.

## Locale

Every user-facing string in `main.qml` goes through `qsTr()` with `%1`-style
placeholders, never concatenation, so a translator can reorder the sentence.
Dates and times render through `Qt.DefaultLocaleShortDate`, amounts through
`Number.toLocaleString(Qt.locale().name, { style: "currency" })`, and
percentages and counts through `numStr()`. A hardcoded `"$"`, a `"ddd h:mm AP"`
format, or a bare `toLocaleString()` with no locale argument all render English
numbering in every locale; `tests/test_main_qml.py` fails on each of them.

`anchors.left`, `anchors.right`, and `anchors.horizontalCenter` are logical
edges in QML and mirror themselves in a right-to-left layout, so the meter fill
uses them as-is. Physical edge math does not.

The applet icon is `package/contents/icons/com.maci.quota-widget.svg`, a 270 degree gauge arc matching the in-app meter. It is a dark rim under a light fill so it reads on both panel themes; keep that pairing if it is redrawn.

## Providers

Fetcher talks to each vendor's own usage endpoint with credentials already on disk (Claude Code, Cursor IDE / cursor-agent, Codex CLI, Grok CLI). Tokens stay on the machine except those HTTPS calls.

## Caches

Two layers hold a last good reading: the fetcher writes `~/.cache/quota-widget/<provider>.json` and reads it back only through `_read_provider_cache`, and the plasmoid keeps its own copy in `mergeProv` for the same window.

- Age a value by `fetched_ms`, the instant the reading was taken, never by when the payload arrived. Replaying a cached payload must not buy a second window.
- Both layers expire at 24 h. `DEFAULT_CACHE_MAX_AGE_S` in `fetch_quota.py` (overridable through `QUOTA_WIDGET_CACHE_MAX_AGE_S`) is the one to change: the fetcher emits the effective window as `cache_max_age_s` and `main.qml` takes `staleKeepMs` from it, so an override reaches the panel. `defaultStaleKeepMs` in `main.qml` is the fallback for a payload that carries no value.
- An expired fetcher entry is deleted when it is read.
- Entries are scoped to one account id (`_account_id`, hashed), so a second account signing in on the same machine never reads the first one's numbers. That id is text off the wire, so `_digest` normalizes it to `NORMALIZATION_FORM` (NFC) before hashing: an NFD spelling of the same account and its NFC twin are one scope, not two.

Nothing personal reaches a log, a cache, or the emitted JSON: no email or session token leaves the function that reads it, and a failed HTTP body is discarded rather than kept. The provider cache holds only what the UI renders, plus the hashed account id used to scope it. README's "Data and privacy" section is the user-facing statement of this; change it with the code.

## Gate

```bash
uv sync --extra dev --frozen
uv run black --check .
uv run ruff check .
uv run mypy
uv run pytest
shellcheck install.sh
```

`ruff` selects its groups in `[tool.ruff.lint]`, defect groups (bugbear,
bandit, comprehensions, datetime, return statements, pathlib) alongside the
style ones, and every gate step is blocking in CI. `mypy` is strict over the
fetcher, `tests/`, and `scripts/`. A
`noqa` carries its rule and a reason; the per-file ignores in `pyproject.toml`
are scoped to `tests/` and say why.

`.github/workflows/test.yml` pins each third-party action to the commit behind
its version tag; `.github/dependabot.yml` opens the bump. Do not repin one to a
floating tag. The `uv` version there is pinned, so raise it there and nowhere
else.

