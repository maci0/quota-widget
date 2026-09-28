# Agent Rules

Inherits the universal rules in `~/.agents/AGENTS.md`. Local notes below; they do not weaken parent rules.

## What this is

KDE Plasma 6 plasmoid (`com.maci.quota-widget`). QML UI in `package/contents/ui/main.qml`. Data source in `package/contents/code/fetch_quota.py`, polled by plasmashell's `executable` engine.

## Runtime Python

Plasmashell runs `python3 package/contents/code/fetch_quota.py`. That path is system Python on purpose: the widget has no venv at display time. Do not switch the QML command to `uv run`.

Every wall-clock read goes through `now_ms()` / `now_utc()`; `QUOTA_WIDGET_NOW_MS` pins the clock so a poll replays byte-for-byte. Call `time.time()` or `datetime.now()` directly anywhere else in the fetcher and the replay guarantee is gone.

Dev and CI use `uv` (`uv run pytest`, `uv run black`, `uv run ruff`, `uv run mypy`).

## Layout

- `package/`: plasmoid (metadata, QML, fetcher)
- `tests/`: pytest
- `scripts/print_smoke.py`: prints a fetched JSON dump (`install.sh` writes `.scratch/smoke.json`)
- `install.sh`: root symlink installer
- `.scratch/`: gitignored local scratch (never `/tmp`)

Project marker: `package/metadata.json`. Scripts walk up to that file.

`install.sh` deletes `~/.local/share/plasma/plasmoids/com.maci.quota-widget` before
symlinking. Run it only when the user asks to install or upgrade, never as a build or test step.

## Providers

Fetcher talks to each vendor's own usage endpoint with credentials already on disk (Claude Code, Cursor IDE / cursor-agent, Codex CLI, Grok CLI). Tokens stay on the machine except those HTTPS calls.

## Caches

Two layers hold a last good reading. `~/.cache/quota-widget/<provider>.json` is written by the fetcher and read only by `_stale_cache`; the plasmoid keeps its own copy in `mergeProv` for the same window. Both expire at `STALE_MAX_AGE_S` (2 h), and the fetcher's entries are scoped to one account id (`_account_id`, hashed) so a second account signing in on the same machine never reads the first one's numbers. Change the window in one place: `STALE_MAX_AGE_S` in `fetch_quota.py` and `staleKeepMs` in `main.qml`.

## Gate

```bash
uv sync --extra dev --frozen
uv run black --check .
uv run ruff check .
uv run mypy
uv run pytest
```
