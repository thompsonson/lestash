"""The folder layout (#304): XDG folders, the legacy folder until the move, the rule for
derived places, settings from the environment, and saving only what you set."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from lestash.core import paths
from lestash.core.blobs import store_root
from lestash.core.config import Config, GeneralConfig, custom_db_dir, get_config_path
from lestash.core.database import get_cache_dir, get_connection, get_media_dir
from lestash.core.db_admin import create_backup
from lestash.core.sync import _take_lock


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A second empty HOME, so a test may make the default database on purpose: the
    test environment checks only the HOME it made."""
    folder = tmp_path / "home"
    folder.mkdir()
    monkeypatch.setenv("HOME", str(folder))
    return folder


def test_xdg_defaults_are_under_home(home: Path) -> None:
    assert paths.config_dir() == paths.legacy_dir() == home / ".config" / paths.APP
    assert paths.data_dir() == home / ".local" / "share" / "lestash"
    assert paths.cache_dir() == home / ".cache" / "lestash"
    assert paths.state_dir() == home / ".local" / "state" / "lestash"
    assert paths.backups_dir() == paths.data_dir() / "backups"
    assert paths.downloads_dir() == paths.cache_dir() / "downloads"  # Takeout zips: cache
    assert paths.database_path() == paths.data_dir() / "lestash.db"
    assert paths.logs_dir() == paths.state_dir() / "logs"
    assert paths.locks_dir() == paths.state_dir() / "locks"


