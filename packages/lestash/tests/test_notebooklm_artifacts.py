"""NotebookLM artifacts from a two-part Takeout, with their files in the store (#217)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from lestash.core.blobs import BlobStore, store_root
from lestash.core.database import SCHEMA_VERSION, get_connection
from lestash.core.sync import SyncStore
from lestash.importers.notebooklm_artifacts import (
    Index,
    collect,
    index_zip,
    index_zips,
    slide_number,
    to_items,
)

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
        index = index_zips(_parts(tmp_path), store)
        artifacts, unmatched = collect(index)
        assert unmatched == []
        items = to_items(artifacts)
        assert index.new_blobs == 5  # pdf, three slides, audio; the report is a note already
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
        again = index_zips(_parts(tmp_path), store)
        store_run = SyncStore(conn, "notebooklm")
        store_run.run(to_items(collect(again)[0]))
        assert again.new_blobs == 0 and store_run.counts.added == 0
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
    artifacts, unmatched = collect(index_zips([part], store))
    assert unmatched == []
    files = {
        a.description["type"]: [f.member.rsplit("/", 1)[-1] for f in a.files] for a in artifacts
    }
    assert files == {
        "ARTIFACT_TYPE_SLIDES": ["Federated_Learning_-_Slide_1", "Federated_Learning_-_Slide_2"],
        "ARTIFACT_TYPE_EXPLAINER_VIDEO": ["Federated Learning.mp4"],
        "ARTIFACT_TYPE_AUDIO_OVERVIEW": ["Federated Learning Brings Code To The Data.wav"],
    }
    items = to_items(artifacts)
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run(items)
        # Two identical slides are still two slides
        positions = conn.execute(
            "SELECT position FROM item_media WHERE media_type = 'image' ORDER BY position"
        ).fetchall()
        assert [r[0] for r in positions] == [1, 2]


def test_parts_read_one_at_a_time_through_a_kept_index(test_db, tmp_path) -> None:
    """The Takeout sync has one part on disk at a time: the index, kept as JSON
    between parts, holds what earlier parts had."""
    store = BlobStore(store_root(test_db))
    one, two = _parts(tmp_path)
    first = index_zip(one, store)
    one.unlink()
    kept = json.loads(json.dumps(first.to_json()))
    index = index_zip(two, store, Index.from_json(kept))
    artifacts, unmatched = collect(index)
    assert unmatched == []
    together = collect(index_zips(_parts(tmp_path), store))[0]
    assert to_items(artifacts) == to_items(together)
    # After the first part alone: the deck has its first two slides, the audio no file,
    # and each says no more than its description, so the later part changes only media
    early = {i.title: i for i in to_items(collect(first)[0])}
    assert {t: len(i.media or []) for t, i in early.items()} == {"Deck": 3, "Solving Forgetting": 0}
    full = {i.title: i for i in to_items(artifacts)}
    for title, item in early.items():
        assert (item.content, item.metadata) == (full[title].content, full[title].metadata)


def _infographic(sources: int, prompt: str = "") -> dict:
    """An infographic's description; ``sources`` and ``prompt`` tell two apart."""
    options = {"generationOptions": {"userSteeringPrompt": prompt}} if prompt else {}
    return {
        "title": "Board",
        "type": "ARTIFACT_TYPE_INFOGRAPHIC",
        "sources": [{"sourceId": {"id": f"s{n}"}} for n in range(sources)],
        "infographic": options,
    }


def _pair(path: Path, first: tuple[dict, bytes], second: tuple[dict, bytes]) -> Path:
    """Two infographics of one title, as Takeout names them: the second's description
    and file get "(1)"."""
    return _zip(
        path,
        {
            f"{ART}/Board metadata.json": first[0],
            f"{ART}/Board.png": first[1],
            f"{ART}/Board metadata(1).json": second[0],
            f"{ART}/Board.png(1)": second[1],
        },
    )


def _files_by_key(index: Index, final: bool = True) -> dict[str, list[str]]:
    """Each artifact's stored files, by its name."""
    artifacts, _ = collect(index, final=final)
    return {a.key: [f.sha256 for f in a.files] for a in artifacts}


