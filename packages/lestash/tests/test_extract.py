"""Text from NotebookLM slides, infographics, apps, audio and video (#218, W3)."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest
from lestash.core import extract
from lestash.core.blobs import BlobStore, store_root
from lestash.core.database import get_connection
from lestash.core.extract import PART, Media, base_content, compose, extract_pending, page_text
from lestash.core.sync import SyncStore
from lestash.models.item import ItemCreate, MediaCreate

NB = "Takeout/NotebookLM/Hebbian"


@pytest.fixture
def fake_ocr(monkeypatch):
    """OCR that 'reads' a picture's bytes as its text, and counts its reads."""
    reads: list[str] = []

    def read(cls, path):
        reads.append(path.name)
        return path.read_bytes().decode()

    monkeypatch.setattr(extract._Ocr, "read", classmethod(read))
    real = extract.tool_version
    monkeypatch.setattr(
        extract,
        "tool_version",
        lambda tool: "fake-ocr 1" if tool == extract.OCR_TOOL else real(tool),
    )
    return reads


def _deck(store: BlobStore, slides: list[bytes], app: bytes | None = None) -> ItemCreate:
    media = []
    for n, data in enumerate(slides, start=1):
        blob = store.put_stream(io.BytesIO(data))
        media.append(
            MediaCreate(
                media_type="image",
                mime_type="image/png",
                alt_text=f"Slide {n}",
                position=n,
                source_origin="takeout",
                sha256=blob.sha256,
                size=blob.size,
            )
        )
    if app is not None:
        blob = store.put_stream(io.BytesIO(app))
        media.append(
            MediaCreate(
                media_type="file",
                mime_type="text/html",
                alt_text="Quiz.html",
                position=0,
                source_origin="takeout",
                sha256=blob.sha256,
                size=blob.size,
            )
        )
    return ItemCreate(
        source_type="notebooklm",
        source_id=f"{NB}/Artifacts/Deck metadata",
        title="Deck",
        content="# Deck\n\nNotebookLM slides",
        is_own_content=True,
        metadata={"type": "artifact", "artifact_type": "slides", "_parent_source_id": NB},
        media=media,
    )


def _item(conn) -> tuple[int, str, dict]:
    row = conn.execute(
        "SELECT id, content, metadata FROM items WHERE source_id = ?",
        (f"{NB}/Artifacts/Deck metadata",),
    ).fetchone()
    return row[0], row[1], json.loads(row[2])


def test_slides_get_their_text_under_slide_headings(test_db, fake_ocr) -> None:
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run([_deck(store, [b"Hebbian learning", b"Fire together"])])
        counts = extract_pending(conn, test_db)
        _, content, metadata = _item(conn)
    assert (counts.files, counts.items, counts.failed) == (2, 1, 0)
    assert content == (
        f"# Deck\n\nNotebookLM slides\n\n{PART}\n\n"
        "## Slide 1\n\nHebbian learning\n\n## Slide 2\n\nFire together"
    )
    assert metadata["text_from"] == ["fake-ocr 1"]


def test_a_file_is_read_once_and_an_import_keeps_the_text(test_db, fake_ocr) -> None:
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run([_deck(store, [b"one", b"one"])])  # same picture
        extract_pending(conn, test_db)
        assert len(fake_ocr) == 1
        _, before, _ = _item(conn)
        history = conn.execute("SELECT count(*) FROM item_history").fetchone()[0]
        # The next export yields the item with its description's content only
        again = SyncStore(conn, "notebooklm")
        again.run([_deck(store, [b"one", b"one"])])
        assert again.counts.updated == 0 and again.counts.unchanged == 1
        assert _item(conn)[1] == before
        assert conn.execute("SELECT count(*) FROM item_history").fetchone()[0] == history
        assert extract_pending(conn, test_db).files == 0 and len(fake_ocr) == 1


