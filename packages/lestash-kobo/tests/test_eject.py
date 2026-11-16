"""Ejecting the Kobo through udisks, so it leaves USB mode."""

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from lestash_kobo import KoboSource
from lestash_kobo import eject as kobo_eject
from lestash_kobo import shelves as kobo_shelf
from lestash_kobo.source import _store
from typer.testing import CliRunner

DRIVE = "/org/freedesktop/UDisks2/drives/Linux_File_Stor_Gadget_N6053C0033803"
INFO = (
    "/org/freedesktop/UDisks2/block_devices/sdb:\n"
    f"  org.freedesktop.UDisks2.Block:\n    Drive:  '{DRIVE}'\n"
)


@pytest.fixture
def disk(tmp_path: Path) -> Path:
    d = tmp_path / "KOBOeReader"
    d.write_text("")
    return d


def _fake(monkeypatch, *, mounted=(), eject_rc=0, eject_err=""):
    calls: list[list[str]] = []

    def run(cmd):
        calls.append(cmd)
        if cmd[0] == kobo_eject.MOUNTPOINT:
            return subprocess.CompletedProcess(cmd, 0 if cmd[-1] in mounted else 1, "", "")
        if cmd[0] == kobo_eject.UDISKSCTL:
            return subprocess.CompletedProcess(cmd, 0, INFO, "")
        return subprocess.CompletedProcess(cmd, eject_rc, "", eject_err)

    monkeypatch.setattr(kobo_eject, "_run", run)
    return calls


def _eject(disk: Path):
    return CliRunner().invoke(KoboSource().get_commands(), ["eject", "--disk", str(disk)])


def test_eject_asks_udisks_to_eject_the_kobo_drive(disk, monkeypatch, test_db) -> None:
    calls = _fake(monkeypatch)
    result = _eject(disk)
    assert result.exit_code == 0, result.output
    assert kobo_shelf.last_eject(_store({}, test_db)) is not None
    assert calls[-1] == [
        kobo_eject.BUSCTL,
        "call",
        "--system",
        "org.freedesktop.UDisks2",
        DRIVE,
        "org.freedesktop.UDisks2.Drive",
        "Eject",
        "a{sv}",
        "0",
    ]


def test_a_mounted_kobo_is_not_ejected(disk, monkeypatch) -> None:
    calls = _fake(monkeypatch, mounted=("/media/kobo-rw",))
    result = _eject(disk)
    assert result.exit_code == 1
    assert "/media/kobo-rw is still mounted" in result.output
    assert not any(c[0] == kobo_eject.BUSCTL for c in calls)


def test_no_kobo_plugged_in(tmp_path, monkeypatch) -> None:
    _fake(monkeypatch)
    result = _eject(tmp_path / "missing")
    assert result.exit_code == 1
    assert "no Kobo disk" in result.output


def test_a_refused_eject_says_how_to_allow_it(disk, monkeypatch) -> None:
    _fake(
        monkeypatch,
        eject_rc=1,
        eject_err="Call failed: Not authorized to perform operation",
    )
    with pytest.raises(kobo_eject.EjectFailed, match="50-lestash-kobo-eject.pkla"):
        kobo_eject.eject(disk)


def test_a_failed_eject_keeps_the_previous_eject_time(disk, monkeypatch, test_db) -> None:
    store = _store({}, test_db)
    kobo_shelf.record_eject(store, datetime(2026, 11, 1, tzinfo=UTC))
    _fake(monkeypatch, eject_rc=1, eject_err="Call failed: busy")
    result = _eject(disk)
    assert result.exit_code == 1
    assert kobo_shelf.last_eject(store) == datetime(2026, 11, 1, tzinfo=UTC)


def test_a_hung_eject_keeps_the_previous_eject_time(disk, monkeypatch, test_db) -> None:
    store = _store({}, test_db)
    kobo_shelf.record_eject(store, datetime(2026, 11, 1, tzinfo=UTC))

    def hang(cmd, **kw):
        if cmd[0] == kobo_eject.BUSCTL:
            raise subprocess.TimeoutExpired(cmd, 30)
        return subprocess.CompletedProcess(
            cmd, 1 if cmd[0] == kobo_eject.MOUNTPOINT else 0, INFO, ""
        )

    monkeypatch.setattr(kobo_eject.subprocess, "run", hang)
    result = _eject(disk)
    assert result.exit_code == 1
    assert "timed out" in result.output
    assert kobo_shelf.last_eject(store) == datetime(2026, 11, 1, tzinfo=UTC)
