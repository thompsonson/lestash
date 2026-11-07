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

import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_ROOT = Path("~/.local/share/lestash/kobo").expanduser()
DB_PATH = ".kobo/KoboReader.sqlite"
SNAPSHOT_NAME = re.compile(r"\d{8}T\d{6}Z")

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

    def digest(self) -> str:
        """One hash of every path and its contents: equal for identical device states."""
        h = hashlib.sha256()
        for path, entry in sorted(self.files.items()):
            h.update(f"{path}\0{entry.sha256}\n".encode())
        return h.hexdigest()

    def blob(self, store: KoboStore, device_path: str) -> Path | None:
        entry = self.files.get(device_path)
        return store.blob_path(entry.sha256) if entry else None


@dataclass
class SnapshotResult:
    snapshot: Snapshot
    new_blobs: int = 0
    new_bytes: int = 0
    changed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    # PDFs whose contents went back to the first version seen: annotations wiped
    reverted: list[str] = field(default_factory=list)

    @property
    def files(self) -> int:
        return len(self.snapshot.files)


def device_name(raw: str) -> str:
    """Repair a name read through a Latin-1 mount (``iocharset=iso8859-1``).

    Such a mount turns ``·`` into the single byte 0xB7, which Python reads as a lone
    surrogate. Those bytes are Latin-1, so decoding them that way restores the name.
    """
    return "/".join(_decode_part(os.fsencode(part)) for part in raw.split("/"))


def _decode_part(data: bytes) -> str:
    # Per path part, so one Latin-1 folder name doesn't garble a UTF-8 file name in it
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _raise(error: OSError) -> None:
    raise error


def _write_durably(path: Path, data: bytes) -> None:
    with path.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _iso(ns: int) -> str:
    # Full precision: a change within the same second must still look like a change
    seconds, rest = divmod(ns, 1_000_000_000)
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S") + f".{rest:09d}Z"


