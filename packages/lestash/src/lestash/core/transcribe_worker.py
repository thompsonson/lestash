"""Words and speakers from recordings: the worker behind LeStash's transcripts (#218).

Standalone: only the standard library at the top, so it runs as a copied script on
another machine (``computer``) as well as here. It reads a job folder:

    job.json    [{"sha256": ..., "speakers": 2}, ...]  (0: count them, 1: one voice)
    in/<sha256>.wav   16 kHz mono 16-bit, made by ffmpeg on the sending side

and writes ``out/<sha256>.json`` for each recording: faster-whisper's segments with
their words and times, and sherpa-onnx's speaker turns. A recording that fails gets
``out/<sha256>.error`` instead. Turning these into text is LeStash's job, not this one.

    python -I transcribe_worker.py setup <root>          make <root>/.venv (computer)
    python -I transcribe_worker.py run <job> --models <dir> --whisper-models <dir>
        [--workers 4] [--threads 6]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

# Bump when the output changes: transcripts made by an older worker are kept, and
# files that failed with it are tried again
WORKER_VERSION = 2
WHISPER_MODEL = "small.en"
PACKAGES = ("faster-whisper==1.2.1", "sherpa-onnx==2.0.1")  # the worker's own environment
_RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
SEGMENTATION = "sherpa-onnx-pyannote-segmentation-3-0"
# English voices. TitaNet small put both hosts of 3 overviews in 4 under one speaker;
# CAM++ split all 4 about evenly, and is faster.
EMBEDDING = "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"
RATE = 16000


def ensure_models(models: Path) -> None:
    """The two speaker models, fetched from sherpa-onnx's releases once (about 36 MB)."""
    models.mkdir(parents=True, exist_ok=True)
    if not (models / SEGMENTATION / "model.onnx").is_file():
        url = f"{_RELEASES}/speaker-segmentation-models/{SEGMENTATION}.tar.bz2"
        with tempfile.TemporaryDirectory(dir=models) as tmp:
            archive = Path(tmp) / "segmentation.tar.bz2"
            with urllib.request.urlopen(url, timeout=120) as r, archive.open("wb") as f:
                shutil.copyfileobj(r, f)
            with tarfile.open(archive) as tar:
                tar.extractall(tmp, filter="data")
            os.replace(Path(tmp) / SEGMENTATION, models / SEGMENTATION)
    if not (models / EMBEDDING).is_file():
        # The release tag's spelling is sherpa-onnx's own
        url = f"{_RELEASES}/speaker-recongition-models/{EMBEDDING}"
        part = models / f"{EMBEDDING}.part"
        with urllib.request.urlopen(url, timeout=120) as r, part.open("wb") as f:
            shutil.copyfileobj(r, f)
        os.replace(part, models / EMBEDDING)


def versions() -> str:
    from importlib.metadata import version

    return (
        f"faster-whisper {version('faster-whisper')}, {WHISPER_MODEL}; "
        f"sherpa-onnx {version('sherpa-onnx')}, campplus-en; worker {WORKER_VERSION}"
    )


def read_wav(path: Path):  # -> numpy float32 array
    import wave

    import numpy as np

    with wave.open(str(path)) as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (RATE, 1, 2):
            raise ValueError(f"not 16 kHz mono 16-bit: {path.name}")
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


_state: dict = {}


def _init(models: str, whisper_models: str, threads: int) -> None:
    """Load the models once per worker process."""
    from faster_whisper import WhisperModel

    _state["whisper"] = WhisperModel(
        WHISPER_MODEL,
        device="cpu",
        compute_type="int8",
        cpu_threads=threads,
        download_root=whisper_models,
    )
    _state["models"] = Path(models)
    _state["threads"] = threads
    _state["versions"] = versions()


