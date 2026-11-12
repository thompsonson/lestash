"""#189: credentials and tokens are written readable only by you."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from lestash.core.config import write_secret


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_a_secret_is_written_at_600_in_a_700_folder(tmp_path: Path) -> None:
    path = tmp_path / "lestash" / "token.json"
    write_secret(path, '{"token": "t"}')
    assert path.read_text() == '{"token": "t"}'
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700


def test_a_readable_file_is_replaced_by_a_600_one(tmp_path: Path) -> None:
    path = tmp_path / "token.json"
    path.write_text("old")
    path.chmod(0o644)
    write_secret(path, "new")
    assert (path.read_text(), _mode(path)) == ("new", 0o600)
    assert [p.name for p in tmp_path.iterdir()] == ["token.json"]  # no temp file left


def test_a_failed_write_leaves_the_old_file(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "token.json"
    write_secret(path, "old")

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", broken)
    with pytest.raises(OSError):
        write_secret(path, "new")
    assert path.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["token.json"]


def test_linkedin_saves_its_token_at_600(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    from lestash_linkedin import api

    api._save_json(api.get_token_path(), {"access_token": "t"})
    assert _mode(api.get_token_path()) == 0o600