class KoboStore:
    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root).expanduser() if root else DEFAULT_ROOT
        self.blobs = self.root / "blobs"
        self.snapshots_dir = self.root / "snapshots"

    def blob_path(self, sha256: str) -> Path:
        return self.blobs / sha256[:2] / sha256

    def _manifests(self, newest_first: bool = False):
        """Readable snapshots in name order (names are UTC times, so time order)."""
        if not self.snapshots_dir.is_dir():
            return
        for name in reversed(self._names()) if newest_first else self._names():
            d = self.snapshots_dir / name
            manifest = d / "manifest.json"
            try:
                yield self._load(d, json.loads(manifest.read_text()))
            except (OSError, ValueError, KeyError, TypeError) as e:
                # One damaged snapshot must not block the others, or new ones
                logger.warning("Skipping unreadable Kobo snapshot %s: %s", d.name, e)

    def snapshots(self) -> list[Snapshot]:
        """All complete snapshots, oldest first."""
        return sorted(self._manifests(), key=lambda s: (s.taken_at, s.name))

    def latest(self) -> Snapshot | None:
        """The newest readable snapshot, reading only its manifest."""
        return next(self._manifests(newest_first=True), None)

    def _names(self) -> list[str]:
        """Snapshot folder names, readable or not, oldest first.

        Only names this store writes (UTC times): a copied folder such as
        ``backup-old`` would otherwise sort as the newest.
        """
        if not self.snapshots_dir.is_dir():
            return []
        return sorted(
            d.name
            for d in self.snapshots_dir.iterdir()
            if SNAPSHOT_NAME.fullmatch(d.name) and (d / "manifest.json").is_file()
        )

    def newest(self) -> tuple[Snapshot | None, str | None]:
        """The newest snapshot folder's name, and its snapshot if it can be read.

        One scan, so a snapshot that lands meanwhile can't make the newest look damaged.
        """
        names = self._names()
        if not names:
            return None, None
        d = self.snapshots_dir / names[-1]
        try:
            return self._load(d, json.loads((d / "manifest.json").read_text())), d.name
        except (OSError, ValueError, KeyError, TypeError) as e:
            logger.warning("Unreadable Kobo snapshot %s: %s", d.name, e)
            return None, d.name

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
        self.root.mkdir(parents=True, exist_ok=True)
        # One snapshot at a time, also when one is run by hand during a plug-in: the
        # temp sweep below would otherwise delete the other run's files
        with open(self.root / ".snapshot.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._take(source, taken_at, verify)

    def _take(self, source: Path, taken_at: datetime | None, verify: bool) -> SnapshotResult:
        taken_at = (taken_at or datetime.now(UTC)).astimezone(UTC)
        name = taken_at.strftime("%Y%m%dT%H%M%SZ")
        snaps = self.snapshots()
        if name in self._names():  # damaged ones too: the rename would fail at the end
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
                mtime = _iso(stat.st_mtime_ns)
                known = previous.get(rel)
                # The database changes in place, often at the same size, and FAT keeps
                # times to 2 s: always read it again (it is a few MB)
                if (
                    not rel.startswith(DB_PATH)
                    and known
                    and known.size == stat.st_size
                    and known.mtime == mtime
                    and self.blob_path(known.sha256).is_file()
                ):
                    files[rel] = known
                    continue
                sha, is_new = self._store_blob(path, repair=verify)
                if is_new:
                    new_blobs += 1
                    new_bytes += stat.st_size
                files[rel] = FileEntry(sha, stat.st_size, mtime)
            manifest = {
                "taken_at": taken_at.isoformat(timespec="seconds"),
                "source": str(source),
                "files": {k: vars(v) for k, v in sorted(files.items())},
            }
            _write_durably(tmp / "manifest.json", json.dumps(manifest, indent=1).encode())
            _fsync_dir(tmp)
            final = self.snapshots_dir / name
            tmp.rename(final)
            _fsync_dir(self.snapshots_dir)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

        snap = Snapshot(name, taken_at, files, final)
        result = SnapshotResult(snap, new_blobs, new_bytes)
        self._compare(result, history)
        return result

    def _sweep_temp(self) -> None:
        """Remove temp files left by a copy that was killed (e.g. the Kobo unplugged).

        Safe because the caller holds the snapshot lock.
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
        # A folder that can't be listed fails the copy: a snapshot missing it would be
        # recorded as complete, and its files would look removed
        for dirpath, dirnames, filenames in os.walk(source, onerror=_raise):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for f in sorted(filenames):
                if f.startswith("._"):  # macOS AppleDouble files
                    continue
                path = Path(dirpath) / f
                if path.is_file() and not path.is_symlink():
                    yield path

    def _store_blob(self, path: Path, repair: bool = False) -> tuple[str, bool]:
        """Copy ``path`` into the blob store. Returns its hash, and whether it was new.

        With ``repair``, an existing blob whose contents no longer match its name
        (disk damage) is replaced by this good copy.
        """
        digest = hashlib.sha256()
        self.blobs.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=self.blobs)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as dst, path.open("rb") as src:
                while chunk := src.read(CHUNK):
                    digest.update(chunk)
                    dst.write(chunk)
                # On disk before it takes its final name: a crash must not leave a short
                # blob that later snapshots trust
                dst.flush()
                os.fsync(dst.fileno())
            sha = digest.hexdigest()
            dest = self.blob_path(sha)
            if dest.exists() and not (repair and sha_file(dest) != sha):
                tmp.unlink()
                return sha, False
            if not dest.parent.is_dir():
                dest.parent.mkdir()
                _fsync_dir(self.blobs)  # the new shard folder's entry, too
            tmp.chmod(0o400)
            tmp.replace(dest)
            _fsync_dir(dest.parent)
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
        # A PDF that Drive sync deleted and then downloaded again shows as added, not changed
        for path in sorted(set(result.changed) | set(result.added)):
            if not path.lower().endswith(".pdf"):
                continue
            versions = [s.files[path].sha256 for s in history if path in s.files]
            if versions and files[path].sha256 == versions[0] and len(set(versions)) > 1:
                result.reverted.append(path)

    @staticmethod
    def _load(path: Path, data: dict[str, Any]) -> Snapshot:
        files = {k: FileEntry(**v) for k, v in data["files"].items()}
        return Snapshot(path.name, datetime.fromisoformat(data["taken_at"]), files, path)