def test_same_titled_artifacts_keep_their_own_files_whichever_takeout_numbers(
    test_db, tmp_path
) -> None:
    """#375: the "(1)" file is the "(1)" description's, and two exports that number the
    two the other way round still give each item the same file."""
    store = BlobStore(store_root(test_db))
    one, five = _infographic(1), _infographic(5, "describe it")
    first = index_zip(_pair(tmp_path / "a.zip", (five, b"five"), (one, b"one")), store)
    second = index_zip(_pair(tmp_path / "b.zip", (one, b"one"), (five, b"five")), store)
    files = _files_by_key(first)
    assert files == _files_by_key(second)
    assert sorted(files) == ["Board metadata", "Board metadata(1)"]
    assert [len(f) for f in files.values()] == [1, 1]
    artifacts = {a.key: a for a in collect(first)[0]}
    assert len(artifacts["Board metadata"].description["sources"]) == 1
    # Each file is its artifact's image, the "(1)" one too
    items = {str(i.source_id).rsplit("/", 1)[-1]: i for i in to_items(collect(first)[0])}
    media = {k: [(m.media_type, m.alt_text) for m in i.media or []] for k, i in items.items()}
    assert media == {
        "Board metadata": [("image", "Board.png")],
        "Board metadata(1)": [("image", "Board.png")],
    }
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run(to_items(collect(first)[0]))
        SyncStore(conn, "notebooklm").run(to_items(collect(second)[0]))
        shas = conn.execute(
            "SELECT i.source_id, count(DISTINCT m.sha256) FROM items i "
            "JOIN item_media m ON m.item_id = i.id GROUP BY i.id"
        ).fetchall()
    assert [r[1] for r in shas] == [1, 1]  # neither got the other's image


def test_identical_same_titled_artifacts_are_told_apart_by_their_files(test_db, tmp_path) -> None:
    store = BlobStore(store_root(test_db))
    same = _infographic(2)
    first = index_zip(_pair(tmp_path / "a.zip", (same, b"red"), (same, b"blue")), store)
    second = index_zip(_pair(tmp_path / "b.zip", (same, b"blue"), (same, b"red")), store)
    assert _files_by_key(first) == _files_by_key(second)


def test_a_same_titled_artifacts_files_wait_until_its_others_are_read(test_db, tmp_path) -> None:
    """Which name each of two gets depends on both descriptions: before an export's last
    part, one whose other is not read yet has no item, and its files wait."""
    store = BlobStore(store_root(test_db))
    part = _zip(
        tmp_path / "a.zip",
        {f"{ART}/Board metadata(1).json": _infographic(1), f"{ART}/Board.png(1)": b"one"},
    )
    artifacts, unmatched = collect(index_zip(part, store), final=False)
    assert artifacts == []
    assert unmatched == [f"{ART}/Board.png(1)"]


def test_a_copys_file_waits_for_its_own_description(test_db, tmp_path) -> None:
    """Review round 1: "Board.png(1)" read with only "Board metadata.json" is not the
    first one's; and the first one may be renamed once the other is read, so it waits
    too. Once the export is read, each has its own."""
    store = BlobStore(store_root(test_db))
    first = _zip(
        tmp_path / "a.zip",
        {
            f"{ART}/Board metadata.json": _infographic(5, "describe it"),
            f"{ART}/Board.png": b"five",
            f"{ART}/Board.png(1)": b"one",
        },
    )
    index = index_zip(first, store)
    artifacts, unmatched = collect(index, final=False)
    assert artifacts == []
    assert unmatched == [f"{ART}/Board.png", f"{ART}/Board.png(1)"]
    second = _zip(tmp_path / "b.zip", {f"{ART}/Board metadata(1).json": _infographic(1)})
    files = _files_by_key(index_zip(second, store, index))
    sha = {f.rsplit("/", 1)[-1]: s for f, (s, _) in index.files.items()}
    # The one-source infographic is named first, whichever Takeout numbered "(1)"
    assert files == {
        "Board metadata": [sha["Board.png(1)"]],
        "Board metadata(1)": [sha["Board.png"]],
    }


