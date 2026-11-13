"""A full rebuild of the vectors on another machine (#305).

Embedding every unit takes about 75 minutes on pop-mini and 25 on ``computer``. As
with transcripts (``extract._Transcriber._run_on``), the units go to the host named by
``[embeddings] host`` over ssh in batches, ``embed_worker`` embeds them there at low
priority, and the vectors come back. The host keeps the worker, its environment and a
copy of the model, and nothing of the items once a batch is back.

Only text is sent: each unit's recipe text, as embedded here.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shlex
import sqlite3
import subprocess
import tempfile
import time
import typing
import uuid
from pathlib import Path

from lestash.core import embed_worker as _worker
from lestash.core import embeddings as emb

logger = logging.getLogger(__name__)

REMOTE_ROOT = "lestash-embedder"  # the worker's folder in the host's home
CHUNK = 4000  # units per batch: a few minutes on computer, and kept once back
SSH_OPTIONS = (
    "-o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=4"
)


class HostUnavailable(RuntimeError):
    """The host can't be reached or set up: rebuild here instead."""


def model_folder() -> Path:
    """This machine's copy of the model, fetched once with its Hugging Face token."""
    from huggingface_hub import snapshot_download

    try:
        return Path(snapshot_download(emb.MODEL_NAME, local_files_only=True))
    except Exception:
        return Path(snapshot_download(emb.MODEL_NAME))


class _Host:
    def __init__(self, host: str) -> None:
        if not host or host.startswith("-"):  # ssh would read it as an option
            raise HostUnavailable(f"not a host name: {host!r}")
        self.host = host
        self.rsync = ["rsync", "-a", "-e", f"ssh {SSH_OPTIONS}"]

    def ssh(self, command: str, timeout: float = 120) -> str:
        return subprocess.run(
            ["ssh", *SSH_OPTIONS.split(), "--", self.host, command],
            check=True, capture_output=True, text=True, timeout=timeout,
        ).stdout  # fmt: skip

    def copy(self, *args: str, timeout: float = 600) -> None:
        subprocess.run(
            [*self.rsync, *args], check=True, capture_output=True, text=True, timeout=timeout
        )


def _tail(error: BaseException) -> str:
    detail = getattr(error, "stderr", None) or str(error)
    return str(detail).strip()[-300:]


def rebuild_on(
    conn: sqlite3.Connection,
    host: str,
    *,
    threads: int = 16,
    budget_s: float | None = None,
    progress_callback: typing.Callable[[int, int], None] | None = None,
) -> emb.RefreshCounts:
    """A whole refresh, with the embedding done on ``host``. Raises HostUnavailable
    before anything is embedded if the host can't be reached or set up; a batch that
    fails later stops the run, keeping the batches already back."""
    started = time.monotonic()
    remote = _Host(host)
    q = shlex.quote
    model = model_folder()
    try:
        home = remote.ssh('printf %s "$HOME"').strip()
        root = f"{home}/{REMOTE_ROOT}"
        remote.ssh(f"mkdir -p {q(root)}/jobs {q(root)}/model")
        lock = f"flock {q(root)}/.lock"  # one batch at a time on the host
        remote.copy(_worker.__file__, f"{host}:{root}/embed_worker.py", timeout=120)
        remote.ssh(f"{lock} python3 -I {q(root)}/embed_worker.py setup {q(root)}", timeout=1800)
        # -L: the cache's files are links into its blob store
        remote.copy("-L", "--delete", f"{model}/", f"{host}:{root}/model/", timeout=3600)
    except (OSError, subprocess.SubprocessError) as e:
        raise HostUnavailable(f"{host}: {_tail(e)}") from e

    todo, removed = emb.plan_refresh(conn)
    counts = emb.RefreshCounts(removed=removed)
    done = 0
    for start in range(0, len(todo), CHUNK):
        if budget_s is not None and time.monotonic() - started > budget_s:
            counts.stopped = True
            break
        batch = todo[start : start + CHUNK]
        vectors = _run_batch(remote, root, [(i, text) for i, _, text in batch], threads)
        counts.embedded += emb.store_vectors(conn, batch, vectors)
        done += len(batch)
        if progress_callback:
            progress_callback(done, len(todo))

    counts.pending = len(todo) - done
    if not counts.pending:
        counts.swapped = emb.swap_in_next(conn)
    counts.seconds = time.monotonic() - started
    return counts


def _run_batch(
    remote: _Host, root: str, units: list[tuple[int, str]], threads: int
) -> list[list[float]]:
    q = shlex.quote
    job = f"{root}/jobs/{uuid.uuid4().hex[:12]}"
    unit = f"lestash-embed-{job.rsplit('/', 1)[1]}"
    with tempfile.TemporaryDirectory(prefix="lestash-embed-") as tmp:
        folder = Path(tmp)
        _worker.write_units(folder, units)
        (folder / "job.json").write_text(
            json.dumps(
                {
                    "model": f"{root}/model",
                    "max_seq_length": emb.MAX_SEQ_LENGTH,
                    "batch": emb.ENCODE_BATCH,
                }
            )  # fmt: skip
        )
        try:
            remote.copy(f"{folder}/", f"{remote.host}:{job}/")
            # Low priority and capped: CI and the agents on the host come first
            remote.ssh(
                f"flock {q(root)}/.lock systemd-run --user --wait --pipe --collect --quiet "
                f"--unit={unit} --nice=10 -p RuntimeMaxSec=1h -p CPUQuota={threads * 100}% "
                f"-p MemoryMax=8G {q(root)}/.venv/bin/python -I {q(root)}/embed_worker.py "
                f"run {q(job)} --threads {threads}",
                timeout=2 * 3600,
            )
            remote.copy(f"{remote.host}:{job}/out/", f"{folder}/out/")
        except (OSError, subprocess.SubprocessError) as e:
            raise RuntimeError(f"embedding on {remote.host} stopped: {_tail(e)}") from e
        finally:
            # A run cut short (a timeout, Ctrl-C) leaves no unit burning the host
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                remote.ssh(f"systemctl --user stop {unit} 2>/dev/null; rm -rf {q(job)}")
        return _worker.read_vectors(folder, len(units))