def test_a_slide_from_a_later_part_adds_its_text(test_db, fake_ocr) -> None:
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run([_deck(store, [b"first"])])
        extract_pending(conn, test_db)
        later = _deck(store, [b"first", b"second"])
        later.media = later.media[1:]  # the later part holds slide two only
        SyncStore(conn, "notebooklm").run([later])
        extract_pending(conn, test_db)
        _, content, _ = _item(conn)
    assert content.endswith("## Slide 1\n\nfirst\n\n## Slide 2\n\nsecond")


def test_an_app_page_gives_its_visible_text_and_never_its_scripts(test_db, fake_ocr) -> None:
    page = (
        b"<html><head><style>p{}</style><script>alert('x')</script></head>"
        b"<body><h1>Quiz</h1><p>What  fires\ntogether?</p></body></html>"
    )
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run([_deck(store, [], app=page)])
        extract_pending(conn, test_db)
        _, content, metadata = _item(conn)
    assert content.endswith(f"{PART}\n\nQuiz\nWhat fires together?")
    assert metadata["text_from"] == ["html.parser"]


def _picky(cls, path):
    data = path.read_bytes().decode()
    if data.startswith("bad"):
        raise RuntimeError("bad picture")
    return data


def test_a_file_that_cannot_be_read_is_recorded_and_read_again_by_a_new_version(
    test_db, fake_ocr, monkeypatch
) -> None:
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run([_deck(store, [b"good", b"bad", b"fine"])])
        monkeypatch.setattr(extract._Ocr, "read", classmethod(_picky))
        first = extract_pending(conn, test_db)
        assert (first.files, first.failed, first.stopped) == (2, 1, None)
        assert extract_pending(conn, test_db).failed == 0  # recorded: not read again
        assert extract_pending(conn, test_db, retry_failed=True).failed == 1
        monkeypatch.setattr(extract, "tool_version", lambda tool: "rapidocr 9")
        assert extract_pending(conn, test_db).failed == 1  # a new version tries it


def test_a_run_whose_first_reads_all_fail_records_nothing(test_db, fake_ocr, monkeypatch) -> None:
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run([_deck(store, [b"bad 1", b"bad 2", b"bad 3"])])
        monkeypatch.setattr(extract._Ocr, "read", classmethod(_picky))
        counts = extract_pending(conn, test_db)
        assert counts.engine_failed and counts.failed == 3
        assert conn.execute("SELECT count(*) FROM blob_text").fetchone()[0] == 0


def test_the_ocr_engine_names_its_version(monkeypatch, tmp_path) -> None:
    """The real reader, with RapidOCR stood in for: its version comes from the package."""
    import sys
    import types

    calls = []

    class FakeRapidOCR:
        def __init__(self, params):
            calls.append(params)

        def __call__(self, path):
            return types.SimpleNamespace(txts=(" Hello ", "", "world"))

    fake = types.SimpleNamespace(
        RapidOCR=FakeRapidOCR,
        LangRec=types.SimpleNamespace(EN="en"),
        OCRVersion=types.SimpleNamespace(PPOCRV5="v5"),
    )
    monkeypatch.setitem(sys.modules, "rapidocr", fake)
    monkeypatch.setattr(extract._Ocr, "_engine", None)
    picture = tmp_path / "p.png"
    picture.write_bytes(b"x")
    text, version = extract.read_file(extract.OCR_TOOL, picture)
    assert text == "Hello\nworld" and len(calls) == 1
    assert version.startswith("rapidocr 3.") and "en" in version
    extract.read_file(extract.OCR_TOOL, picture)
    assert len(calls) == 1  # loaded once


def test_an_ocr_engine_that_cannot_start_stops_the_run(test_db, fake_ocr, monkeypatch) -> None:
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run([_deck(store, [b"one", b"two"])])

        def no_engine(cls, path):
            raise extract.EngineUnavailable("model could not be fetched")

        monkeypatch.setattr(extract._Ocr, "read", classmethod(no_engine))
        counts = extract_pending(conn, test_db)
        assert counts.stopped and counts.failed == 0
        assert conn.execute("SELECT count(*) FROM blob_text").fetchone()[0] == 0
        monkeypatch.setattr(extract._Ocr, "read", classmethod(lambda cls, p: "read"))
        assert extract_pending(conn, test_db).files == 2


