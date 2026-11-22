"""Pytest plugin: every LeStash test runs in the test environment.

Registered as a ``pytest11`` entry point of the ``lestash`` package, so it loads for the
tests of every package in the workspace without a conftest.

A session fixture migrates one template database, which each test's new database
copies (#405).

The test environment sets HOME to an empty temp folder for each test, and clears the
``XDG_*`` folder variables and the ``LESTASH_*`` setting variables, so every default
place in ``lestash.core.paths`` is under that folder. Code that loads the default config
(``Config.load()``) instead of the test's own then finds no config, not the real
database. A test that opens the default database fails, so the code path that ignored
the test's config is found and fixed.

It only redirects HOME: a test can still reach any file by its full path. Model
caches (HuggingFace, which Docling also uses) stay in the real home folder, so a test
that loads a model doesn't download it again. The check covers only the default
database, not tokens, logs or caches written under the temp HOME.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from lestash.core.config import Config


_XDG_FOLDERS = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME")


@pytest.fixture(autouse=True, scope="session")
def _lestash_template_database(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Migrate one database per session; each test's new database copies it (#405).

    Running every migration commits dozens of times, about 10 s of fsyncs on disk per
    test database. A database file that already exists still migrates as before.
    """
    from lestash.core import database
    from lestash.core.config import Config, GeneralConfig

    path = tmp_path_factory.mktemp("template") / "lestash.db"
    database.init_database(Config(general=GeneralConfig(database_path=str(path))))
    database.template_database = path
    yield
    database.template_database = None


@pytest.fixture(autouse=True)
def _lestash_test_environment(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    home = tmp_path_factory.mktemp("home")
    # Read before HOME moves: the real model cache, where huggingface_hub would look
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    monkeypatch.setenv("HF_HOME", os.environ.get("HF_HOME") or str(Path(cache) / "huggingface"))
    monkeypatch.setenv("HOME", str(home))
    from lestash.core import paths
    from lestash.core.config import ENV_OVERRIDES

    for variable in (*_XDG_FOLDERS, *ENV_OVERRIDES):
        monkeypatch.delenv(variable, raising=False)
    defaults = (paths.legacy_dir() / paths.DB_NAME, paths.data_dir() / paths.DB_NAME)
    yield
    if any(db.exists() for db in defaults):
        pytest.fail(
            "The test opened the default database, not its own: some code path loaded "
            "the default config (Config.load()) instead of the one it was given",
            pytrace=False,
        )


def forbid_default_config(
    monkeypatch: pytest.MonkeyPatch, config: Config, allowed: int = 0
) -> None:
    """``Config.load()`` gives ``config`` the first ``allowed`` times, then fails the test.

    ``allowed=1`` lets the CLI load its own config. A sync after that must use the
    config it was handed (``plugin.app_config``): a plugin loading the default would
    read and write the default database, not the caller's (#231).
    """
    from lestash.core.config import Config as _Config

    calls = 0

    def load(cls: type[_Config]) -> _Config:
        nonlocal calls
        calls += 1
        if calls > allowed:
            raise AssertionError("Config.load() in a sync: it ignored the caller's config")
        return config

    monkeypatch.setattr(_Config, "load", classmethod(load))
