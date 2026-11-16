"""``lestash kobo eject``: eject the Kobo, so it leaves USB mode (#336).

An unmount alone leaves the Kobo on its Connected screen. It saves its database,
and imports books copied to it, only once it leaves USB mode: on an eject, or when
the cable is pulled. An eject is the safe one of the two.

The eject goes through udisks, so it needs no root. udisks asks polkit, which lets a
user eject without a password only from a desktop session; the plug-in unit and SSH
have none, so ``deploy/50-lestash-kobo-eject.pkla`` grants it (``install-root.sh``).
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from lestash.core.config import Config
from rich.console import Console

from lestash_kobo.shelves import record_eject
from lestash_kobo.store import KoboStore

console = Console()

KOBO_DISK = Path("/dev/disk/by-label/KOBOeReader")
MOUNTS = (Path("/media/kobo"), Path("/media/kobo-rw"))
UDISKSCTL, BUSCTL, MOUNTPOINT = "/usr/bin/udisksctl", "/usr/bin/busctl", "/usr/bin/mountpoint"
DRIVE = re.compile(r"^\s*Drive:\s*'(/org/freedesktop/UDisks2/drives/[^']+)'", re.MULTILINE)


class EjectFailed(RuntimeError):
    """The Kobo could not be ejected."""


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=30)


def eject(disk: Path = KOBO_DISK) -> None:
    """Eject the Kobo's disk. Raises EjectFailed with the reason."""
    if not disk.exists():
        raise EjectFailed(f"no Kobo disk at {disk}: plugged in, with Connect tapped?")
    for mount in MOUNTS:
        if _run([MOUNTPOINT, "-q", str(mount)]).returncode == 0:
            raise EjectFailed(f"{mount} is still mounted; unmount it first")
    info = _run([UDISKSCTL, "info", "-b", str(disk.resolve())])
    found = DRIVE.search(info.stdout)
    if info.returncode != 0 or not found:
        raise EjectFailed(f"udisks doesn't know {disk}: {info.stderr.strip() or info.stdout}")
    done = _run(
        [
            BUSCTL,
            "call",
            "--system",
            "org.freedesktop.UDisks2",
            found.group(1),
            "org.freedesktop.UDisks2.Drive",
            "Eject",
            "a{sv}",
            "0",
        ]
    )
    if done.returncode != 0:
        reason = done.stderr.strip() or str(done.returncode)
        if "NotAuthorized" in reason or "not authorized" in reason.lower():
            reason += " (install deploy/50-lestash-kobo-eject.pkla: sudo deploy/install-root.sh)"
        raise EjectFailed(f"udisks could not eject the Kobo: {reason}")


def add_eject_command(app: typer.Typer, store_for: Callable[[Config], KoboStore]) -> None:
    @app.command("eject")
    def eject_cmd(
        disk: Annotated[
            Path, typer.Option("--disk", help="The Kobo's disk", readable=False)
        ] = KOBO_DISK,
    ) -> None:
        """Eject the Kobo, so it leaves USB mode, saves, and imports new books."""
        try:
            eject(disk)
        except EjectFailed as e:
            console.print(f"[red]Not ejected: {e}[/red]")
            raise typer.Exit(1) from None
        record_eject(store_for(Config.load()), datetime.now(UTC))
        console.print("Ejected: the Kobo leaves USB mode and imports new books.")