def test_a_deck_made_again_reads_only_its_newest_slides(test_db, fake_ocr) -> None:
    """The old export's slides stay as media beside the new ones at the same places."""
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run([_deck(store, [b"old one", b"old two"])])
        extract_pending(conn, test_db)
        SyncStore(conn, "notebooklm").run([_deck(store, [b"new one", b"new two"])])
        extract_pending(conn, test_db)
        content = _item(conn)[1]
        # And an import of the old export again changes nothing
        SyncStore(conn, "notebooklm").run([_deck(store, [b"old one", b"old two"])])
        assert _item(conn)[1] == content
    assert content.endswith("## Slide 1\n\nnew one\n\n## Slide 2\n\nnew two")


def test_a_time_budget_leaves_the_rest_for_next_time(test_db, fake_ocr) -> None:
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run([_deck(store, [b"a", b"b", b"c"])])
        first = extract_pending(conn, test_db, budget_s=-1)  # out of time at once
        assert (first.files, first.pending) == (0, 3)
        assert extract_pending(conn, test_db).files == 3


def test_other_items_get_no_text_part(test_db, fake_ocr) -> None:
    store = BlobStore(store_root(test_db))
    item = _deck(store, [b"x"])
    item.metadata = {"type": "artifact", "artifact_type": "file"}
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run([item])
        assert extract_pending(conn, test_db).files == 0
        assert PART not in _item(conn)[1]


def test_base_content_and_compose() -> None:
    texts = {("a" * 64, extract.OCR_TOOL): ("text a", "v1")}
    slide = Media("image", "image/png", 1, "Slide 1", "a" * 64)
    built, versions = compose("own", [slide], texts)
    assert built == f"own\n\n{PART}\n\n## Slide 1\n\ntext a" and versions == ["v1"]
    assert base_content(built) == "own"
    assert compose(built, [slide], texts)[0] == built  # rebuilding gives the same
    assert compose(built, [], {})[0] == "own"  # no files with text: the part goes


def test_page_text_skips_scripts() -> None:
    from pathlib import Path
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as d:
        page = Path(d) / "p.html"
        page.write_text("<p>a</p><script>b</script><svg><text>c</text></svg><p>d</p>")
        assert page_text(page) == "a\nd"


def test_bad_files_first_do_not_stop_the_good_ones(test_db, fake_ocr, monkeypatch) -> None:
    """Files are read in hash order, so the same bad ones come first every run."""
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run(
            [_deck(store, [b"bad 1", b"bad 2", b"bad 3", b"bad 4", b"good"])]
        )
        monkeypatch.setattr(extract._Ocr, "read", classmethod(_picky))
        counts = extract_pending(conn, test_db)
        assert (counts.files, counts.failed, counts.engine_failed) == (1, 4, False)
        assert extract_pending(conn, test_db).failed == 0  # all recorded


def test_a_file_missing_from_the_store_is_skipped(test_db, fake_ocr) -> None:
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        deck = _deck(store, [b"gone", b"here"])
        SyncStore(conn, "notebooklm").run([deck])
        store.path(deck.media[0].sha256).unlink()
        counts = extract_pending(conn, test_db)
    assert (counts.files, counts.failed) == (1, 0)


def test_an_apps_own_data_gives_its_cards(tmp_path) -> None:
    page = tmp_path / "app.html"
    data = json.dumps({"flashcards": [{"f": "What is  Hebb's rule?", "b": "Fire together"}]})
    page.write_text(
        f'<title>Cards</title><app-root data-app-data="{data.replace(chr(34), "&quot;")}">'
    )
    assert page_text(page) == "Cards\nWhat is Hebb's rule?\nFire together"


