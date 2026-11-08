"""Pytest plugin: every LeStash test runs in the test environment.

Registered as a ``pytest11`` entry point of the ``lestash`` package, so it loads for the
tests of every package in the workspace without a conftest.

The test environment sets HOME to an empty temp folder for each test. Code that loads
the default config (``Config.load()``) instead of the test's own then finds no config,
not the real ``~/.config/lestash/lestash.db``. A test that opens the default database
there fails, so the code path that ignored the test's config is found and fixed.

It only redirects HOME: a test can still reach any file by its full path.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _lestash_test_environment(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    yield
    default_db = home / ".config" / "lestash" / "lestash.db"
    if default_db.exists():
        pytest.fail(
            "The test opened the default database, not its own: some code path loaded "
            "the default config (Config.load()) instead of the one it was given",
            pytrace=False,
        )
