"""NotebookLM artifacts from a two-part Takeout, with their files in the store (#217)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from lestash.core.blobs import BlobStore, store_root
from lestash.core.database import get_connection
from lestash.core.sync import SyncStore
from lestash.importers.notebooklm_artifacts import collect, slide_number, to_items

NB = "Takeout/NotebookLM/Hebbian"
ART = f"{NB}/Artifacts"


def _zip(path: Path, files: dict[str, bytes | dict]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, json.dumps(data) if isinstance(data, dict) else data)
    return path


def _parts(tmp: Path) -> list[Path]:
    deck = {"title": "Deck", "type": "ARTIFACT_TYPE_SLIDES", "slides": {"title": "Deck"}}
    audio = {
        "title": "Solving Forgetting",
        "type": "ARTIFACT_TYPE_AUDIO_OVERVIEW",
        "audioOverview": {"generationOptions": {"episodeFocus": "Two experts"}},
    }
    report = {"title": "Brief", "type": "ARTIFACT_TYPE_TAILORED_REPORT"}
    one = _zip(
        tmp / "t-1.zip",
        {
            f"{ART}/Deck metadata.json": deck,
            f"{ART}/Deck/Deck.pdf": b"%PDF deck",
            f"{ART}/Deck/Deck_-_Slide": b"slide one",
            f"{ART}/Deck/Deck_-_Slide(1)": b"slide two",
            # Cut shorter than its file
            f"{ART}/Solving Forgettin.json": audio,
            f"{ART}/Brief.json": report,
            f"{ART}/Brief.md": b"# Brief",
        },
    )
    two = _zip(
        tmp / "t-2.zip",
        {
            f"{ART}/Deck/Deck_-_Slide(2)": b"slide three",
            f"{ART}/Solving Forgetting.wav": b"RIFF audio",
        },
    )
    return [one, two]


def test_slide_numbers_from_each_naming() -> None:
    assert slide_number("Deck_-_Slide_7", numbered_deck=True) == 7
    assert slide_number("Deck_-_Slide", numbered_deck=False) == 1
    assert slide_number("Deck_-_Slid(3)", numbered_deck=False) == 4
    assert slide_number("Deck_-(16)", numbered_deck=False) == 17
    assert slide_number("Deck.pdf", numbered_deck=False) is None


def test_artifacts_from_both_parts_become_children_with_stored_files(test_db, tmp_path) -> None:
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        notebook = conn.execute(
            "INSERT INTO items (source_type, source_id, content) VALUES ('notebooklm', ?, 'nb') "
            "RETURNING id",
            (NB,),
        ).fetchone()[0]
        conn.commit()
        artifacts, unmatched = collect(_parts(tmp_path))
        assert unmatched == []
        items, new_blobs = to_items(artifacts, store)
        assert new_blobs == 5  # pdf, three slides, audio; the report is a note already
        SyncStore(conn, "notebooklm").run(items)
        rows = conn.execute(
            "SELECT title, parent_id, json_extract(metadata, '$.artifact_type') FROM items "
            "WHERE json_extract(metadata, '$.type') = 'artifact' ORDER BY title"
        ).fetchall()
        assert [tuple(r) for r in rows] == [
            ("Deck", notebook, "slides"),
            ("Solving Forgetting", notebook, "audio_overview"),
        ]
        slides = conn.execute(
            "SELECT m.position, b.size FROM item_media m JOIN blobs b ON b.sha256 = m.sha256 "
            "JOIN items i ON i.id = m.item_id WHERE i.title = 'Deck' AND m.media_type = 'image' "
            "ORDER BY m.position"
        ).fetchall()
        assert [tuple(r) for r in slides] == [(1, 9), (2, 9), (3, 11)]
        media_before = conn.execute("SELECT count(*) FROM item_media").fetchone()[0]
        # The same export again: nothing new
        items, new_blobs = to_items(collect(_parts(tmp_path))[0], store)
        store_run = SyncStore(conn, "notebooklm")
        store_run.run(items)
        assert new_blobs == 0 and store_run.counts.added == 0
        assert conn.execute("SELECT count(*) FROM item_media").fetchone()[0] == media_before


def test_two_artifacts_of_one_title_each_get_their_own_files(test_db, tmp_path) -> None:
    store = BlobStore(store_root(test_db))
    part = _zip(
        tmp_path / "t.zip",
        {
            f"{ART}/Federated Learning metadata.json": {
                "title": "Federated Learning",
                "type": "ARTIFACT_TYPE_SLIDES",
            },
            f"{ART}/Federated Learning metadata(1).json": {
                "title": "Federated Learning",
                "type": "ARTIFACT_TYPE_EXPLAINER_VIDEO",
            },
            f"{ART}/Federated Learning/Federated_Learning_-_Slide_1": b"same",
            f"{ART}/Federated Learning/Federated_Learning_-_Slide_2": b"same",
            f"{ART}/Federated Learning.mp4": b"video",
            # A description name cut short through " metadata"
            f"{ART}/Federated Learning Brings Code To The Data met.json": {
                "title": "Federated Learning Brings Code To The Data",
                "type": "ARTIFACT_TYPE_AUDIO_OVERVIEW",
            },
            f"{ART}/Federated Learning Brings Code To The Data.wav": b"audio",
        },
    )
    artifacts, unmatched = collect([part])
    assert unmatched == []
    files = {a.description["type"]: [m.rsplit("/", 1)[-1] for _, m in a.files] for a in artifacts}
    assert files == {
        "ARTIFACT_TYPE_SLIDES": ["Federated_Learning_-_Slide_1", "Federated_Learning_-_Slide_2"],
        "ARTIFACT_TYPE_EXPLAINER_VIDEO": ["Federated Learning.mp4"],
        "ARTIFACT_TYPE_AUDIO_OVERVIEW": ["Federated Learning Brings Code To The Data.wav"],
    }
    items, _ = to_items(artifacts, store)
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run(items)
        # Two identical slides are still two slides
        positions = conn.execute(
            "SELECT position FROM item_media WHERE media_type = 'image' ORDER BY position"
        ).fetchall()
        assert [r[0] for r in positions] == [1, 2]