def test_a_title_ending_in_a_number_is_not_a_copy(test_db, tmp_path) -> None:
    store = BlobStore(store_root(test_db))
    deck = {"title": "Plan (2)", "type": "ARTIFACT_TYPE_SLIDES"}
    part = _zip(
        tmp_path / "a.zip",
        {f"{ART}/Plan (2) metadata.json": deck, f"{ART}/Plan (2)/Plan_2.pdf": b"%PDF"},
    )
    artifacts, unmatched = collect(index_zip(part, store), final=True)
    assert unmatched == []
    assert [f.member.rsplit("/", 1)[-1] for f in artifacts[0].files] == ["Plan_2.pdf"]


def _app_export(store: BlobStore, path: Path, page: bytes) -> list:
    app = {"title": "Cards", "type": "ARTIFACT_TYPE_APP"}
    part = _zip(path, {f"{ART}/Cards metadata.json": app, f"{ART}/Cards": page})
    return collect(index_zip(part, store))[0]


def test_an_app_changed_between_exports_keeps_one_current_page(test_db, tmp_path) -> None:
    """#376: each export's page is a version dated by the export's request time; the
    newest export's is current, whichever is read last."""
    from lestash.importers.notebooklm_artifacts import Export, export_time

    store = BlobStore(store_root(test_db))
    march = Export("2026-03-23 22:17:13", _app_export(store, tmp_path / "m.zip", b"<p>march</p>"))
    november = Export(
        "2026-11-06 10:09:07", _app_export(store, tmp_path / "n.zip", b"<p>november</p>")
    )
    assert export_time("takeout-20261106T100907Z-1-002.zip") == november.seen_at
    with get_connection(test_db) as conn:
        # The newer export first, then the older
        for export, other in ((november, march), (march, november)):
            SyncStore(conn, "notebooklm").run(to_items(export.artifacts, export.seen_at, [other]))
        rows = conn.execute(
            "SELECT version, created_at, superseded_at, size FROM item_media "
            "JOIN blobs USING (sha256) ORDER BY created_at"
        ).fetchall()
        assert [tuple(r) for r in rows] == [
            # Both from the first read: the other export's index gives the older page
            (1, "2026-03-23 22:17:13", "2026-11-06 10:09:07", len(b"<p>march</p>")),
            (2, "2026-11-06 10:09:07", None, len(b"<p>november</p>")),
        ]
        before = conn.total_changes
        SyncStore(conn, "notebooklm").run(to_items(march.artifacts, march.seen_at, [november]))
        assert conn.total_changes == before  # read again, nothing changes


def test_a_file_artifacts_markdown_is_its_file_and_a_reports_is_not(test_db, tmp_path) -> None:
    """#377: an uploaded "notes.md" is kept as "notes.md.md"; a report's markdown is a
    note already, so it is neither stored nor attached."""
    store = BlobStore(store_root(test_db))
    upload = {
        "title": "notes.md",
        "type": "ARTIFACT_TYPE_FILE",
        "file": {"fileName": "notes.md", "mimeType": "text/markdown"},
    }
    report = {"title": "Brief", "type": "ARTIFACT_TYPE_TAILORED_REPORT"}
    part = _zip(
        tmp_path / "a.zip",
        {
            f"{ART}/notes.md metadata.json": upload,
            f"{ART}/notes.md.md": b"# my notes",
            f"{ART}/Brief metadata.json": report,
            f"{ART}/Brief.md": b"# Brief",
        },
    )
    index = index_zip(part, store)
    assert index.new_blobs == 1
    artifacts, unmatched = collect(index)
    assert unmatched == []
    (item,) = to_items(artifacts)
    assert [(m.media_type, m.mime_type, m.alt_text) for m in item.media or []] == [
        ("file", "text/markdown", "notes.md")
    ]


