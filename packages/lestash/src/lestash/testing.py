"""Pytest plugin: every LeStash test runs in the test environment.

Registered as a ``pytest11`` entry point of the ``lestash`` package, so it loads for the
tests of every package in the workspace without a conftest.

The test environment sets HOME to an empty temp folder for each test. Code that loads
the default config (``Config.load()``) instead of the test's own then finds no config,
not the real ``~/.config/lestash/lestash.db``. A test that opens the default database
there fails, so the code path that ignored the test's config is found and fixed.

It only redirects HOME: a test can still reach any file by its full path. Model
caches (HuggingFace, which Docling also uses) stay in the real home folder, so a test
that loads a model doesn't download it again. The check covers only the default
database, not tokens, logs or caches written under the temp HOME.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _lestash_test_environment(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    home = tmp_path_factory.mktemp("home")
    # Read before HOME moves: the real model cache, where huggingface_hub would look
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    monkeypatch.setenv("HF_HOME", os.environ.get("HF_HOME") or str(Path(cache) / "huggingface"))
    monkeypatch.setenv("HOME", str(home))
    yield
    default_db = home / ".config" / "lestash" / "lestash.db"
    if default_db.exists():
        pytest.fail(
            "The test opened the default database, not its own: some code path loaded "
            "the default config (Config.load()) instead of the one it was given",
            pytrace=False,
        )
