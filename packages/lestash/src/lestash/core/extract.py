"""Text from files that have none: NotebookLM slides, infographics, apps, audio and video
(#218, W3).

Text is read once per stored file and kept in ``blob_text`` (by hash and tool), so a
file in two items, or an item imported again, is never read twice. An item's text is
its own content, then a "## Text from the files" part built from its current media:
``## Slide 7`` and the slide's text for each slide, the text of an infographic, an
app's page text. The part is rebuilt from ``blob_text`` whenever the item is stored,
so an import keeps it and adds no history.

Slides and infographics are pictures: RapidOCR's English model reads them (the same
OCR Docling uses; Docling itself drops most of a slide's text as a picture). Apps are
web pages: their visible text is kept, and the texts in the data a NotebookLM app
draws itself from (``data-app-data``).

Audio overviews and explainer videos are transcribed by ``transcribe_worker``:
faster-whisper's ``small.en`` model for the words, sherpa-onnx for who says them. A
two-host overview reads as turns, ``**Speaker 1** (2:14): ...``, each sentence one
voice's; one voice (a brief, an explainer video) as text with a ``## 12:34`` heading
about each minute. The worker runs here, or on the machine named by ``[transcribe]
host`` (``computer``), which gets the recordings as 16 kHz audio over ssh and keeps
nothing once a batch is back.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from lestash.core import paths
from lestash.core import transcribe_worker as _worker
from lestash.core.blobs import BlobStore, store_root
from lestash.core.config import Config
from lestash.core.database import dump_metadata

logger = logging.getLogger(__name__)

PART = "## Text from the files"
OCR_TOOL = "rapidocr-en"
HTML_TOOL = "html-text"
TRANSCRIPT_TOOL = f"faster-whisper-{_worker.WHISPER_MODEL}+speakers"
# The model folder lestash-voice uses too
REMOTE_ROOT = "lestash-worker"  # the worker's folder in the host's home
FFMPEG_TIMEOUT_S = 600  # converting an hour's recording takes seconds
HEADING_EVERY_S = 60  # a transcript's ``## 12:34`` headings, about this far apart
# Which items get text: NotebookLM artifacts of these kinds
ARTIFACT_KINDS = frozenset({"slides", "infographic", "app", "audio_overview", "explainer_video"})


def wants_text(source_type: str, metadata: dict[str, Any] | None) -> bool:
    metadata = metadata or {}
    return (
        source_type == "notebooklm"
        and metadata.get("type") == "artifact"
        and metadata.get("artifact_type") in ARTIFACT_KINDS
    )


def tool_for(media_type: str, mime_type: str | None) -> str | None:
    """The tool that reads a media row's file, or None if none does."""
    if media_type == "image" and (mime_type or "").startswith("image/"):
        return OCR_TOOL
    if mime_type == "text/html":
        return HTML_TOOL
    if media_type in ("audio", "video") and (mime_type or "").startswith(("audio/", "video/")):
        return TRANSCRIPT_TOOL
    return None


def speakers_for(metadata: dict[str, Any] | None) -> int:
    """How many voices a recording has: 1 narrator, else 2 hosts. (Letting the worker
    count heard 6 voices in an overview's first 17 seconds; every format but the brief
    has two hosts, so one with no format recorded is taken to have two.)"""
    metadata = metadata or {}
    if metadata.get("artifact_type") == "explainer_video":
        return 1
    options = (metadata.get("options") or {}).get("generationOptions") or {}
    return 1 if options.get("showFormat") == "SHOW_FORMAT_THE_BRIEF" else 2


# --------------------------------------------------------------------------- #
# Reading files


class EngineUnavailable(Exception):
    """A tool's engine can't start (e.g. its model can't be fetched): no file can be read."""


def _package_version(name: str) -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(name)
    except PackageNotFoundError:
        return "?"


def tool_version(tool: str) -> str:
    """The version of the tool that reads files now: a file that failed with another
    version is tried again."""
    if tool == OCR_TOOL:
        return f"rapidocr {_package_version('rapidocr')}, PP-OCRv5 en"
    if tool == TRANSCRIPT_TOOL:
        # What the worker reports is kept with each transcript; this is what a file
        # that failed is recorded with
        return f"transcribe worker {_worker.WORKER_VERSION}"
    return "html.parser"


class _Ocr:
    """RapidOCR, loaded once: its models take a second to load."""

    _engine: Any = None

    @classmethod
    def read(cls, path: Path) -> str:
        if cls._engine is None:
            # The default model is Chinese, and runs English words together. The
            # English one is fetched on first use, into rapidocr's models folder.
            try:
                from rapidocr import LangRec, OCRVersion, RapidOCR

                cls._engine = RapidOCR(
                    params={"Rec.lang_type": LangRec.EN, "Rec.ocr_version": OCRVersion.PPOCRV5}
                )
            except Exception as e:  # an import (cv2, onnxruntime) or the model fetch
                raise EngineUnavailable(f"RapidOCR could not start: {e}") from e
        result = cls._engine(str(path))
        return "\n".join(t.strip() for t in (result.txts or ()) if t.strip())


