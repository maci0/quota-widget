# Contributing

Unofficial KDE Plasma 6 plasmoid. Two pieces make up a change: QML under
`package/contents/ui/`, and the Python data source at
`package/contents/code/fetch_quota.py`. `scripts/` holds helper scripts.

## Branch

Cut `feat/`, `fix/`, `refactor/`, `docs/`, `test/`, or `chore/` prefixed branches.
Do not commit to `main`.

## Setup and gate

```bash
uv sync --extra dev --frozen
uv run black --check . && uv run ruff check . && uv run mypy && uv run pytest
```

That is the whole gate, in CI order. Both must be green before a push.

Adding a dependency: `uv add <pkg>` (or `uv add --optional dev <pkg>`), which
rewrites `pyproject.toml` and `uv.lock`. Hand-editing `uv.lock` is not a change a
review can follow. Never hand-edit it.

## Adding a test

One module, `tests/test_fetch_quota.py`, holds every test, grouped in
`unittest.TestCase` classes named after the function or provider they cover
(`CodexWindowTest`, `ProviderCacheTest`). Copy the setup of the closest class:
tests point `QUOTA_WIDGET_CACHE` at a `tempfile.TemporaryDirectory()` and patch
`fetch_quota.fetch_json` instead of touching the network. A test that reaches the
network or the real home directory does not belong here.

## QML changes

`package/contents/ui/main.qml` is plain QtQuick. After editing, clear the QML
cache and restart plasmashell so the change shows up:

```bash
./install.sh
rm -rf ~/.cache/plasmashell/qmlcache
systemctl --user restart plasma-plasmashell.service
```

`./install.sh` symlinks `package/` into `~/.local/share/plasma/plasmoids/`, so
there is nothing to reinstall while iterating. It also runs the fetcher once and
prints the result, which is the quickest check that provider parsing still works.