def test_new_bad_files_after_good_ones_are_recorded(test_db, fake_ocr, monkeypatch) -> None:
    """Every good file read in earlier runs: the tool still reads one of them, so the
    new failures are the files', and are recorded."""
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run([_deck(store, [b"good 1", b"good 2"])])
        extract_pending(conn, test_db)
        SyncStore(conn, "notebooklm").run(
            [_deck(store, [b"good 1", b"good 2", b"bad 1", b"bad 2", b"bad 3"])]
        )
        monkeypatch.setattr(extract._Ocr, "read", classmethod(_picky))
        counts = extract_pending(conn, test_db)
        assert (counts.failed, counts.engine_failed, counts.stopped) == (3, False, None)
        assert extract_pending(conn, test_db).failed == 0


def test_deeply_nested_app_data_keeps_the_page_text(tmp_path) -> None:
    page = tmp_path / "app.html"
    page.write_text('<p>Visible</p><app-root data-app-data="' + "[" * 100000 + '">')
    assert page_text(page) == "Visible"


# --------------------------------------------------------------------------- #
# Audio and video (step 2)


def _recording(
    store: BlobStore,
    data: bytes,
    kind: str,
    mime: str,
    show: str | None = None,
    name: str | None = None,
) -> ItemCreate:
    blob = store.put_stream(io.BytesIO(data))
    metadata = {"type": "artifact", "artifact_type": kind, "_parent_source_id": NB}
    if show:
        metadata["options"] = {"generationOptions": {"showFormat": show}}
    return ItemCreate(
        source_type="notebooklm",
        source_id=f"{NB}/Artifacts/{name or kind} metadata",
        title=kind,
        content=f"# {kind}",
        is_own_content=True,
        metadata=metadata,
        media=[
            MediaCreate(
                media_type=mime.split("/")[0],
                mime_type=mime,
                position=0,
                source_origin="takeout",
                sha256=blob.sha256,
                size=blob.size,
            )
        ],
    )


def _raw(text: str, speakers: int) -> dict:
    """A worker result: one word a second; with two voices, they take turns each word."""
    words = text.split()
    segments = [
        [float(n), n + 0.9, f" {w}", [[float(n), n + 0.9, f" {w}"]]] for n, w in enumerate(words)
    ]
    turns = [[float(n), n + 0.95, n % 2] for n in range(len(words))] if speakers != 1 else []
    return {"version": "fake-worker 1", "seconds": len(words), "segments": segments, "turns": turns}


@pytest.fixture
def fake_worker(monkeypatch):
    """A worker that 'hears' a recording's bytes as its words; it records its jobs."""
    jobs: list[extract.Job] = []

    def run(cls, batch):
        jobs.extend(batch)
        return {j.sha256: _raw(j.path.read_bytes().decode(), j.speakers) for j in batch}

    monkeypatch.setattr(extract._Transcriber, "run", classmethod(run))
    return jobs


def test_clock_and_transcript_headings() -> None:
    assert [extract.clock(s) for s in (0, 59.9, 754, 3725)] == ["0:00", "0:59", "12:34", "1:02:05"]
    segments = [(0.0, " Hello "), (20.0, "there."), (25.0, "  "), (61.5, "Next"), (130.0, "Last")]
    assert extract.transcript(segments) == (
        "## 0:00\n\nHello there.\n\n## 1:01\n\nNext\n\n## 2:10\n\nLast"
    )
    assert extract.transcript([]) == ""


def _words(*timed: tuple[float, str]) -> list[list]:
    return [[t, t + 0.5, f" {w}"] for t, w in timed]