def clock(seconds: float) -> str:
    """``12:34``, or ``1:02:03`` from an hour on."""
    s = int(seconds)
    h, m, s = s // 3600, s // 60 % 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def transcript(segments: Iterable[tuple[float, str]], every: float = HEADING_EVERY_S) -> str:
    """A transcript from (start, text) segments: a ``## 12:34`` heading, then the text
    until the first segment that starts in a later ``every``-second span."""
    parts: list[str] = []
    span: int | None = None
    for start, text in segments:
        text = " ".join(text.split())
        if not text:
            continue
        if span is None or int(start // every) > span:
            parts.append(f"## {clock(start)}\n\n{text}")
            span = int(start // every)
        else:
            parts[-1] += f" {text}"
    return "\n\n".join(parts)


Raw = dict[str, Any]  # a worker's result for one recording


def _speaker_at(start: float, end: float, turns: list[list[Any]]) -> int:
    """The speaker of the turn a word overlaps most, else of the nearest turn."""
    best, overlap = None, 0.0
    for t_start, t_end, speaker in turns:
        o = min(end, t_end) - max(start, t_start)
        if o > overlap:
            best, overlap = speaker, o
    if best is None:
        mid = (start + end) / 2
        best = min(turns, key=lambda t: min(abs(mid - t[0]), abs(mid - t[1])))[2]
    return int(best)


SENTENCE_END = (".", "?", "!")
# A run of one voice inside a sentence is kept only from this long: shorter, it is
# the diarizer's edge falling off, not a second voice. Over 25 stored overviews, 8
# still left 246 phrases cut mid-sentence, 16 left 9 (#381); hosts rarely share one.
RUN_ON_WORDS = 16

Word = tuple[float, str, int]  # (start, text, speaker)


# Titles before a name, when capitalised ("Dr. Smith"; "200 ms." is a unit), and Latin
# linking words: their full stop never ends a sentence
TITLES = frozenset({"Mr", "Mrs", "Ms", "Dr", "Prof"})
LINKING = frozenset({"e.g", "i.e", "vs", "cf"})
BEFORE_A_NUMBER = frozenset({"fig", "approx"})  # "Fig. 3", "approx. 30"
# Words whose full stop may or may not end a sentence: "Apple Inc.", "Main St."
ABBREVIATIONS = frozenset({
    "sr", "jr", "st", "mt", "al", "inc", "ltd", "corp", "dept", "etc", "a.m", "p.m", "ms",
    "min", "sec",
})  # fmt: skip
_DOTTED = re.compile(r"(?:[A-Za-z]\.){2,}")  # U.S., B.F.
_OPENERS = "\"'([“‘"
_CLOSERS = "\"')]”’"


def _ends_sentence(word: str, following: str | None = None) -> bool:
    """Whether a word ends its sentence; ``following`` is the next word, if any.

    A capitalised title ("Dr.") or "e.g." never does; "Fig." and "approx." don't
    before a number. Any other abbreviation ("Inc.", "St.", "200 ms."), an initial
    ("B.") or a dotted acronym ("U.S.") ends a sentence when the next word starts
    with a capital, as in "in the U.S. Right.", and not when it is lowercase; with
    nothing after it, it ends one.

    Text alone can't tell "Baker St. Right." from "St. Louis", or "John B. Watson"
    from "Plan B. Then": these are cut there. A wrong cut matters only if a voice
    change falls on it; a wrong join gives a reply to the wrong voice (#381).
    """
    parts = word.split()
    last = parts[-1].rstrip(_CLOSERS) if parts else ""
    if not last.endswith(SENTENCE_END):
        return False
    if not last.endswith("."):
        return True
    shown = last.lstrip(_OPENERS)[:-1]
    stem = shown.lower()
    # Of a segment kept as one "word", the first word
    nxt = (following or "").split()[0].lstrip(_OPENERS) if (following or "").split() else ""
    if shown in TITLES or stem in LINKING:
        return False
    if stem in BEFORE_A_NUMBER and nxt[:1].isdigit():
        return False
    initial = len(stem) == 1 and shown.isupper()
    if stem in ABBREVIATIONS | BEFORE_A_NUMBER or initial or _DOTTED.fullmatch(stem + "."):
        return not nxt or not nxt[0].islower()
    return True


def _size(run: list[Word]) -> int:
    return sum(len(w[1].split()) for w in run) or 1


def _sentence_runs(sentence: list[Word]) -> list[list[Word]]:
    """A sentence's words as runs of one voice. A run shorter than ``RUN_ON_WORDS``
    goes to the longer run beside it, so a sentence is one voice's unless whisper ran
    two voices' sentences together (#381)."""
    runs: list[list[Word]] = []
    for word in sentence:
        if runs and runs[-1][-1][2] == word[2]:
            runs[-1].append(word)
        else:
            runs.append([word])
    while len(runs) > 1:
        n = min(range(len(runs)), key=lambda i: _size(runs[i]))
        if _size(runs[n]) >= RUN_ON_WORDS:
            break
        sides = [i for i in (n - 1, n + 1) if 0 <= i < len(runs)]
        into = max(sides, key=lambda i: _size(runs[i]))
        speaker = runs[into][0][2]
        moved = [(s, w, speaker) for s, w, _ in runs[n]]
        runs[into] = runs[into] + moved if into < n else moved + runs[into]
        del runs[n]
        # Runs of one voice now side by side are one
        merged: list[list[Word]] = []
        for run in runs:
            if merged and merged[-1][0][2] == run[0][2]:
                merged[-1].extend(run)
            else:
                merged.append(run)
        runs = merged
    return runs


def group_turns(words: list[Word]) -> list[tuple[int, float, str]]:
    """(speaker, start, text) for each turn, from words each given a voice: the voice
    is decided a sentence at a time, never a word at a time. A diarizer's turn edges
    fall a word or two off, and a blip of the other voice inside a turn would split a
    phrase between two speakers (#381)."""
    turns: list[tuple[int, float, list[str]]] = []
    sentence: list[Word] = []

    def flush() -> None:
        for run in _sentence_runs(sentence):
            speaker = run[0][2]
            if turns and turns[-1][0] == speaker:
                turns[-1][2].extend(w for _, w, _ in run)
            else:
                turns.append((speaker, run[0][0], [w for _, w, _ in run]))
        sentence.clear()

    for n, word in enumerate(words):
        sentence.append(word)
        if _ends_sentence(word[1], words[n + 1][1] if n + 1 < len(words) else None):
            flush()
    flush()
    return [(speaker, start, " ".join("".join(ws).split())) for speaker, start, ws in turns]


def speaker_turns(segments: list[list[Any]], turns: list[list[Any]]) -> str:
    """``**Speaker 1** (0:07): ...`` for each turn; speakers numbered as they first speak."""
    words: list[Word] = []
    for seg in segments:
        # A segment whisper gave no word times keeps its text, as one "word"
        for start, end, word in seg[3] or [[seg[0], seg[1], seg[2]]]:
            words.append((start, word, _speaker_at(start, end, turns)))
    names: dict[int, int] = {}
    return "\n\n".join(
        f"**Speaker {names.setdefault(speaker, len(names) + 1)}** ({clock(start)}): {text}"
        for speaker, start, text in group_turns(words)
        if text
    )


_TURN = re.compile(r"\*\*Speaker (\d+)\*\* \((\d+(?::\d\d){1,2})\): (.*)", re.DOTALL)


def _seconds(clock_text: str) -> float:
    seconds = 0
    for part in clock_text.split(":"):
        seconds = seconds * 60 + int(part)
    return float(seconds)


def regroup_transcript(text: str) -> str:
    """A stored transcript's turns grouped again a sentence at a time, as
    ``speaker_turns`` groups them now (#381): for transcripts made before, whose word
    times were not kept. Each word counts as one. Text that is not all turns comes back
    as it is.

    Only each stored turn's start is known. A new turn takes the start of the stored
    turn its first word comes from, with one exception: when that word now has another
    voice than in the stored turn, it is part of a tail moved forward into the next
    stored turn, and the new turn takes that one's start. The tail was said just
    before it, so that time is never before the real one, and late by at most the
    tail. A turn left after its head moved back keeps its stored start: early by at
    most that head."""
    blocks = text.split("\n\n")
    matches = [_TURN.fullmatch(b) for b in blocks]
    if not blocks or not all(matches):
        return text
    starts: list[float] = []
    voices: list[int] = []
    block_of: list[int] = []  # each word's stored turn
    words: list[Word] = []
    for n, m in enumerate(matches):
        assert m is not None
        starts.append(_seconds(m.group(2)))
        voices.append(int(m.group(1)))
        for w in m.group(3).split():
            # The word's number stands in for its start: its turn's time is set below
            words.append((float(len(words)), f" {w}", voices[n]))
            block_of.append(n)

    def start(speaker: int, first: float) -> float:
        n = block_of[int(first)]
        moved = speaker != voices[n] and n + 1 < len(starts)
        return starts[n + 1] if moved else starts[n]

    names: dict[int, int] = {}
    return "\n\n".join(
        f"**Speaker {names.setdefault(speaker, len(names) + 1)}** "
        f"({clock(start(speaker, first))}): {body}"
        for speaker, first, body in group_turns(words)
        if body
    )


@dataclass
class RegroupCounts:
    files: int = 0  # transcripts whose turns changed
    items: int = 0  # items whose text changed


def regroup_transcripts(conn: sqlite3.Connection, *, dry_run: bool = False) -> RegroupCounts:
    """Group the turns of every stored transcript again (``regroup_transcript``) and
    rebuild the items that show them. Needs no recording: only the stored text. With
    ``dry_run``, counts what would change and writes nothing. Call it with no
    transaction open."""
    from lestash.core.database import begin_write, mark_recent_history, max_history_id

    counts = RegroupCounts()
    begin_write(conn)
    try:
        rows = conn.execute(
            "SELECT sha256, text FROM blob_text WHERE tool = ? AND text LIKE '**Speaker %'",
            (TRANSCRIPT_TOOL,),
        ).fetchall()
        changed: list[str] = []
        for sha, text in rows:
            new = regroup_transcript(text)
            if new != text:
                changed.append(sha)
                conn.execute(
                    "UPDATE blob_text SET text = ? WHERE sha256 = ? AND tool = ?",
                    (new, sha, TRANSCRIPT_TOOL),
                )
        counts.files = len(changed)
        item_ids: list[int] = []
        if changed:
            item_ids = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT item_id FROM item_media WHERE superseded_at IS NULL "
                    f"AND sha256 IN ({','.join('?' * len(changed))}) ORDER BY item_id",
                    changed,
                )
            ]
        since = max_history_id(conn)
        updated = [i for i in item_ids if refresh_item(conn, i)]
        mark_recent_history(conn, since, "extract", item_ids=updated)
        counts.items = len(updated)
    except BaseException:
        conn.rollback()
        raise
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    return counts