def _diarizer(speakers: int):
    import sherpa_onnx

    models, threads = _state["models"], _state["threads"]
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(models / SEGMENTATION / "model.onnx")
            ),
            num_threads=threads,
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(models / EMBEDDING), num_threads=threads
        ),
        # A known count; else voices closer than the threshold are one speaker
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=speakers if speakers > 1 else -1, threshold=0.5
        ),
        min_duration_on=0.2,
        min_duration_off=0.3,
    )
    if not config.validate():
        raise RuntimeError("sherpa-onnx: bad speaker model config")
    return sherpa_onnx.OfflineSpeakerDiarization(config)


def transcribe(wav: Path, speakers: int) -> dict:
    audio = read_wav(wav)
    # Audio, not a path: faster-whisper then needs no decoder. The voice-activity
    # filter skips music and silence, where whisper makes up words.
    segments, _ = _state["whisper"].transcribe(audio, vad_filter=True, word_timestamps=True)
    out = [
        [s.start, s.end, s.text, [[w.start, w.end, w.word] for w in (s.words or [])]]
        for s in segments
    ]
    turns = []
    if speakers != 1:  # one voice: no turns to find
        result = _diarizer(speakers).process(audio).sort_by_start_time()
        turns = [[t.start, t.end, t.speaker] for t in result]
    return {
        "version": _state["versions"],
        "seconds": len(audio) / RATE,
        "segments": out,
        "turns": turns,
    }


def _one(job: dict, folder: str) -> str:
    sha = job["sha256"]
    root = Path(folder)
    try:
        result = transcribe(root / "in" / f"{sha}.wav", int(job.get("speakers", 0)))
        tmp = root / "out" / f"{sha}.json.part"
        tmp.write_text(json.dumps(result))
        os.replace(tmp, root / "out" / f"{sha}.json")
    except Exception as e:  # noqa: BLE001 - one bad recording must not stop the rest
        (root / "out" / f"{sha}.error").write_text(f"{type(e).__name__}: {e}"[:300])
    return sha


def run(folder: Path, models: Path, whisper_models: Path, workers: int, threads: int) -> int:
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor
    from concurrent.futures.process import BrokenProcessPool

    jobs = json.loads((folder / "job.json").read_text())
    (folder / "out").mkdir(exist_ok=True)
    # Fetched once, here: workers starting together would each fetch them
    ensure_models(models)
    from faster_whisper import download_model

    try:
        download_model(WHISPER_MODEL, cache_dir=str(whisper_models), local_files_only=True)
    except Exception:  # noqa: BLE001 - not there yet
        download_model(WHISPER_MODEL, cache_dir=str(whisper_models))

    def pool(batch: list[dict], size: int) -> None:
        # Fresh processes, not forks: this one has loaded threaded native libraries
        with ProcessPoolExecutor(
            max_workers=max(1, min(size, len(batch))),
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_init,
            initargs=(str(models), str(whisper_models), threads),
        ) as workers_:
            for sha in workers_.map(_one, batch, [str(folder)] * len(batch)):
                print(sha, flush=True)

    def finished(job: dict) -> bool:
        return any((folder / "out" / f"{job['sha256']}{x}").exists() for x in (".json", ".error"))

    try:
        pool(jobs, workers)
    except BrokenProcessPool:
        # A recording killed its worker (memory, a crash in a library): the rest are
        # done one at a time, so only that one fails
        for job in jobs:
            if finished(job):
                continue
            try:
                pool([job], 1)
            except BrokenProcessPool:
                (folder / "out" / f"{job['sha256']}.error").write_text(
                    "the worker process died on this recording (out of memory?)"
                )
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
    subprocess.run([uv, "pip", "install", "-q", "--python", str(python), *PACKAGES], check=True)
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="transcribe_worker")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("setup")
    s.add_argument("root", type=Path)
    r = sub.add_parser("run")
    r.add_argument("folder", type=Path)
    r.add_argument("--models", type=Path, required=True)
    r.add_argument("--whisper-models", type=Path, required=True)
    r.add_argument("--workers", type=int, default=1)
    r.add_argument("--threads", type=int, default=4)
    args = parser.parse_args(argv)
    if args.command == "setup":
        return setup(args.root)
    return run(args.folder, args.models, args.whisper_models, args.workers, args.threads)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