def test_a_markdown_file_whose_description_is_in_another_part_is_kept(test_db, tmp_path) -> None:
    store = BlobStore(store_root(test_db))
    part = _zip(tmp_path / "a.zip", {f"{ART}/notes.md.md": b"# my notes"})
    assert list(index_zip(part, store).files) == [f"{ART}/notes.md.md"]
    report = {f"{ART}/notes.md.json": {"title": "notes", "type": "ARTIFACT_TYPE_TAILORED_REPORT"}}
    # Known from an earlier part to be a report's
    assert index_zip(part, store, known=report).files == {}


def test_migration_37_refiles_files_from_the_kept_indexes(test_db, tmp_path) -> None:
    """Rows written before #375 and #376: the first of two same-titled infographics holds
    both images, the second as a "file"; the second has none; an app holds two current
    pages. Migration 37 puts them right from the Takeout sync's kept indexes."""
    from lestash.core.database import add_item_media, apply_migrations, save_checkpoint

    store = BlobStore(store_root(test_db))
    one, five = _infographic(1), _infographic(5, "describe it")
    app = {"title": "Cards", "type": "ARTIFACT_TYPE_APP"}
    exports: dict[str, dict[str, bytes | dict]] = {
        "notebooklm:takeout-20261113T024920Z": {
            f"{ART}/Cards metadata.json": app,
            f"{ART}/Cards": b"old page",
        },
        "notebooklm:takeout-20261113T050346Z": {
            f"{ART}/Cards metadata.json": app,
            f"{ART}/Cards": b"new page",
        },
    }
    with get_connection(test_db) as conn:
        for n, (key, app_files) in enumerate(exports.items()):
            pair = _pair(tmp_path / f"{n}.zip", (five, b"five"), (one, b"one"))
            app_part = _zip(tmp_path / f"{n}-app.zip", app_files)
            index = index_zip(app_part, store, index_zip(pair, store))
            save_checkpoint(conn, "takeout", key, {**index.to_json(), "final": True})
            if n == 0:
                old_page = index.files[f"{ART}/Cards"][0]
        sha = {f.rsplit("/", 1)[-1]: s for f, (s, _) in index.files.items()}
        ids = {}
        for key in ("Board metadata", "Board metadata(1)", "Cards metadata"):
            ids[key] = conn.execute(
                "INSERT INTO items (source_type, source_id, content, metadata) "
                "VALUES ('notebooklm', ?, 'x', '{\"type\": \"artifact\"}') RETURNING id",
                (f"{ART}/{key}",),
            ).fetchone()[0]

        def put(item: str, media_type: str, sha256: str) -> None:
            add_item_media(
                conn,
                ids[item],
                media_type=media_type,
                source_origin="takeout",
                sha256=sha256,
                size=1,
                _commit=False,
            )

        # As imported before the fix: both images on the first, "(1)" typed a file
        put("Board metadata", "image", sha["Board.png"])
        put("Board metadata", "file", sha["Board.png(1)"])
        put("Cards metadata", "file", old_page)
        put("Cards metadata", "file", sha["Cards"])
        conn.execute("PRAGMA user_version = 36")
        conn.commit()
        assert apply_migrations(conn) == SCHEMA_VERSION - 36

        def files(item: str) -> list[tuple]:
            return [
                tuple(r)
                for r in conn.execute(
                    "SELECT media_type, sha256, superseded_at IS NULL FROM item_media "
                    "WHERE item_id = ? ORDER BY version, id",
                    (ids[item],),
                )
            ]

        board = {k: files(k) for k in ("Board metadata", "Board metadata(1)")}
        # The one-source infographic is "Board metadata" in both exports
        assert board == {
            "Board metadata": [("image", sha["Board.png(1)"], 1)],
            "Board metadata(1)": [("image", sha["Board.png"], 1)],
        }
        assert files("Cards metadata") == [("file", old_page, 0), ("file", sha["Cards"], 1)]
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_merging_one_requests_two_kept_indexes() -> None:
    """Review round 1: syncs before #378 kept one request's parts as two exports."""
    from lestash.importers.notebooklm_artifacts import merge_split_exports, stored_exports

    early: dict[str, Any] = {
        "descriptions": {f"{ART}/Cards metadata.json": {"title": "Cards"}},
        "files": {},
    }
    late: dict[str, Any] = {"descriptions": {}, "files": {f"{ART}/Cards": ["a" * 64, 4]}}
    late["final"] = True
    checkpoints = {
        "notebooklm:takeout-20261106T100906Z": {**early, "final": True},
        "notebooklm:takeout-20261106T100907Z": late,
        "notebooklm:takeout-20261113T024920Z": late,  # another request
    }
    merged = merge_split_exports(checkpoints)
    assert list(merged) == ["notebooklm:takeout-20261106T100907Z"]
    value, replaced = merged["notebooklm:takeout-20261106T100907Z"]
    assert replaced == ["notebooklm:takeout-20261106T100906Z"]
    assert value["final"] is False  # completed again by the sync
    assert set(value["descriptions"]) == set(early["descriptions"])
    assert set(value["files"]) == set(late["files"])
    exports = stored_exports(checkpoints)
    assert [e.seen_at for e in exports] == ["2026-11-06 10:09:07", "2026-11-13 02:49:20"]
    assert [len(a.files) for a in exports[0].artifacts] == [1]