def test_speaker_turns_number_voices_and_move_a_stray_word() -> None:
    segments = [
        [0.0, 3.0, "", _words((0, "It's"), (1, "so"), (2, "confident."), (3, "And"))],
        [4.0, 8.0, "", _words((4, "then"), (5, "it"), (6, "crashes."))],
        [9.0, 10.0, "", _words((9, "Exactly."))],
    ]
    turns = [[0.0, 3.6, 7], [3.6, 8.5, 3], [8.8, 10.0, 7]]
    assert extract.speaker_turns(segments, turns) == (
        "**Speaker 1** (0:00): It's so confident.\n\n"
        "**Speaker 2** (0:03): And then it crashes.\n\n"
        "**Speaker 1** (0:09): Exactly."
    )


def test_a_turn_ending_mid_sentence_keeps_long_tails() -> None:
    segments = [
        [0.0, 5.0, "", _words((0, "one"), (1, "two"), (2, "three"), (3, "four"), (4, "five"))]
    ]
    turns = [[0.0, 3.5, 0], [3.5, 5.0, 1]]
    # No full stop to cut at: the words stay with the voice heard
    assert extract.speaker_turns(segments, turns) == (
        "**Speaker 1** (0:00): one two three four\n\n**Speaker 2** (0:04): five"
    )


def test_one_voice_reads_as_minutes() -> None:
    raw = {"segments": [[0.0, 2.0, " Hello.", []], [61.0, 62.0, " Later.", []]], "turns": []}
    assert extract.transcript_text(raw) == "## 0:00\n\nHello.\n\n## 1:01\n\nLater."
    # The worker counted one voice
    raw["turns"] = [[0.0, 62.0, 0]]
    assert extract.transcript_text(raw).startswith("## 0:00")


def test_how_many_voices() -> None:
    def show(fmt):
        return {
            "artifact_type": "audio_overview",
            "options": {"generationOptions": {"showFormat": fmt}},
        }

    assert extract.speakers_for(show("SHOW_FORMAT_UNSPECIFIED")) == 2
    assert extract.speakers_for(show("SHOW_FORMAT_THE_BRIEF")) == 1
    assert extract.speakers_for({"artifact_type": "audio_overview"}) == 2  # not recorded
    assert extract.speakers_for({"artifact_type": "explainer_video"}) == 1


def test_audio_and_video_get_their_transcripts(test_db, fake_worker) -> None:
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run(
            [
                _recording(
                    store, b"Hebbian plasticity.", "audio_overview", "audio/x-wav",
                    "SHOW_FORMAT_UNSPECIFIED",
                ),
                _recording(store, b"Neurons wire", "explainer_video", "video/mp4"),
            ]
        )  # fmt: skip
        counts = extract_pending(conn, test_db)
        rows = dict(
            conn.execute(
                "SELECT json_extract(metadata, '$.artifact_type'), content FROM items "
                "WHERE json_extract(metadata, '$.text_from[0]') = 'fake-worker 1'"
            ).fetchall()
        )
    assert (counts.files, counts.items, counts.failed) == (2, 2, 0)
    assert sorted(j.speakers for j in fake_worker) == [1, 2]
    assert rows["audio_overview"] == (
        f"# audio_overview\n\n{PART}\n\n"
        "**Speaker 1** (0:00): Hebbian\n\n**Speaker 2** (0:01): plasticity."
    )
    assert rows["explainer_video"].endswith(f"{PART}\n\n## 0:00\n\nNeurons wire")


def test_a_recording_that_fails_is_recorded_and_the_rest_kept(test_db, monkeypatch) -> None:
    def run(cls, batch):
        return {
            j.sha256: "ValueError: bad audio" if b"bad" in j.path.read_bytes() else _raw("Fine.", 1)
            for j in batch
        }

    monkeypatch.setattr(extract._Transcriber, "run", classmethod(run))
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run(
            [
                _recording(store, b"good", "explainer_video", "video/mp4"),
                _recording(store, b"bad", "audio_overview", "audio/x-wav"),
            ]
        )
        counts = extract_pending(conn, test_db)
        versions = sorted(r[0] for r in conn.execute("SELECT tool_version FROM blob_text"))
    assert (counts.files, counts.failed) == (1, 1)
    assert (
        versions[0]
        == f"failed [transcribe worker {extract._worker.WORKER_VERSION}]: ValueError: bad audio"
    )
    assert versions[1] == "fake-worker 1"


