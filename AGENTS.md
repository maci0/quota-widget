# Agent Rules

Inherits the universal rules in `~/.agents/AGENTS.md`. Local notes below; they do not weaken parent rules.

## What this is

KDE Plasma 6 plasmoid (`com.maci.quota-widget`). QML UI in `package/contents/ui/main.qml`. Data source in `package/contents/code/fetch_quota.py`, polled by plasmashell's `executable` engine.

## Runtime Python

Plasmashell runs `python3 package/contents/code/fetch_quota.py`. That path is system Python on purpose: the widget has no venv at display time. Do not switch the QML command to `uv run`.

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

## Gate

```bash
uv sync --extra dev --frozen
uv run black --check .
uv run ruff check .
uv run mypy
uv run pytest
```