def test_migration_37_keeps_each_item_standing_for_its_artifact(test_db, tmp_path) -> None:
    """Review rounds 1 and 3: an item made before #375 under a name the description
    order would give the other artifact keeps that name, and its tags, with its own
    artifact's files."""
    from lestash.core.database import add_tag, apply_migrations, save_checkpoint

    store = BlobStore(store_root(test_db))
    one, five = _infographic(1), _infographic(5, "describe it")
    index = index_zip(_pair(tmp_path / "a.zip", (five, b"five"), (one, b"one")), store)
    sha = {f.rsplit("/", 1)[-1]: s for f, (s, _) in index.files.items()}
    with get_connection(test_db) as conn:
        save_checkpoint(
            conn,
            "takeout",
            "notebooklm:takeout-20261113T024920Z",
            {**index.to_json(), "final": True},
        )
        # As the export named them before: the five-source one first
        ids = {}
        for key, description in (("Board metadata", five), ("Board metadata(1)", one)):
            metadata = {
                "type": "artifact",
                "artifact_type": "infographic",
                "notebook_sources": [s["sourceId"]["id"] for s in description["sources"]],
                "options": description["infographic"],
            }
            ids[key] = conn.execute(
                "INSERT INTO items (source_type, source_id, content, metadata) "
                "VALUES ('notebooklm', ?, 'x', ?) RETURNING id",
                (f"{ART}/{key}", json.dumps(metadata)),
            ).fetchone()[0]
        add_tag(conn, ids["Board metadata"], "five")
        conn.execute("PRAGMA user_version = 36")
        conn.commit()
        apply_migrations(conn)
        rows = conn.execute(
            "SELECT i.source_id, m.sha256, group_concat(t.name) FROM items i "
            "JOIN item_media m ON m.item_id = i.id "
            "LEFT JOIN item_tags it ON it.item_id = i.id LEFT JOIN tags t ON t.id = it.tag_id "
            "WHERE i.id = ? GROUP BY i.id",
            (ids["Board metadata"],),
        ).fetchone()
    # The five-source item keeps its name, its tag and its own image
    assert tuple(rows) == (f"{ART}/Board metadata", sha["Board.png"], "five")


def test_nothing_is_written_before_an_exports_last_part(test_db, tmp_path) -> None:
    """Review round 2: a part can't tell whether a later one holds another artifact of
    a title, which would change the names; so before the last part, no artifact has an
    item and every file waits, even a lone one with nothing "(n)" in sight."""
    store = BlobStore(store_root(test_db))
    part = _zip(
        tmp_path / "a.zip",
        {f"{ART}/Board metadata.json": _infographic(5, "describe it"), f"{ART}/Board.png": b"5"},
    )
    assert collect(index_zip(part, store), final=False) == ([], [f"{ART}/Board.png"])


