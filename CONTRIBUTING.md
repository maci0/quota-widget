# Contributing

Unofficial KDE Plasma 6 plasmoid. Two pieces make up a change: QML under
`package/contents/ui/`, and the Python data source at
`package/contents/code/fetch_quota.py`. `scripts/` holds helper scripts.

## Branch

Cut `feat/`, `fix/`, `refactor/`, `docs/`, `test/`, or `chore/` prefixed branches.
Do not commit to `main`.

## Setup and gate

Two tools come from the system, not from `pyproject.toml`: `uv` 0.12.13 or newer
(CI pins `setup-uv` to that release, and `[tool.uv] required-version` in
`pyproject.toml` makes an older one fail on the first command) and `shellcheck`,
which the last step below needs. Everything else installs into `.venv` with the
first command.

```bash
uv sync --extra dev --locked
uv run black --check . && uv run ruff check . && uv run mypy && uv run pytest && shellcheck install.sh
```

That is the whole gate, in CI order (`.github/workflows/test.yml` runs exactly
these steps, and the workflow grants the token read-only access). Every step
must be green before a push.

Adding a dependency: `uv add <pkg>` (or `uv add --optional dev <pkg>`), which
rewrites `pyproject.toml` and `uv.lock`. Hand-editing `uv.lock` is not a change a
review can follow. Never hand-edit it.

The fetcher plasmashell runs is stdlib only, so a new runtime dependency lands on
a machine with no venv. Do not add one for a single function. Every dev tool
is floored at the release the tree was last verified against and capped below its
next major (`black>=26.5,<27` and its siblings), because `uv add` writes an
uncapped range; restore both ends in the same commit, then `uv lock`.

## Releasing

`package/metadata.json` `KPlugin.Version` is the version Plasma shows and the
single source of truth; `pyproject.toml` `project.version` and the fetcher's
`User-Agent` are derived from it or kept in step by `tests/test_release.py`.

A release is: an entry in `CHANGELOG.md` under the new version, that version in
`package/metadata.json` and `pyproject.toml`, then a `vX.Y.Z` tag on the commit
that does it. The tag message states what changed for a user, not the commit
list.

The manifest version names the last tagged release, so a tree between releases
reports that version to Plasma while carrying the `Unreleased` changes above
it. Do not bump it early: `tests/test_release.py` fails on a version with no
dated section in the changelog, which is what keeps the number a released one.

Bump by what breaks an installed widget, not by how big the diff is. The fetcher
JSON and the `main.xml` keys are the contract: a change to either that an older
plasmoid or an older `main.xml` cannot read is a major, a new provider, gauge, or
config key with a default is a minor, anything else is a patch.

A breaking change goes in the `Unreleased` section under `### Breaking`, states
what it was before and what it is now, and names the next version in a
`Next release: X.Y.Z.` line above it. `tests/test_release.py` fails when a
`Breaking` entry ships without one, or when the version it names is not a major
above the version in `package/metadata.json`.

## Adding a test

`tests/test_fetch_quota.py` holds the fetcher tests, grouped in
`unittest.TestCase` classes named after the function or provider they cover
(`CodexWindowTest`, `ProviderCacheTest`). Copy the setup of the closest class:
point `QUOTA_WIDGET_CACHE` at a `tempfile.TemporaryDirectory()` with
`config_env`, which restores the previous value, and patch
`fetch_quota.fetch_json` instead of touching the network. A test that reaches the
network or the real home directory does not belong here, and the suite's
`tests/conftest.py` fails the one that leaves the fetcher's config pointing at
the real home, so never `os.environ.pop` a sandbox variable: the suite's
`QUOTA_WIDGET_HOME` and `QUOTA_WIDGET_CACHE` are set before any test module is
imported and a test that removed them would leave every later module polling
`~/.cache/quota-widget`.
`tests/test_release.py` covers the version metadata, which is not fetcher
behavior, and `tests/test_print_smoke.py` covers `scripts/print_smoke.py`. One
test file per module under test; a module's tests do not live in another
module's file.

A test that has to read the tree takes its paths from
`tests/project_paths.py`, which finds the root by walking up to
`package/metadata.json`. Do not walk up from `__file__` in a test.

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