def transcript_text(raw: Raw) -> str:
    """A recording's text from its worker result: turns when two or more voices speak."""
    segments, turns = raw.get("segments") or [], raw.get("turns") or []
    if turns and len({t[2] for t in turns}) > 1 and any(seg[3] for seg in segments):
        return speaker_turns(segments, turns)
    return transcript((seg[0], seg[2]) for seg in segments)


@dataclass(frozen=True)
class Job:
    sha256: str
    path: Path
    speakers: int


def _tail(output: str | bytes | None, n: int = 300) -> str:
    if isinstance(output, bytes):  # a timeout's output is bytes, even with text=True
        output = output.decode(errors="replace")
    return (output or "").strip()[-n:]


class _WorkerFailed(Exception):
    """The worker ran but stopped part way: the results it wrote are kept."""


class Unfinished(str):
    """A recording a stopped worker never reached: not the file's fault, so it waits."""

    __slots__ = ()


class _Transcriber:
    """Runs the worker on a batch of recordings: here, or on ``[transcribe] host``."""

    settings: dict[str, Any] = {}  # noqa: RUF012 - set by configure()

    @classmethod
    def configure(cls, config: Config) -> None:
        cls.settings = config.get_plugin_config("transcribe")

    @classmethod
    def read(cls, path: Path) -> str:
        """One recording's text (the probe); the voices are counted."""
        raw = cls.run([Job(path.name, path, 0)])[path.name]
        if isinstance(raw, str):
            raise RuntimeError(raw)
        return transcript_text(raw)

    @classmethod
    def run(cls, jobs: list[Job]) -> dict[str, Raw | str]:
        """Each recording's worker result, or why it failed. EngineUnavailable when
        the batch can't run at all (no ffmpeg, the host can't be reached)."""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise EngineUnavailable("transcripts: ffmpeg not found")
        results: dict[str, Raw | str] = {}
        with tempfile.TemporaryDirectory(prefix="lestash-transcribe-") as tmp:
            folder = Path(tmp)
            (folder / "in").mkdir()
            ready = []

            def convert(source: Path, target: Path) -> tuple[int, str]:
                try:
                    done = subprocess.run(
                        [ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(source), "-vn",
                         "-ar", str(_worker.RATE), "-ac", "1", "-c:a", "pcm_s16le", str(target)],
                        capture_output=True, text=True, check=False, timeout=FFMPEG_TIMEOUT_S,
                    )  # fmt: skip
                except subprocess.TimeoutExpired:
                    target.unlink(missing_ok=True)
                    return -1, f"took over {FFMPEG_TIMEOUT_S} s"
                if done.returncode:
                    target.unlink(missing_ok=True)  # a part-made file only takes space
                return done.returncode, done.stderr or ""

            def ffmpeg_works() -> bool:
                """Whether ffmpeg converts a second of silence, made here, into the same
                folder: if not, this machine is at fault (a full disk, a broken
                install), not the recording. Asked after each failure: the disk can
                fill part way through a batch."""
                import wave

                silence = folder / "probe-in.wav"
                try:
                    with wave.open(str(silence), "wb") as w:
                        w.setnchannels(1)
                        w.setsampwidth(2)
                        w.setframerate(_worker.RATE)
                        w.writeframes(b"\0\0" * _worker.RATE)
                except OSError:
                    silence.unlink(missing_ok=True)
                    return False
                try:
                    return convert(silence, folder / "in" / "probe.wav")[0] == 0
                finally:
                    silence.unlink(missing_ok=True)
                    (folder / "in" / "probe.wav").unlink(missing_ok=True)

            for job in jobs:
                code, stderr = convert(job.path, folder / "in" / f"{job.sha256}.wav")
                if code:
                    why = f"ffmpeg: {_tail(stderr, 250) or f'exit {code}'}"
                    # The recording's fault is recorded; this machine's makes it wait
                    # A full disk frees itself as the part-made file goes, so the probe
                    # can't see it; ffmpeg says so
                    full = any(m in stderr for m in ("No space left", "Disk quota exceeded"))
                    local = full or not ffmpeg_works()
                    results[job.sha256] = Unfinished(why) if local else why
                else:
                    ready.append({"sha256": job.sha256, "speakers": job.speakers})
            if not ready:
                return results
            (folder / "job.json").write_text(json.dumps(ready))
            host = cls.settings.get("host")
            missing = Unfinished("no result from the worker")
            try:
                if host:
                    cls._run_on(str(host), folder)
                else:
                    cls._run_here(folder)
            except _WorkerFailed as e:  # the recordings it didn't finish fail, one by one
                missing = Unfinished(f"the worker stopped: {e}")
            except (OSError, subprocess.SubprocessError) as e:
                detail = _tail(getattr(e, "stderr", None)) or str(e)
                raise EngineUnavailable(
                    f"transcripts{f' on {host}' if host else ''}: {detail}"
                ) from e
            for entry in ready:
                sha = str(entry["sha256"])
                out = folder / "out" / f"{sha}.json"
                error = folder / "out" / f"{sha}.error"
                if out.is_file():
                    results[sha] = json.loads(out.read_text())
                else:
                    results[sha] = error.read_text() if error.is_file() else missing
        return results

    @classmethod
    def _sizes(cls) -> tuple[int, int]:
        workers = int(cls.settings.get("workers", 4 if cls.settings.get("host") else 1))
        threads = int(cls.settings.get("threads", 6 if cls.settings.get("host") else 4))
        return max(1, workers), max(1, threads)

    @classmethod
    def batch_size(cls) -> int:
        return 2 * cls._sizes()[0]  # two recordings per worker between budget checks

    @classmethod
    def _run_here(cls, folder: Path) -> None:
        workers, threads = cls._sizes()
        nice = ["nice", "-n", "10"] if shutil.which("nice") else []
        try:
            subprocess.run(
                [*nice, sys.executable, "-I", _worker.__file__, "run", str(folder),
                 "--models", str(paths.speaker_models_dir()),
                 "--whisper-models", str(paths.whisper_models_dir()),
                 "--workers", str(workers), "--threads", str(threads)],
                check=True, capture_output=True, text=True,
            )  # fmt: skip
        except subprocess.CalledProcessError as e:
            raise _WorkerFailed(_tail(e.stderr) or f"exit {e.returncode}") from e

    @classmethod
    def _run_on(cls, host: str, folder: Path) -> None:
        """Copy the batch to ``host``, run the worker there at low priority, copy the
        results back, and delete the batch there."""
        workers, threads = cls._sizes()
        if host.startswith("-"):  # ssh would read it as an option
            raise EngineUnavailable(f"transcripts: not a host name: {host}")

        # Keepalives: a host that drops off mid-batch is noticed in minutes, not hours
        options = "-o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=30"
        options += " -o ServerAliveCountMax=4"

        def ssh(command: str, timeout: float = 120) -> str:
            return subprocess.run(
                ["ssh", *options.split(), "--", host, command],
                check=True, capture_output=True, text=True, timeout=timeout,
            ).stdout  # fmt: skip

        home = ssh('printf %s "$HOME"').strip()
        root = f"{home}/{REMOTE_ROOT}"
        job = f"{root}/jobs/{uuid.uuid4().hex[:12]}"
        unit = f"lestash-transcribe-{job.rsplit('/', 1)[1]}"
        q = shlex.quote
        lock = f"flock {q(root)}/.lock"  # one batch at a time on the host
        ssh(f"mkdir -p {q(job)}")
        failed = None
        try:
            rsync = ["rsync", "-a", "-e", f"ssh {options}"]
            subprocess.run(
                [*rsync, _worker.__file__, f"{host}:{root}/transcribe_worker.py"],
                check=True, capture_output=True, text=True, timeout=120,
            )  # fmt: skip
            # The worker's own environment: made once, its packages pinned
            ssh(f"{lock} python3 -I {q(root)}/transcribe_worker.py setup {q(root)}", timeout=1800)
            subprocess.run(
                [*rsync, f"{folder}/", f"{host}:{job}/"],
                check=True, capture_output=True, text=True, timeout=3600,
            )  # fmt: skip
            # Low priority and capped: CI and the agents on the host come first
            try:
                ssh(
                    f"{lock} systemd-run --user --wait --pipe --collect --quiet "
                    # A recording that runs out of memory kills its worker, not the
                    # batch: the worker then does the rest one at a time
                    f"--unit={unit} --nice=10 -p RuntimeMaxSec=5h -p OOMPolicy=continue "
                    f"-p CPUQuota={workers * threads * 100}% -p MemoryMax={3 * workers}G "
                    f"{q(root)}/.venv/bin/python -I {q(root)}/transcribe_worker.py run "
                    f"{q(job)} --models {q(root)}/models "
                    f"--whisper-models {q(root)}/whisper-models "
                    f"--workers {workers} --threads {threads}",
                    timeout=6 * 3600,
                )
            except subprocess.CalledProcessError as e:
                if e.returncode == 255:  # ssh's own failure: the host went away
                    raise
                failed = _tail(e.stderr) or f"exit {e.returncode}"
            back = subprocess.run(
                [*rsync, f"{host}:{job}/out/", f"{folder}/out/"],
                check=failed is None, capture_output=True, text=True, timeout=600,
            )  # fmt: skip
            if failed is not None:
                logger.warning("Transcribe worker on %s stopped: %s", host, failed)
                if back.returncode:
                    (folder / "out").mkdir(exist_ok=True)
                raise _WorkerFailed(failed)
        finally:
            # A run cut short (a timeout, Ctrl-C) leaves no unit burning the host
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                ssh(f"systemctl --user stop {unit} 2>/dev/null; rm -rf {q(job)}")