def test_same_titled_artifacts_keep_the_items_they_made(test_db, tmp_path) -> None:
    """Review round 3: an export names same-titled artifacts after the stored items
    whose sources and options they have, the order of their descriptions only for one
    no item matches; so a later export can't make an item stand for the other one."""
    from lestash.importers.notebooklm_artifacts import stored_identities

    store = BlobStore(store_root(test_db))
    one, five = _infographic(1), _infographic(5, "describe it")
    index = index_zip(_pair(tmp_path / "a.zip", (five, b"five"), (one, b"one")), store)
    sha = {f.rsplit("/", 1)[-1]: s for f, (s, _) in index.files.items()}
    # By description order alone, the one-source infographic is named first
    assert _files_by_key(index)["Board metadata"] == [sha["Board.png(1)"]]
    with get_connection(test_db) as conn:
        # An item the five-source one made under the first name
        (made,) = [
            i
            for i in to_items(collect(index)[0])
            if len((i.metadata or {})["notebook_sources"]) == 5
        ]
        item = made.model_copy(update={"source_id": f"{ART}/Board metadata", "media": []})
        SyncStore(conn, "notebooklm").run([item])
        identities = stored_identities(conn)
    assert list(identities) == [f"{ART}/Board metadata"]
    artifacts, _ = collect(index, stored=identities)
    files = {a.key: [f.sha256 for f in a.files] for a in artifacts}
    assert files == {
        "Board metadata": [sha["Board.png"]],  # the five-source one, as stored
        "Board metadata(1)": [sha["Board.png(1)"]],
    }


def _store_item(conn, key: str, description: dict) -> None:
    """An artifact item as an earlier import stored it under ``key``."""
    metadata = {
        "type": "artifact",
        "artifact_type": "infographic",
        "notebook_sources": [s["sourceId"]["id"] for s in description.get("sources", [])],
        "options": description.get("infographic"),
    }
    conn.execute(
        "INSERT INTO items (source_type, source_id, content, metadata) "
        "VALUES ('notebooklm', ?, 'x', ?)",
        (f"{ART}/{key}", json.dumps(metadata)),
    )


def test_an_artifact_keeps_its_item_when_fewer_share_its_title(test_db, tmp_path) -> None:
    """Review round 4: two "Board"s were stored; the next export has only the second,
    which Takeout now calls "Board metadata". It keeps its own item, "(1)"; one no item
    matches keeps its own name."""
    from lestash.importers.notebooklm_artifacts import stored_identities

    store = BlobStore(store_root(test_db))
    one, five = _infographic(1), _infographic(5, "describe it")
    with get_connection(test_db) as conn:
        _store_item(conn, "Board metadata", one)
        _store_item(conn, "Board metadata(1)", five)
        conn.commit()
        identities = stored_identities(conn)
    part = _zip(tmp_path / "a.zip", {f"{ART}/Board metadata.json": five, f"{ART}/Board.png": b"5"})
    artifacts, _ = collect(index_zip(part, store), stored=identities)
    assert [a.key for a in artifacts] == ["Board metadata(1)"]
    other = _infographic(2, "something new")
    part = _zip(tmp_path / "b.zip", {f"{ART}/Board metadata.json": other, f"{ART}/Board.png": b"2"})
    artifacts, _ = collect(index_zip(part, store), stored=identities)
    assert [a.key for a in artifacts] == ["Board metadata"]


def test_stored_options_that_are_a_string_still_match(test_db, tmp_path) -> None:
    """Review round 4: json_extract gave a JSON string back bare, so it did not match."""
    from lestash.importers.notebooklm_artifacts import stored_identities

    store = BlobStore(store_root(test_db))
    one = {**_infographic(1), "infographic": "plain"}
    five = {**_infographic(5), "infographic": "[1, 2]"}  # a string that parses as JSON
    with get_connection(test_db) as conn:
        _store_item(conn, "Board metadata(1)", one)
        _store_item(conn, "Board metadata", five)
        conn.commit()
        identities = stored_identities(conn)
    part = _pair(tmp_path / "a.zip", (one, b"one"), (five, b"five"))
    names = {
        a.key: len(a.description["sources"])
        for a in collect(index_zip(part, store), stored=identities)[0]
    }
    assert names == {"Board metadata(1)": 1, "Board metadata": 5}


