# Agent Rules

Inherits the universal rules in `~/.agents/AGENTS.md`. Local notes below; they do not weaken parent rules.

## What this is

KDE Plasma 6 plasmoid (`com.maci.quota-widget`). QML UI in `package/contents/ui/main.qml`. Data source in `package/contents/code/fetch_quota.py`, polled by plasmashell's `executable` engine.

## Runtime Python

Plasmashell runs `python3 package/contents/code/fetch_quota.py`. That path is system Python on purpose: the widget has no venv at display time. Do not switch the QML command to `uv run`.

Every wall-clock read goes through `now_ms()` / `now_utc()`; `QUOTA_WIDGET_NOW_MS` pins the clock so a poll replays byte-for-byte. Call `time.time()` or `datetime.now()` directly anywhere else in the fetcher and the replay guarantee is gone.

Every credential, cache, and state file is UTF-8 JSON, read through `fetch_quota._read_text()` and written by `_atomic_write_json()`. Never `Path.read_text()` bare: plasmashell can start under a C locale where `open()` defaults to ASCII, and the merge-write fallback would then rewrite a shared store without the fields it could not decode.

Dev and CI use `uv` (`uv run pytest`, `uv run black`, `uv run ruff`, `uv run mypy`).

## Polling

The QML owns the fetcher process. One run at a time: `exec.poll()` returns early while a source is connected, `onNewData` disconnects it, and a run older than `pollTimeoutMs` (10 min, in the token block with the other timing values) is disconnected so a stalled fetch cannot wedge polling. Keep the release on every path that starts a run.

## Layout

- `package/`: plasmoid (metadata, QML, fetcher, `contents/icons/com.maci.quota-widget.svg`)
- `docs/THREAT_MODEL.md`: entry points, trust boundaries, assets, and the threats per boundary
- `tests/`: pytest
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

The applet icon is `package/contents/icons/com.maci.quota-widget.svg`, a 270 degree gauge arc matching the in-app meter. It is a dark rim under a light fill so it reads on both panel themes; keep that pairing if it is redrawn.

## Providers

Fetcher talks to each vendor's own usage endpoint with credentials already on disk (Claude Code, Cursor IDE / cursor-agent, Codex CLI, Grok CLI). Tokens stay on the machine except those HTTPS calls.

## Caches

Two layers hold a last good reading. `~/.cache/quota-widget/<provider>.json` is written by the fetcher and read only by `_read_provider_cache`; the plasmoid keeps its own copy in `mergeProv` for the same window. Both expire at `DEFAULT_CACHE_MAX_AGE_S` (24 h, overridable through `QUOTA_WIDGET_CACHE_MAX_AGE_S`), an expired fetcher entry is deleted when it is read, and the entries are scoped to one account id (`_account_id`, hashed) so a second account signing in on the same machine never reads the first one's numbers. Change the window in one place: `DEFAULT_CACHE_MAX_AGE_S` in `fetch_quota.py` and `staleKeepMs` in `main.qml`.

Nothing personal reaches a log, a cache, or the emitted JSON: no email or session token leaves the function that reads it, and a failed HTTP body is discarded rather than kept. The provider cache holds only what the UI renders, plus the hashed account id used to scope it. README's "Data and privacy" section is the user-facing statement of this; change it with the code.

## Gate

```bash
uv sync --extra dev --frozen
uv run black --check .
uv run ruff check .
uv run mypy
uv run pytest
```
