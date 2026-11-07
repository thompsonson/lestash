"""Versioned store of every file the Kobo has held.

The Kobo keeps no history, and its Drive sync can replace an annotated PDF with the
clean original. So each plug-in copies the device into a store that never forgets:

    <root>/blobs/ab/abcdef…            file contents, stored once by SHA-256
    <root>/snapshots/<time>/manifest.json   device path -> sha256, size, mtime

A snapshot is written to a temporary folder and renamed when complete, so an unplug
mid-copy never leaves a half-written snapshot. Temp files a killed copy leaves behind
are removed by the next one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_ROOT = Path("~/.local/share/lestash/kobo").expanduser()
DB_PATH = ".kobo/KoboReader.sqlite"

# Written by macOS and Windows when the Kobo was mounted there; not Kobo data
SKIP_DIRS = frozenset({".Spotlight-V100", ".fseventsd", ".Trashes", "System Volume Information"})
CHUNK = 1 << 20


@dataclass(frozen=True)
class FileEntry:
    sha256: str
    size: int
    mtime: str


@dataclass
class Snapshot:
    name: str
    taken_at: datetime
    files: dict[str, FileEntry]
    path: Path

    def blob(self, store: KoboStore, device_path: str) -> Path | None:
        entry = self.files.get(device_path)
        return store.blob_path(entry.sha256) if entry else None


@dataclass
class SnapshotResult:
    snapshot: Snapshot
    files: int = 0
    new_blobs: int = 0
    new_bytes: int = 0
    changed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    # PDFs whose contents went back to the first version seen: annotations wiped
    reverted: list[str] = field(default_factory=list)


def device_name(raw: str) -> str:
    """Repair a name read through a Latin-1 mount (``iocharset=iso8859-1``).

    Such a mount turns ``·`` into the single byte 0xB7, which Python reads as a lone
    surrogate. Those bytes are Latin-1, so decoding them that way restores the name.
    """
    data = os.fsencode(raw)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


class KoboStore:
    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root).expanduser() if root else DEFAULT_ROOT
        self.blobs = self.root / "blobs"
        self.snapshots_dir = self.root / "snapshots"

    def blob_path(self, sha256: str) -> Path:
        return self.blobs / sha256[:2] / sha256

    def snapshots(self) -> list[Snapshot]:
        """All complete snapshots, oldest first."""
        if not self.snapshots_dir.is_dir():
            return []
        found = []
        for d in self.snapshots_dir.iterdir():
            manifest = d / "manifest.json"
            if d.name.startswith(".") or not manifest.is_file():
                continue
            try:
                found.append(self._load(d, json.loads(manifest.read_text())))
            except (OSError, ValueError, KeyError, TypeError) as e:
                # One damaged snapshot must not block the others, or new ones
                logger.warning("Skipping unreadable Kobo snapshot %s: %s", d.name, e)
        return sorted(found, key=lambda s: (s.taken_at, s.name))

    def latest(self) -> Snapshot | None:
        snaps = self.snapshots()
        return snaps[-1] if snaps else None

    def take(
        self, source: Path | str, taken_at: datetime | None = None, verify: bool = False
    ) -> SnapshotResult:
        """Copy every file under ``source`` (the Kobo's root) into a new snapshot.

        A file whose size and mtime match the previous snapshot is not read again, so a
        plug-in only reads what changed. ``verify`` re-reads and re-hashes everything.
        """
        source = Path(source)
        if not (source / DB_PATH).is_file():
            raise FileNotFoundError(f"{source} has no {DB_PATH}: is the Kobo mounted?")
        taken_at = (taken_at or datetime.now(UTC)).astimezone(UTC)
        name = taken_at.strftime("%Y%m%dT%H%M%SZ")
        snaps = self.snapshots()
        if any(s.name == name for s in snaps):
            raise FileExistsError(f"snapshot {name} already exists")
        # Compare with what came before this copy, also when importing an old copy late
        history = [s for s in snaps if s.taken_at < taken_at]
        previous = history[-1].files if history and not verify else {}

        self._sweep_temp()
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=self.snapshots_dir))
        try:
            files: dict[str, FileEntry] = {}
            new_blobs = new_bytes = 0
            for path in self._walk(source):
                rel = device_name(str(path.relative_to(source)))
                stat = path.stat()
                mtime = _iso(stat.st_mtime)
                known = previous.get(rel)
                if (
                    known
                    and known.size == stat.st_size
                    and known.mtime == mtime
                    and self.blob_path(known.sha256).is_file()
                ):
                    files[rel] = known
                    continue
                sha, is_new = self._store_blob(path)
                if is_new:
                    new_blobs += 1
                    new_bytes += stat.st_size
                files[rel] = FileEntry(sha, stat.st_size, mtime)
            manifest = {
                "taken_at": taken_at.isoformat(timespec="seconds"),
                "source": str(source),
                "files": {k: vars(v) for k, v in sorted(files.items())},
            }
            (tmp / "manifest.json").write_text(json.dumps(manifest, indent=1))
            final = self.snapshots_dir / name
            tmp.rename(final)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

        snap = Snapshot(name, taken_at, files, final)
        result = SnapshotResult(snap, len(files), new_blobs, new_bytes)
        self._compare(result, history)
        return result

    def _sweep_temp(self) -> None:
        """Remove temp files left by a copy that was killed (e.g. the Kobo unplugged).

        Snapshots of one store don't run at the same time: the plug-in unit is a oneshot.
        """
        for parent in (self.snapshots_dir, self.blobs):
            if not parent.is_dir():
                continue
            for leftover in parent.glob(".tmp-*"):
                if leftover.is_dir():
                    shutil.rmtree(leftover, ignore_errors=True)
                else:
                    leftover.unlink(missing_ok=True)

    def _walk(self, source: Path):
        for dirpath, dirnames, filenames in os.walk(source):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for f in sorted(filenames):
                if f.startswith("._"):  # macOS AppleDouble files
                    continue
                path = Path(dirpath) / f
                if path.is_file() and not path.is_symlink():
                    yield path

    def _store_blob(self, path: Path) -> tuple[str, bool]:
        """Copy ``path`` into the blob store. Returns its hash, and whether it was new."""
        digest = hashlib.sha256()
        self.blobs.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=self.blobs)
        tmp = Path(tmp_name)
        try:
            with path.open("rb") as src, os.fdopen(fd, "wb") as dst:
                while chunk := src.read(CHUNK):
                    digest.update(chunk)
                    dst.write(chunk)
            sha = digest.hexdigest()
            dest = self.blob_path(sha)
            if dest.exists():
                tmp.unlink()
                return sha, False
            dest.parent.mkdir(exist_ok=True)
            tmp.chmod(0o400)
            tmp.rename(dest)
            return sha, True
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def _compare(self, result: SnapshotResult, history: list[Snapshot]) -> None:
        files = result.snapshot.files
        if history:
            prev = history[-1].files
            result.added = sorted(set(files) - set(prev))
            result.removed = sorted(set(prev) - set(files))
            result.changed = sorted(
                p for p in files if p in prev and prev[p].sha256 != files[p].sha256
            )
        for path in result.changed:
            if not path.lower().endswith(".pdf"):
                continue
            versions = [s.files[path].sha256 for s in history if path in s.files]
            if versions and files[path].sha256 == versions[0] and len(set(versions)) > 1:
                result.reverted.append(path)

    @staticmethod
    def _load(path: Path, data: dict[str, Any]) -> Snapshot:
        files = {k: FileEntry(**v) for k, v in data["files"].items()}
        return Snapshot(path.name, datetime.fromisoformat(data["taken_at"]), files, path)