def test_a_titles_artifacts_of_two_types_never_share_a_name(test_db, tmp_path) -> None:
    """Review round 5: Takeout's names are shared by a title's artifacts of every type.
    The audio's stored item is "Talk metadata", the name Takeout now gives the deck:
    the audio keeps its item, and the deck gets a name no other artifact or item has.
    A stored item of another type is never taken over, matched or not."""
    from lestash.importers.notebooklm_artifacts import StoredItem, _artifact_identity

    store = BlobStore(store_root(test_db))
    deck = {"title": "Talk", "type": "ARTIFACT_TYPE_SLIDES", "slides": {"x": 1}}
    audio = {"title": "Talk", "type": "ARTIFACT_TYPE_AUDIO_OVERVIEW", "audioOverview": {"y": 2}}
    part = _zip(
        tmp_path / "a.zip",
        {f"{ART}/Talk metadata.json": deck, f"{ART}/Talk metadata(1).json": audio},
    )
    index = index_zip(part, store)
    plain = {a.kind: a for a in collect(index)[0]}
    stored = {
        f"{ART}/Talk metadata": StoredItem(
            _artifact_identity(plain["audio_overview"]), "audio_overview"
        )
    }
    names = {a.kind: a.key for a in collect(index, stored=stored)[0]}
    assert names["audio_overview"] == "Talk metadata"
    assert names["slides"] not in ("Talk metadata", "Talk metadata(1)")
    # The audio changed since: its item is not the deck's either
    changed = {f"{ART}/Talk metadata": StoredItem("something else", "audio_overview")}
    names = {a.kind: a.key for a in collect(index, stored=changed)[0]}
    assert names["slides"] != "Talk metadata"
    assert names["audio_overview"] == "Talk metadata(1)"  # its own name, by Takeout
    assert len(set(names.values())) == 2


def test_a_files_number_is_its_rank_among_its_types_descriptions(test_db, tmp_path) -> None:
    """Review round 6: Takeout numbers a title's descriptions across its types (as a
    deck and a video of one title are "metadata.json" and "metadata(1).json" in the
    kept indexes), but its files only among files of one name. A deck and two
    infographics: the infographics are "metadata(1)" and "metadata(2)", and their
    images "Plan.png" and "Plan.png(1)"."""
    store = BlobStore(store_root(test_db))
    deck = {"title": "Plan", "type": "ARTIFACT_TYPE_SLIDES"}
    first = {"title": "Plan", "type": "ARTIFACT_TYPE_INFOGRAPHIC", "sources": [{}]}
    second = {"title": "Plan", "type": "ARTIFACT_TYPE_INFOGRAPHIC", "sources": [{}, {}]}
    part = _zip(
        tmp_path / "a.zip",
        {
            f"{ART}/Plan metadata.json": deck,
            f"{ART}/Plan metadata(1).json": first,
            f"{ART}/Plan metadata(2).json": second,
            f"{ART}/Plan/Plan.pdf": b"%PDF",
            f"{ART}/Plan.png": b"first",
            f"{ART}/Plan.png(1)": b"second",
            f"{ART}/Plan.png(2)": b"no third",  # no description of that rank
        },
    )
    artifacts, unmatched = collect(index_zip(part, store))
    files = {
        (a.kind, len(a.description.get("sources", []))): [
            f.member.rsplit("/", 1)[-1] for f in a.files
        ]
        for a in artifacts
    }
    assert files == {
        ("slides", 0): ["Plan.pdf"],
        ("infographic", 1): ["Plan.png"],
        ("infographic", 2): ["Plan.png(1)"],
    }
    assert unmatched == [f"{ART}/Plan.png(2)"]