class _PageText(HTMLParser):
    SKIP = frozenset({"script", "style", "noscript", "template", "svg"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skipping += 1
        for name, value in attrs:
            # A NotebookLM app draws its page with scripts from data in this attribute:
            # flashcards, mind maps, quizzes
            if name == "data-app-data" and value:
                with contextlib.suppress(ValueError, RecursionError):  # nested too deep
                    self.parts.extend(_strings(json.loads(value)))

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skipping:
            self._skipping -= 1

    def handle_data(self, data: str) -> None:
        if not self._skipping and data.strip():
            self.parts.append(" ".join(data.split()))


def _strings(value: Any) -> list[str]:
    """Every text in a JSON value, in order, one per line."""
    if isinstance(value, str):
        text = " ".join(value.split())
        return [text] if text else []
    if isinstance(value, dict):
        return [t for v in value.values() for t in _strings(v)]
    if isinstance(value, list):
        return [t for v in value for t in _strings(v)]
    return []


def page_text(path: Path) -> str:
    """A web page's visible text, one run of text per line. Only parsed, never run."""
    parser = _PageText()
    parser.feed(path.read_text(encoding="utf-8", errors="replace"))
    parser.close()
    return "\n".join(parser.parts)


def read_file(tool: str, path: Path) -> tuple[str, str]:
    """The text of a file and the tool's version."""
    if tool == OCR_TOOL:
        return _Ocr.read(path), tool_version(tool)
    if tool == HTML_TOOL:
        return page_text(path), tool_version(tool)
    if tool == TRANSCRIPT_TOOL:
        return _Transcriber.read(path), tool_version(tool)
    raise ValueError(f"unknown tool {tool}")


def _failed(version: str) -> str:
    return f"failed [{version}]"


# --------------------------------------------------------------------------- #
# Building an item's text


@dataclass(frozen=True)
class Media:
    media_type: str
    mime_type: str | None
    position: int
    alt_text: str | None
    sha256: str
    order: int = 1 << 62  # its row id: a later version is newer; one being imported newest


def base_content(content: str | None) -> str:
    """An item's own content, without the part built from its files."""
    content = content or ""
    cut = content.find("\n\n" + PART + "\n")
    if cut < 0 and content.startswith(PART + "\n"):
        cut = 0
    return content[:cut] if cut >= 0 else content


Texts = dict[tuple[str, str], tuple[str, str]]  # (sha256, tool): (text, tool version)


def compose(content: str | None, media: Iterable[Media], texts: Texts) -> tuple[str, list[str]]:
    """``content`` with its files' text, and the tool versions that read it."""
    sections = []
    versions: set[str] = set()
    # One file per place: a deck made again keeps its old slides as media too, and
    # only the newest at each position is read into the text
    newest: dict[tuple[str, int, str], Media] = {}
    for m in media:
        tool = tool_for(m.media_type, m.mime_type)
        if tool is None:
            continue
        key = (m.media_type, m.position, tool)
        if key not in newest or m.order > newest[key].order:
            newest[key] = m
    for m in sorted(newest.values(), key=lambda m: (m.media_type != "image", m.position)):
        tool = tool_for(m.media_type, m.mime_type) or ""
        text, version = texts.get((m.sha256, tool), ("", ""))
        text = text.strip()
        if not text:
            continue
        versions.add(version)
        heading = m.alt_text if (m.alt_text or "").startswith("Slide ") else None
        sections.append(f"## {heading}\n\n{text}" if heading else text)
    base = base_content(content)
    if not sections:
        return base, []
    return f"{base}\n\n{PART}\n\n" + "\n\n".join(sections), sorted(versions)


def with_text_from(metadata: dict[str, Any] | None, versions: list[str]) -> dict[str, Any]:
    """``metadata`` naming the tools that read its files' text (``text_from``)."""
    out = {k: v for k, v in (metadata or {}).items() if k != "text_from"}
    if versions:
        out["text_from"] = versions
    return out


def _current_media(conn: sqlite3.Connection, item_id: int) -> list[Media]:
    return [
        Media(*row)
        for row in conn.execute(
            "SELECT media_type, mime_type, position, alt_text, sha256, id FROM item_media "
            "WHERE item_id = ? AND sha256 IS NOT NULL AND superseded_at IS NULL",
            (item_id,),
        )
    ]


def _texts(conn: sqlite3.Connection, media: Iterable[Media]) -> Texts:
    shas = sorted({m.sha256 for m in media})
    if not shas:
        return {}
    rows = conn.execute(
        "SELECT sha256, tool, text, tool_version FROM blob_text "
        f"WHERE sha256 IN ({','.join('?' * len(shas))})",
        shas,
    )
    return {(sha, tool): (text, version or "") for sha, tool, text, version in rows}


def content_on_import(
    conn: sqlite3.Connection,
    source_type: str,
    content: str | None,
    metadata: dict[str, Any] | None,
    media: Iterable[Any],
    item_id: int | None,
) -> tuple[str, dict[str, Any]] | None:
    """The content and metadata to store for an item being imported: its files' text
    added, from the media it has and the ones it brings. None if it gets no text."""
    if not wants_text(source_type, metadata):
        return None
    incoming = [
        # In the order they come, after every stored row
        Media(m.media_type, m.mime_type, m.position, m.alt_text, m.sha256, (1 << 62) + n)
        for n, m in enumerate(m for m in media if getattr(m, "sha256", None))
    ]
    stored = _current_media(conn, item_id) if item_id is not None else []
    # An incoming file already stored keeps its row's place among the versions
    known = {(m.media_type, m.position, m.sha256) for m in stored}
    every = stored + [m for m in incoming if (m.media_type, m.position, m.sha256) not in known]
    text, versions = compose(content, every, _texts(conn, every))
    return text, with_text_from(metadata, versions)


def refresh_item(conn: sqlite3.Connection, item_id: int) -> bool:
    """Rebuild one item's text from its current media; True if it changed. Does not
    commit."""
    row = conn.execute("SELECT content, metadata FROM items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return False
    media = _current_media(conn, item_id)
    new, versions = compose(row[0], media, _texts(conn, media))
    try:
        metadata = json.loads(row[1]) if row[1] else {}
    except ValueError:
        metadata = {}
    new_metadata = with_text_from(metadata, versions)
    if new == (row[0] or "") and new_metadata == metadata:
        return False
    conn.execute(
        "UPDATE items SET content = ?, metadata = ? WHERE id = ?",
        (new, dump_metadata(new_metadata) if new_metadata else None, item_id),
    )
    return True


# --------------------------------------------------------------------------- #
# The background step


@dataclass
class ExtractCounts:
    files: int = 0  # files read this run
    failed: int = 0  # files that could not be read: recorded, tried again by a new version
    stopped: str | None = None  # why the run stopped early: the rest wait
    engine_failed: bool = False  # the tool itself is broken, not one file
    items: int = 0  # items whose text changed
    pending: int = 0  # files left for the next run (time budget)


def _items_wanting_text(conn: sqlite3.Connection) -> list[int]:
    kinds = sorted(ARTIFACT_KINDS)
    return [
        r[0]
        for r in conn.execute(
            "SELECT id FROM items WHERE source_type = 'notebooklm' "
            "AND json_valid(metadata) AND json_extract(metadata, '$.type') = 'artifact' "
            f"AND json_extract(metadata, '$.artifact_type') IN ({','.join('?' * len(kinds))}) "
            "ORDER BY id",
            kinds,
        )
    ]


def _still_reads(conn: sqlite3.Connection, store: BlobStore, tool: str) -> bool | None:
    """Whether the tool reads again a file it read before: if so, files that fail are
    bad, not the tool. None if it has read no file still in the store."""
    # The smallest first: transcribing a long recording takes minutes
    for (sha,) in conn.execute(
        "SELECT t.sha256 FROM blob_text t LEFT JOIN blobs b ON b.sha256 = t.sha256 "
        "WHERE t.tool = ? AND t.tool_version NOT LIKE 'failed [%' "
        "ORDER BY coalesce(b.size, 0) = 0, b.size, t.extracted_at DESC",  # 0: size unknown
        (tool,),
    ):
        path = store.path(sha)
        if path.is_file():
            try:
                read_file(tool, path)
            except EngineUnavailable:  # can't tell now (the host is away)
                return None
            except Exception:  # noqa: BLE001 - any failure: the tool is broken
                return False
            return True
    return None


def extract_pending(
    conn: sqlite3.Connection,
    config: Config | None = None,
    *,
    budget_s: float | None = None,
    retry_failed: bool = False,
) -> ExtractCounts:
    """Read each wanted file not read yet, then rebuild the items whose text changed.

    Files are read with no write open; each file's text is committed on its own, so a
    run stopped part-way keeps what it read. ``budget_s`` stops reading after that
    many seconds; the rest wait for the next run. Call it with no transaction open.

    A file that fails is recorded, and read again once the tool's version changes,
    or with ``retry_failed``. Failures are only recorded once a file in the run has
    been read: a run whose first reads all fail stops, as the tool is broken.
    """
    from lestash.core.database import mark_recent_history, max_history_id, write_transaction

    config = config or Config.load()
    _Transcriber.configure(config)
    store = BlobStore(store_root(config))
    counts = ExtractCounts()
    item_ids = _items_wanting_text(conn)
    todo: dict[tuple[str, str], str | None] = {}  # (sha, tool): mime
    speakers: dict[str, int] = {}  # a recording's voices, from its item
    for item_id in item_ids:
        media = _current_media(conn, item_id)
        if any(tool_for(m.media_type, m.mime_type) == TRANSCRIPT_TOOL for m in media):
            (raw_metadata,) = conn.execute(
                "SELECT metadata FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            voices = speakers_for(json.loads(raw_metadata) if raw_metadata else None)
        for m in media:
            if tool := tool_for(m.media_type, m.mime_type):
                todo.setdefault((m.sha256, tool), m.mime_type)
                if tool == TRANSCRIPT_TOOL:
                    speakers[m.sha256] = max(speakers.get(m.sha256, voices), voices)
    done = set()
    for sha, tool, version in conn.execute("SELECT sha256, tool, tool_version FROM blob_text"):
        if (sha, tool) not in todo:
            continue
        version = version or ""
        if version.startswith("failed [") and (
            retry_failed or not version.startswith(_failed(tool_version(tool)))
        ):
            continue  # failed with another version: try again
        done.add((sha, tool))
    started = time.monotonic()
    pending = sorted(key for key in todo if key not in done)
    # Transcripts last, in batches: they are slow, and a recording that kills the run
    # then stops no picture or page from being read
    files = [key for key in pending if key[1] != TRANSCRIPT_TOOL]
    recordings = [key[0] for key in pending if key[1] == TRANSCRIPT_TOOL]
    # Failures of a tool are kept back until that tool reads a file: if it reads none,
    # the tool is broken, not the files, and they are read again next run
    held: dict[str, list[tuple[str, str, str, str]]] = {}
    good: set[str] = set()
    # A tool whose engine can't start: its files wait, and the other tools go on
    unavailable: dict[str, str] = {}
    waiting = 0  # files of an unavailable tool

    def write(rows: list[tuple[str, str, str, str]]) -> bool:
        try:
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO blob_text (sha256, tool, tool_version, text) "
                    "VALUES (?, ?, ?, ?)",
                    rows,
                )
        except sqlite3.OperationalError as e:  # a sync holds the lock: go on next run
            counts.stopped = f"database: {e}"
            return False
        return True

    def failed(sha: str, tool: str, why: str) -> bool:
        """Record (or hold back) a file that could not be read; False: stop."""
        counts.failed += 1
        failure = (sha, tool, f"{_failed(tool_version(tool))}: {why}"[:300], "")
        if tool in good:
            return write([failure])
        held.setdefault(tool, []).append(failure)
        return True

    def read(sha: str, tool: str, text: str, version: str) -> bool:
        counts.files += 1
        good.add(tool)
        return write([*held.pop(tool, []), (sha, tool, version, text)])

    def over_budget() -> bool:
        return budget_s is not None and time.monotonic() - started > budget_s

    for n, (sha, tool) in enumerate(files):
        if over_budget():
            counts.pending = len(files) - n + len(recordings)
            recordings = []
            break
        if tool in unavailable:
            waiting += 1
            continue
        path = store.path(sha)
        if not path.is_file():
            logger.warning("Not in the file store: %s", sha)  # not the file's fault
            continue
        try:
            text, version = read_file(tool, path)
        except EngineUnavailable as e:  # every file of this tool would fail
            logger.warning("%s", e)
            unavailable[tool] = str(e)
            held.pop(tool, None)
            waiting += 1
            continue
        except Exception as e:  # noqa: BLE001 - one bad file must not stop the rest
            logger.warning("Could not read text from %s (%s): %s", path.name, tool, e)
            if not failed(sha, tool, f"{type(e).__name__}: {e}"):
                recordings = []
                break
            continue
        if not read(sha, tool, text, version):
            recordings = []
            break
    jobs = []
    for sha in recordings:
        path = store.path(sha)
        if path.is_file():
            jobs.append(Job(sha, path, speakers.get(sha, 0)))
        else:
            logger.warning("Not in the file store: %s", sha)
    size = _Transcriber.batch_size()
    for n in range(0, len(jobs), size):
        if over_budget():
            counts.pending += len(jobs) - n
            break
        batch = jobs[n : n + size]
        try:
            results = _Transcriber.run(batch)
        except EngineUnavailable as e:
            logger.warning("%s", e)
            unavailable[TRANSCRIPT_TOOL] = str(e)
            held.pop(TRANSCRIPT_TOOL, None)
            waiting += len(jobs) - n
            break
        ok, stopped = True, None
        for job in batch:
            raw = results.get(job.sha256, "no result")
            if isinstance(raw, Unfinished):
                waiting += 1
                stopped = str(raw)
                continue
            if isinstance(raw, str):
                logger.warning("Could not transcribe %s: %s", job.sha256, raw)
                ok = failed(job.sha256, TRANSCRIPT_TOOL, raw)
            else:
                ok = read(job.sha256, TRANSCRIPT_TOOL, transcript_text(raw), raw["version"])
            if not ok:
                break
        if stopped:  # the rest wait too, and the run says why
            logger.warning("Transcripts: %s", stopped)
            unavailable[TRANSCRIPT_TOOL] = f"transcripts: {stopped}"
            held.pop(TRANSCRIPT_TOOL, None)
            waiting += len(jobs) - n - len(batch)
            break
        if not ok:
            break
    if not counts.stopped and not counts.pending:  # judged only on a whole run
        broken = []
        for tool, rows in sorted(held.items()):
            reads = _still_reads(conn, store, tool)
            if reads is False or (reads is None and len(rows) >= 3):
                broken.append(tool)
            elif not write(rows):  # the files, not the tool: record them
                break
        if broken and not counts.stopped:
            counts.stopped = f"{', '.join(broken)} read no file and failed on every one"
            counts.engine_failed = True
    counts.pending += waiting  # after the judging above, which a budget stop skips
    if unavailable:
        counts.engine_failed = True
        counts.stopped = "; ".join([*unavailable.values(), *filter(None, [counts.stopped])])
    try:
        with write_transaction(conn):  # read and write each item under one lock
            since = max_history_id(conn)
            changed = [i for i in item_ids if refresh_item(conn, i)]
            mark_recent_history(conn, since, "extract", item_ids=changed)
    except sqlite3.OperationalError as e:
        counts.stopped = "; ".join(filter(None, [counts.stopped, f"database: {e}"]))
        return counts
    counts.items = len(changed)
    return counts
