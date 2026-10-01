"""Point every test at a sandbox home and cache, for the whole session.

The fetcher resolves its config from the environment the first time it is
asked and caches it, so the override has to be in place before any test module
is imported and has to stay in place between modules. pytest imports
`conftest.py` before it collects anything, which is what makes that hold here;
a test module doing it in its own `setUpModule` leaves every module collected
after its `tearDownModule` running against the real `~/.cache/quota-widget`,
where a stray `_account_id` call installs the account salt for good.

`_outside_the_real_home` is the other half: it fails the test that unpins the
override, rather than leaving the write to be found in a contributor's cache
directory months later.
"""

from __future__ import annotations

import dataclasses
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import fetch_quota
from project_paths import project_root

if TYPE_CHECKING:
    from collections.abc import Iterator

SCRATCH = project_root() / ".scratch"
SCRATCH.mkdir(exist_ok=True)
SANDBOX = tempfile.TemporaryDirectory(dir=SCRATCH)
SANDBOX_HOME = SANDBOX.name
SANDBOX_CACHE = str(Path(SANDBOX.name) / "cache")
SANDBOX_ENV = {
    "QUOTA_WIDGET_HOME": SANDBOX_HOME,
    "QUOTA_WIDGET_CACHE": SANDBOX_CACHE,
}

# Taken before the override goes in, so the check below can tell the real
# account's files from the ones this session is allowed to touch.
REAL_HOME = Path.home()
REAL_CACHE = (
    Path(os.environ.get("XDG_CACHE_HOME") or REAL_HOME / ".cache") / "quota-widget"
)


def _config_paths() -> list[tuple[str, Path]]:
    config = fetch_quota.config()
    return [
        (field.name, getattr(config, field.name))
        for field in dataclasses.fields(fetch_quota.Config)
        if isinstance(getattr(config, field.name), Path)
    ]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Drop the sandbox and the config that resolved under it."""
    del session, exitstatus
    for key in SANDBOX_ENV:
        os.environ.pop(key, None)
    fetch_quota.load_config()
    SANDBOX.cleanup()


for _key, _value in SANDBOX_ENV.items():
    os.environ[_key] = _value
fetch_quota.load_config()


@pytest.fixture(autouse=True)
def _outside_the_real_home(
    request: pytest.FixtureRequest,
) -> Iterator[None]:
    """Fail the test that leaves the fetcher configured against the real home."""
    yield
    escaped = [
        f"{name}={path}"
        for name, path in _config_paths()
        if path == REAL_HOME or path.is_relative_to(REAL_CACHE)
    ]
    assert not escaped, (
        f"{request.node.nodeid} left the fetcher reading the real account: "
        f"{', '.join(escaped)}"
    )
