"""Vectors for a batch of units: the worker behind a rebuild on another machine (#305).

Standalone: only the standard library at the top, so it runs as a copied script on
another machine (``computer``) as well as here. It reads a job folder:

    units.jsonl   {"id": ..., "text": ...} per line
    job.json      {"model": <folder of the model>, "max_seq_length": 512, "batch": 32}

and writes ``out/vectors.f32`` (each unit's vector, float32, in the order of
units.jsonl) and ``out/done.json`` (the model, the width and the count). The model is
sent as a folder, so the machine needs no Hugging Face account.

    python -I embed_worker.py setup <root>          make <root>/.venv (computer)
    python -I embed_worker.py run <job> [--threads 16]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

# The worker's own environment; torch's CPU build keeps it small. Bump with the
# versions LeStash itself runs, so both make the same vectors.
PACKAGES = ("torch==2.11.0", "sentence-transformers==5.3.0", "transformers==5.4.0")


def read_units(folder: Path) -> list[str]:
    with (folder / "units.jsonl").open(encoding="utf-8") as f:
        return [json.loads(line)["text"] for line in f if line.strip()]


def write_units(folder: Path, units: list[tuple[int, str]]) -> None:
    with (folder / "units.jsonl").open("w", encoding="utf-8") as f:
        for item_id, text in units:
            f.write(json.dumps({"id": item_id, "text": text}) + "\n")


def read_vectors(folder: Path, count: int) -> list[list[float]]:
    """The worker's vectors, checked against what was sent."""
    from array import array

    done = json.loads((folder / "out" / "done.json").read_text())
    if done["count"] != count:
        raise ValueError(f"{done['count']} vectors for {count} units")
    flat = array("f")
    flat.frombytes((folder / "out" / "vectors.f32").read_bytes())
    if sys.byteorder != "little":
        flat.byteswap()
    dim = int(done["dim"])
    if len(flat) != count * dim:
        raise ValueError(f"{len(flat)} floats for {count} vectors of {dim}")
    return [flat[i * dim : (i + 1) * dim].tolist() for i in range(count)]


def run(folder: Path, threads: int) -> int:
    import numpy as np
    import torch
    from sentence_transformers import SentenceTransformer

    torch.set_num_threads(threads)
    job = json.loads((folder / "job.json").read_text())
    texts = read_units(folder)
    model = SentenceTransformer(job["model"], device="cpu")
    model.max_seq_length = int(job["max_seq_length"])
    started = time.monotonic()
    vectors = np.asarray(
        model.encode_document(
            texts, normalize_embeddings=True, batch_size=int(job["batch"]), convert_to_numpy=True
        )
    )
    out = folder / "out"
    out.mkdir(exist_ok=True)
    vectors.astype("<f4", copy=False).tofile(out / "vectors.f32")
    seconds = time.monotonic() - started
    done = {"count": len(texts), "dim": int(vectors.shape[1]) if len(texts) else 0}
    (out / "done.json").write_text(json.dumps({**done, "seconds": round(seconds, 1)}))
    return 0


def _uv() -> str:
    for candidate in (
        shutil.which("uv"),
        str(Path.home() / ".local" / "bin" / "uv"),
        "/home/linuxbrew/.linuxbrew/bin/uv",
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    raise SystemExit("uv not found")


def setup(root: Path) -> int:
    """The worker's own Python environment under ``root``, with its packages pinned."""
    venv = root / ".venv"
    python = venv / "bin" / "python"
    uv = _uv()
    if not python.is_file():
        subprocess.run([uv, "venv", "-q", "--python", "3.12", str(venv)], check=True)
    # torch's CPU build: the CUDA one is gigabytes this machine can't use
    subprocess.run(
        [uv, "pip", "install", "-q", "--python", str(python), "--torch-backend", "cpu",
         *PACKAGES],
        check=True,
    )  # fmt: skip
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="embed_worker")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("setup")
    s.add_argument("root", type=Path)
    r = sub.add_parser("run")
    r.add_argument("folder", type=Path)
    r.add_argument("--threads", type=int, default=8)
    args = parser.parse_args(argv)
    if args.command == "setup":
        return setup(args.root)
    return run(args.folder, args.threads)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