def test_a_file_of_no_known_type_is_matched_only_when_one_type_could_own_it(
    test_db, tmp_path
) -> None:
    """Review round 7: "Plan.xyz" says no type. With a deck and a
    video of the title its "(n)" can't be ranked, so it is left unmatched; with one
    type only, it is that artifact's."""
    store = BlobStore(store_root(test_db))
    deck = {"title": "Plan", "type": "ARTIFACT_TYPE_SLIDES"}
    video = {"title": "Plan", "type": "ARTIFACT_TYPE_EXPLAINER_VIDEO"}
    mixed = _zip(
        tmp_path / "a.zip",
        {
            f"{ART}/Plan metadata.json": deck,
            f"{ART}/Plan metadata(1).json": video,
            f"{ART}/Plan.xyz": b"?",
        },
    )
    artifacts, unmatched = collect(index_zip(mixed, store))
    assert [a.files for a in artifacts] == [[], []]
    assert unmatched == [f"{ART}/Plan.xyz"]
    alone = _zip(tmp_path / "b.zip", {f"{ART}/Plan metadata.json": deck, f"{ART}/Plan.xyz": b"?"})
    artifacts, unmatched = collect(index_zip(alone, store))
    assert [len(a.files) for a in artifacts] == [1] and unmatched == []


def test_requests_seconds_apart_in_a_run_are_not_one_export() -> None:
    """Review round 7: each within 2 s of the next, the first and last 4 s apart."""
    from lestash.importers.notebooklm_artifacts import request_groups

    keys = [f"notebooklm:takeout-20261106T10090{s}Z" for s in (0, 2, 4)]
    groups = request_groups(keys)
    assert groups[keys[0]] == groups[keys[1]] == keys[1]
    assert groups[keys[2]] == keys[2]


def test_a_file_never_ranks_among_another_types_descriptions(test_db, tmp_path) -> None:
    """Review round 8: "Plan.wav" with no audio overview of its title falls back to
    the title's descriptions only when they are of one type; and a top-level
    "Plan.pdf" is a deck's (or an upload's), so it finds the deck beside a video."""
    store = BlobStore(store_root(test_db))
    deck = {"title": "Plan", "type": "ARTIFACT_TYPE_SLIDES"}
    video = {"title": "Plan", "type": "ARTIFACT_TYPE_EXPLAINER_VIDEO"}
    part = _zip(
        tmp_path / "a.zip",
        {
            f"{ART}/Plan metadata.json": deck,
            f"{ART}/Plan metadata(1).json": video,
            f"{ART}/Plan.wav": b"audio",
            f"{ART}/Plan.pdf": b"%PDF",
        },
    )
    artifacts, unmatched = collect(index_zip(part, store))
    files = {a.kind: [f.member.rsplit("/", 1)[-1] for f in a.files] for a in artifacts}
    assert files == {"slides": ["Plan.pdf"], "explainer_video": []}
    assert unmatched == [f"{ART}/Plan.wav"]
    one_type = _zip(
        tmp_path / "b.zip", {f"{ART}/Plan metadata.json": video, f"{ART}/Plan.wav": b"audio"}
    )
    artifacts, unmatched = collect(index_zip(one_type, store))
    assert [len(a.files) for a in artifacts] == [1] and unmatched == []


def test_an_uploaded_file_keeps_its_file_beside_a_shorter_title(test_db, tmp_path) -> None:
    """Review round 9: an uploaded "Plan notes.png" beside a "Plan" infographic. The
    upload's image was the infographic's by its name's start; each keeps its own."""
    store = BlobStore(store_root(test_db))
    upload = {"title": "Plan notes.png", "type": "ARTIFACT_TYPE_FILE"}
    infographic = {"title": "Plan", "type": "ARTIFACT_TYPE_INFOGRAPHIC"}
    part = _zip(
        tmp_path / "a.zip",
        {
            f"{ART}/Plan notes.png metadata.json": upload,
            f"{ART}/Plan notes.png.png": b"upload",
            f"{ART}/Plan metadata.json": infographic,
            f"{ART}/Plan.png": b"generated",
        },
    )
    artifacts, unmatched = collect(index_zip(part, store))
    files = {a.kind: [f.member.rsplit("/", 1)[-1] for f in a.files] for a in artifacts}
    assert files == {"file": ["Plan notes.png.png"], "infographic": ["Plan.png"]}
    assert unmatched == []