def test_xdg_variables_are_honoured_and_relative_ones_ignored(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        monkeypatch.setenv(variable, str(tmp_path / variable))
    assert paths.config_dir() == tmp_path / "XDG_CONFIG_HOME" / "lestash"
    assert paths.data_dir() == tmp_path / "XDG_DATA_HOME" / "lestash"
    assert paths.cache_dir() == tmp_path / "XDG_CACHE_HOME" / "lestash"
    assert paths.state_dir() == tmp_path / "XDG_STATE_HOME" / "lestash"
    monkeypatch.setenv("XDG_DATA_HOME", "relative/share")
    assert paths.data_dir() == home / ".local" / "share" / "lestash"


def test_the_legacy_database_wins_over_an_empty_file_in_the_new_place(home: Path) -> None:
    """pop-mini's case: the real database in the legacy folder, and a 0-byte
    lestash.db in ~/.local/share/lestash. The empty one must never be opened."""
    legacy = paths.legacy_dir() / "lestash.db"
    with get_connection(Config(general=GeneralConfig(database_path=str(legacy)))) as conn:
        conn.execute("CREATE TABLE real_data (x)")
        conn.commit()
    empty = paths.data_dir() / "lestash.db"
    empty.parent.mkdir(parents=True)
    empty.touch()

    assert paths.database_path() == legacy
    assert Path(Config.load().general.database_path) == legacy
    with get_connection() as conn:  # the default config's database
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "real_data" in tables
    assert empty.stat().st_size == 0  # never opened, never written


def test_after_the_move_the_new_places_are_used(home: Path) -> None:
    for name in ("lestash.db", "logs", "media", "bluesky_session.json"):
        target = paths.legacy_dir() / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
    assert paths.database_path().parent == paths.legacy_dir()
    assert paths.logs_dir().parent == paths.legacy_dir()
    for name in ("lestash.db", "logs", "media", "bluesky_session.json"):
        (paths.legacy_dir() / name).unlink()
    assert paths.database_path() == paths.data_dir() / "lestash.db"
    assert paths.logs_dir() == paths.state_dir() / "logs"
    assert paths.media_dir() == paths.data_dir() / "media"
    assert paths.bluesky_session_path() == paths.state_dir() / "bluesky_session.json"


def test_the_default_database_takes_its_places_from_the_paths_module(home: Path) -> None:
    config = Config()
    assert custom_db_dir(config) is None
    assert store_root(config) == paths.store_dir()
    assert get_media_dir(config) == paths.media_dir()
    assert get_cache_dir(config) == paths.posts_cache_dir()

    with get_connection(config) as conn:
        lock = _take_lock(conn, "probe", None)
    assert lock is not None
    assert Path(lock.name) == paths.locks_dir() / "sync-probe.lock"
    lock.close()
    assert create_backup(paths.database_path()).parent == paths.backups_dir()


def test_a_database_set_elsewhere_keeps_its_places_beside_it(tmp_path: Path) -> None:
    db = tmp_path / "other" / "stash.db"
    config = Config(general=GeneralConfig(database_path=str(db)))
    assert custom_db_dir(config) == db.parent
    assert store_root(config) == db.parent / "store"
    assert get_media_dir(config) == db.parent / "media"
    assert get_cache_dir(config) == db.parent / "cache" / "posts"
    with get_connection(config) as conn:
        lock = _take_lock(conn, "probe", None)
    assert lock is not None
    assert Path(lock.name) == Path(f"{db}.sync-probe.lock")
    lock.close()
    assert create_backup(db).parent == db.parent


def _saved() -> dict:
    with get_config_path().open("rb") as f:
        return tomllib.load(f)


def test_save_writes_only_what_was_set(home: Path) -> None:
    config = Config.load()
    config.logging.level = "DEBUG"
    config.__pydantic_extra__["kobo"] = {"collections": True}  # type: ignore[index]
    config.save()
    saved = _saved()
    assert saved == {"logging": {"level": "DEBUG"}, "kobo": {"collections": True}}
    # The defaults that name places are never pinned
    assert "general" not in saved
    assert Config.load().general.database_path == str(paths.database_path())


def test_a_set_database_path_is_saved(home: Path, tmp_path: Path) -> None:
    config = Config(general=GeneralConfig(database_path=str(tmp_path / "x.db")))
    config.save()
    assert _saved() == {"general": {"database_path": str(tmp_path / "x.db")}}


def test_the_environment_wins_and_is_never_saved(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    in_file = str(tmp_path / "file.db")
    Config(general=GeneralConfig(database_path=in_file)).save()
    assert Config.load().general.database_path == in_file  # config.toml over the default

    from_env = str(tmp_path / "env.db")
    monkeypatch.setenv("LESTASH_DATABASE_PATH", from_env)
    monkeypatch.setenv("LESTASH_LOG_FILE", str(tmp_path / "env.log"))
    config = Config.load()
    assert config.general.database_path == from_env  # the environment over config.toml
    assert config.logging.file_path == str(tmp_path / "env.log")

    config.logging.level = "WARNING"
    config.save()
    assert _saved() == {"general": {"database_path": in_file}, "logging": {"level": "WARNING"}}


def test_config_set_does_not_pin_defaults(home: Path) -> None:
    from lestash.cli.main import app
    from typer.testing import CliRunner

    result = CliRunner().invoke(app, ["config", "set", "logging.level", "DEBUG"])
    assert result.exit_code == 0, result.output
    assert _saved() == {"logging": {"level": "DEBUG"}}


# Built in pieces, so this file doesn't match its own check
_OLD = re.compile(r"\.config/" + "lestash" + r"|[\"']\.config[\"']\s*[/,]\s*[\"']" + "lestash")
_SCANNED = ("packages", "scripts", "deploy", "app/src", "app/src-tauri/src", "extensions")
_SUFFIXES = {".py", ".js", ".rs", ".sh", ".service", ".timer", ".rules", ".toml", ".kt"}


def test_only_the_paths_module_names_the_legacy_folder() -> None:
    """Every package, plugin, script and unit gets its folders from lestash.core.paths."""
    root = Path(__file__).resolve().parents[3]
    own = root / "packages/lestash/src/lestash/core/paths.py"
    found = []
    for top in _SCANNED:
        for path in (root / top).rglob("*"):
            if path.suffix not in _SUFFIXES or not path.is_file() or path == own:
                continue
            if any(part in {".venv", "node_modules", "target", "gen"} for part in path.parts):
                continue
            for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                if _OLD.search(line):
                    found.append(f"{path.relative_to(root)}:{n}: {line.strip()}")
    assert not found, "\n".join(found)


def test_an_old_default_pinned_in_config_toml_follows_the_default(home: Path) -> None:
    """A config.toml saved before #304 names the old default places; after the move
    they must not bring the old database back."""
    legacy = paths.legacy_dir()
    legacy.mkdir(parents=True)
    get_config_path().write_text(
        f'[general]\ndatabase_path = "{legacy / "lestash.db"}"\n\n'
        f'[logging]\nfile_path = "{legacy / "logs" / "lestash.log"}"\nlevel = "DEBUG"\n'
    )
    config = Config.load()
    assert config.general.database_path == str(paths.data_dir() / "lestash.db")
    assert config.logging.file_path == str(paths.state_dir() / "logs" / "lestash.log")
    config.save()
    assert _saved() == {"logging": {"level": "DEBUG"}}


def test_backups_made_beside_the_default_database_are_still_found(home: Path) -> None:
    from lestash.core.db_admin import find_backup, list_backups

    legacy = paths.legacy_dir() / "lestash.db"
    with get_connection(Config(general=GeneralConfig(database_path=str(legacy)))):
        pass
    old = legacy.parent / "lestash-backup-20260101-000000.db"
    old.write_bytes(legacy.read_bytes())
    new = create_backup(legacy)
    assert new.parent == paths.backups_dir()
    assert [b["path"] for b in list_backups(legacy)] == [str(new), str(old)]
    assert find_backup(legacy, old.name) == old
    assert find_backup(legacy, new.name) == new


def test_a_stale_check_file_in_the_old_folder_is_removed(home: Path) -> None:
    """Backups beside the database are checked there too, so their stale files go (#414)."""
    import os

    legacy = paths.legacy_dir() / "lestash.db"
    with get_connection(Config(general=GeneralConfig(database_path=str(legacy)))):
        pass
    stale = legacy.parent / "lestash-backup-20200101-000000.db.zst.verify.1-ab.partial"
    stale.write_text("x")
    os.utime(stale, (0, 0))
    create_backup(legacy)
    assert not stale.exists()
