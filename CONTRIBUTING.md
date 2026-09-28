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
uv run black --check . && uv run ruff check . && uv run mypy && uv run pytest && shellcheck install.sh
```

That is the whole gate, in CI order. Both must be green before a push.

Adding a dependency: `uv add <pkg>` (or `uv add --optional dev <pkg>`), which
rewrites `pyproject.toml` and `uv.lock`. Hand-editing `uv.lock` is not a change a
review can follow. Never hand-edit it.

## Releasing

`package/metadata.json` `KPlugin.Version` is the version Plasma shows and the
single source of truth; `pyproject.toml` `project.version` and the fetcher's
`User-Agent` are derived from it or kept in step by `tests/test_release.py`.

A release is: an entry in `CHANGELOG.md` under the new version, that version in
`package/metadata.json` and `pyproject.toml`, then a `vX.Y.Z` tag on the commit
that does it. The tag message states what changed for a user, not the commit
list.

Bump by what breaks an installed widget, not by how big the diff is. The fetcher
JSON and the `main.xml` keys are the contract: a change to either that an older
plasmoid or an older `main.xml` cannot read is a major, a new provider, gauge, or
config key with a default is a minor, anything else is a patch.

## Adding a test

`tests/test_fetch_quota.py` holds the fetcher tests, grouped in
`unittest.TestCase` classes named after the function or provider they cover
(`CodexWindowTest`, `ProviderCacheTest`). Copy the setup of the closest class:
tests point `QUOTA_WIDGET_CACHE` at a `tempfile.TemporaryDirectory()` and patch
`fetch_quota.fetch_json` instead of touching the network. A test that reaches the
network or the real home directory does not belong here.
`tests/test_release.py` covers the version metadata, which is not fetcher
behavior.

## QML changes

`package/contents/ui/main.qml` is plain QtQuick. Restart plasmashell so the change
shows up:

```bash
./install.sh
systemctl --user restart plasma-plasmashell.service
```

`./install.sh` symlinks `package/` into `~/.local/share/plasma/plasmoids/` and
clears the QML cache, so there is nothing to reinstall while iterating and no
cache to clear by hand. It also runs the fetcher once and prints the result,
which is the quickest check that provider parsing still works.