def test_the_tool_for_each_file() -> None:
    assert extract.tool_for("audio", "audio/x-wav") == extract.TRANSCRIPT_TOOL
    assert extract.tool_for("video", "video/mp4") == extract.TRANSCRIPT_TOOL
    assert extract.tool_for("file", "audio/x-wav") is None
    assert extract.tool_for("image", "image/png") == extract.OCR_TOOL
    assert extract.wants_text(
        "notebooklm", {"type": "artifact", "artifact_type": "explainer_video"}
    )


def test_a_transcriber_without_ffmpeg_is_unavailable(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(extract.shutil, "which", lambda name: None)
    with pytest.raises(extract.EngineUnavailable, match="ffmpeg not found"):
        extract._Transcriber.run([extract.Job("a", tmp_path / "a.wav", 2)])


def test_the_probe_reads_the_smallest_file_read_before(test_db, monkeypatch) -> None:
    store = BlobStore(store_root(test_db))
    big = store.put_stream(io.BytesIO(b"x" * 1000))
    small = store.put_stream(io.BytesIO(b"y" * 10))
    read: list[str] = []
    monkeypatch.setattr(extract, "read_file", lambda tool, path: read.append(path.name) or ("", ""))
    with get_connection(test_db) as conn:
        for blob in (big, small):
            conn.execute("INSERT INTO blobs (sha256, size) VALUES (?, ?)", (blob.sha256, blob.size))
            conn.execute(
                "INSERT INTO blob_text (sha256, tool, tool_version, text) VALUES (?, ?, 'v', 't')",
                (blob.sha256, extract.TRANSCRIPT_TOOL),
            )
        assert extract._still_reads(conn, store, extract.TRANSCRIPT_TOOL) is True
    assert read == [store.path(small.sha256).name]


def test_a_transcriber_that_cannot_run_leaves_the_other_tools_reading(
    test_db, fake_ocr, monkeypatch
) -> None:
    def no_host(cls, batch):
        raise extract.EngineUnavailable("transcripts on computer: no route to host")

    monkeypatch.setattr(extract._Transcriber, "run", classmethod(no_host))
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run(
            [
                _recording(store, b"a recording", "audio_overview", "audio/x-wav"),
                _deck(store, [b"Slide text"]),
            ]
        )
        counts = extract_pending(conn, test_db)
        tools = [r[0] for r in conn.execute("SELECT tool FROM blob_text")]
        _, content, _ = _item(conn)
    assert counts.files == 1 and counts.engine_failed and "no route" in (counts.stopped or "")
    assert counts.pending == 1
    assert tools == [extract.OCR_TOOL]  # the recording waits, unrecorded
    assert "Slide text" in content


def _fake_host(monkeypatch, worker_exit: int = 0):
    """subprocess.run for a host: ffmpeg writes audio, the results come back with rsync,
    and the worker exits with ``worker_exit``. Returns the commands run."""
    commands: list[list[str]] = []

    def run(cmd, **kwargs):
        commands.append(cmd)
        code = 0
        if cmd[0].endswith("ffmpeg"):
            Path(cmd[-1]).write_bytes(b"wav")
        elif cmd[0] == "rsync" and cmd[-1].endswith("/out/"):  # the results come back
            out = Path(cmd[-1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "a.json").write_text(json.dumps(_raw("Hi.", 1)))
            (out / "b.error").write_text("ValueError: bad")
        elif cmd[0] == "ssh" and "systemd-run" in cmd[-1]:
            code = worker_exit
        if code and kwargs.get("check"):
            raise subprocess.CalledProcessError(code, cmd, "", "worker died")
        stdout = "/home/mt" if "HOME" in cmd[-1] else ""
        return subprocess.CompletedProcess(cmd, code, stdout, "")

    monkeypatch.setattr(extract.subprocess, "run", run)
    monkeypatch.setattr(extract.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(extract._Transcriber, "settings", {"host": "computer"})
    return commands


def test_a_batch_runs_on_the_host_at_low_priority_and_is_deleted_there(
    monkeypatch, tmp_path
) -> None:
    commands = _fake_host(monkeypatch)
    jobs = [extract.Job(s, tmp_path / s, 2) for s in ("a", "b", "c")]
    results = extract._Transcriber.run(jobs)
    assert results["a"]["version"] == "fake-worker 1" and results["b"] == "ValueError: bad"
    assert isinstance(results["c"], extract.Unfinished)  # the worker wrote nothing for it
    remote = [c[-1] for c in commands if c[0] == "ssh"]
    assert all(c[c.index("computer") - 1] == "--" for c in commands if c[0] == "ssh")
    job = [c for c in remote if c.startswith("mkdir -p ")][0].split()[-1]
    assert job.startswith("/home/mt/lestash-worker/jobs/")
    started = [c for c in remote if "systemd-run" in c][0]
    assert started.startswith("flock /home/mt/lestash-worker/.lock systemd-run")
    assert "--nice=10" in started and "CPUQuota=2400%" in started and "--workers 4" in started
    assert "OOMPolicy=continue" in started
    assert "ServerAliveInterval=30" in " ".join(next(c for c in commands if c[0] == "ssh"))
    assert "ServerAliveInterval=30" in next(c for c in commands if c[0] == "rsync")[3]
    # Nothing kept there, and no unit left running
    assert remote[-1].startswith("systemctl --user stop lestash-transcribe-")
    assert remote[-1].endswith(f"; rm -rf {job}")


def test_a_worker_that_stops_part_way_keeps_what_it_finished(monkeypatch, tmp_path) -> None:
    _fake_host(monkeypatch, worker_exit=1)
    results = extract._Transcriber.run([extract.Job(s, tmp_path / s, 2) for s in ("a", "c")])
    assert results["a"]["version"] == "fake-worker 1"
    assert results["c"] == "the worker stopped: worker died"
    assert isinstance(results["c"], extract.Unfinished)


def test_recordings_a_stopped_worker_never_reached_wait(test_db, monkeypatch) -> None:
    calls = []

    def run(cls, batch):
        calls.append(len(batch))
        if len(calls) == 1:  # the first batch is done
            return {j.sha256: _raw("Fine.", 1) for j in batch}
        first, *rest = batch  # the second stops after one
        out: dict = {first.sha256: _raw("Fine.", 1)}
        out.update({j.sha256: extract.Unfinished("the worker stopped: disk full") for j in rest})
        return out

    monkeypatch.setattr(extract._Transcriber, "run", classmethod(run))
    monkeypatch.setattr(extract._Transcriber, "batch_size", classmethod(lambda cls: 2))
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run(
            [
                _recording(
                    store, f"take {n}".encode(), "audio_overview", "audio/x-wav", name=f"take {n}"
                )
                for n in range(5)
            ]
        )
        counts = extract_pending(conn, test_db)
        failures = conn.execute("SELECT count(*) FROM blob_text WHERE text = ''").fetchone()[0]
    assert calls == [2, 2]  # no third batch once the worker stopped
    assert (counts.files, counts.failed, failures) == (3, 0, 0)
    assert counts.pending == 2 and counts.engine_failed and "disk full" in (counts.stopped or "")


def test_a_host_that_goes_away_makes_transcripts_wait(monkeypatch, tmp_path) -> None:
    _fake_host(monkeypatch, worker_exit=255)  # ssh's own exit: the connection dropped
    with pytest.raises(extract.EngineUnavailable, match="on computer: worker died"):
        extract._Transcriber.run([extract.Job("a", tmp_path / "a", 2)])
    monkeypatch.setattr(extract._Transcriber, "settings", {"host": "-oProxyCommand=x"})
    with pytest.raises(extract.EngineUnavailable, match="not a host name"):
        extract._Transcriber.run([extract.Job("a", tmp_path / "a", 2)])


def _ffmpeg(monkeypatch, works: bool):
    """ffmpeg that fails on every recording; on its own silent probe only if not ``works``."""

    def run(cmd, **kwargs):
        ok = works and "lavfi" in cmd
        return subprocess.CompletedProcess(cmd, 0 if ok else 1, "", "" if ok else "Invalid data")

    monkeypatch.setattr(extract.subprocess, "run", run)
    monkeypatch.setattr(extract.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_an_ffmpeg_failure_is_the_recordings_only_if_ffmpeg_works(monkeypatch, tmp_path) -> None:
    jobs = [extract.Job(s, tmp_path / s, 2) for s in ("a", "b")]
    _ffmpeg(monkeypatch, works=True)  # the files are bad: recorded
    results = extract._Transcriber.run(jobs)
    assert results["a"] == "ffmpeg: Invalid data"
    assert not any(isinstance(r, extract.Unfinished) for r in results.values())
    _ffmpeg(monkeypatch, works=False)  # this machine is: they wait
    assert all(isinstance(r, extract.Unfinished) for r in extract._Transcriber.run(jobs).values())


def test_bad_recordings_together_do_not_stop_the_good_ones(test_db, monkeypatch) -> None:
    _ffmpeg(monkeypatch, works=True)
    real_run = extract._Transcriber.run.__func__

    def run(cls, batch):
        bad = [j for j in batch if b"bad" in j.path.read_bytes()]
        out = real_run(cls, bad) if bad else {}
        out.update({j.sha256: _raw("Fine.", 1) for j in batch if j not in bad})
        return out

    monkeypatch.setattr(extract._Transcriber, "run", classmethod(run))
    monkeypatch.setattr(extract._Transcriber, "batch_size", classmethod(lambda cls: 2))
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run(
            [
                _recording(store, d, "audio_overview", "audio/x-wav", name=d.decode())
                for d in (b"bad 1", b"bad 2", b"good")
            ]
        )
        first = extract_pending(conn, test_db)
        second = extract_pending(conn, test_db)
    assert (first.files, first.failed, first.pending) == (1, 2, 0)
    assert (second.files, second.failed, second.pending) == (0, 0, 0)  # not tried again


def test_a_segment_without_word_times_keeps_its_text() -> None:
    segments = [[0.0, 1.0, " Hello.", _words((0, "Hello."))], [2.0, 3.0, " Hi there.", []]]
    assert extract.speaker_turns(segments, [[0.0, 1.5, 0], [1.5, 3.0, 1]]) == (
        "**Speaker 1** (0:00): Hello.\n\n**Speaker 2** (0:02): Hi there."
    )


def test_the_worker_writes_a_result_or_an_error(monkeypatch, tmp_path) -> None:
    from lestash.core import transcribe_worker as worker

    (tmp_path / "out").mkdir()
    monkeypatch.setattr(
        worker, "transcribe", lambda wav, n: {"n": n} if wav.name == "a.wav" else 1 / 0
    )
    worker._one({"sha256": "a", "speakers": 2}, str(tmp_path))
    worker._one({"sha256": "b"}, str(tmp_path))
    assert json.loads((tmp_path / "out" / "a.json").read_text()) == {"n": 2}
    assert (tmp_path / "out" / "b.error").read_text().startswith("ZeroDivisionError")


def test_the_worker_reads_only_16_khz_mono(tmp_path) -> None:
    import wave

    from lestash.core import transcribe_worker as worker

    for name, rate in (("ok.wav", 16000), ("cd.wav", 44100)):
        with wave.open(str(tmp_path / name), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(b"\x00\x40" * 4)
    assert worker.read_wav(tmp_path / "ok.wav").tolist() == [0.5] * 4
    with pytest.raises(ValueError, match="not 16 kHz"):
        worker.read_wav(tmp_path / "cd.wav")
