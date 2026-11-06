"""Zip importers (lestash.importers): Takeout products, multi-part NotebookLM, merging."""

import io
import json
import zipfile
from pathlib import Path

from lestash.core.database import get_connection, resolve_parent_id, upsert_item
from lestash.importers import merge_with_stored, parse_zip
from lestash.models.item import ItemCreate


def _zip(files: dict[str, str], path: Path | None = None) -> bytes | Path:
    buf = io.BytesIO() if path is None else path
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue() if isinstance(buf, io.BytesIO) else path


NB = "Takeout/NotebookLM/Planning"
META = {
    f"{NB}/Planning.json": json.dumps(
        {"title": "Planning", "emoji": "🗺", "metadata": {"createTime": "2026-03-01T10:00:00Z"}}
    ),
    f"{NB}/Notes/Plan A.html": "<p>first plan</p>",
}
SOURCES = {
    f"{NB}/Sources/paper metadata.json": json.dumps({"title": "HTN paper"}),
    f"{NB}/Sources/paper.html": "<p>text</p>",
}
ARTIFACTS = {
    f"{NB}/Artifacts/Deep dive.wav": "x",
    f"{NB}/Artifacts/Deep dive metadata.json": json.dumps(
        {"title": "Deep dive", "type": "ARTIFACT_TYPE_AUDIO_OVERVIEW"}
    ),
    f"{NB}/Artifacts/Briefing.json": json.dumps(
        {"title": "Briefing", "type": "ARTIFACT_TYPE_REPORT"}
    ),
    f"{NB}/Artifacts/Briefing.md": "# Briefing\nHTN planning in one page.",
}
KEEP = {
    "Takeout/Keep/note.json": json.dumps(
        {"title": "Shopping", "textContent": "milk", "createdTimestampUsec": 1_700_000_000_000_000}
    )
}
GEMINI = {
    "Takeout/Gemini/Conversation 1.txt": json.dumps(
        {
            "conversation_turns": [
                {
                    "user_turn": {
                        "prompt": "What is HTN?",
                        "turn_last_modified": "2026-01-01T09:00:00Z",
                    }
                },
                {"system_turn": {"text": [{"data": "Hierarchical task networks."}]}},
            ]
        }
    )
}
YT = "Takeout/YouTube and YouTube Music/history"


def _watch(vid: str, when: str, title: str = "Watched Markov blankets explained") -> dict:
    return {
        "header": "YouTube",
        "title": title,
        "titleUrl": f"https://www.youtube.com/watch?v={vid}",
        "time": when,
        "subtitles": [{"name": "Chan", "url": "https://www.youtube.com/c/chan"}],
    }


def _store(conn, items: list[ItemCreate]) -> None:
    """What the sync loops do for each item."""
    for item in items:
        resolve_parent_id(conn, item)
        merge_with_stored(conn, item)
        upsert_item(conn, item)


def _row(conn, source_id: str):
    row = conn.execute(
        "SELECT title, content, metadata, parent_id, created_at FROM items WHERE source_id = ?",
        (source_id,),
    ).fetchone()
    return row[0], row[1], json.loads(row[2] or "{}"), row[3], row[4]


# --- parse_zip ---------------------------------------------------------------------------


def test_every_takeout_product_in_one_zip_is_imported() -> None:
    files = {
        **KEEP,
        **GEMINI,
        **META,
        f"{YT}/watch-history.json": json.dumps([_watch("AAAAAAAAAAA", "2026-01-01T10:00:00Z")]),
    }
    items, source_type = parse_zip(_zip(files))
    assert source_type == "takeout"
    assert {i.source_type for i in items} == {"google-keep", "gemini", "notebooklm", "youtube"}


def test_parse_zip_accepts_a_path(tmp_path) -> None:
    items, source_type = parse_zip(_zip(META, tmp_path / "p.zip"))
    assert source_type == "notebooklm"
    assert items[0].title == "Planning"


def test_sources_only_notebook_is_kept_with_artifact_labels_and_reports() -> None:
    items, _ = parse_zip(_zip({**META, **SOURCES, **ARTIFACTS}))
    parent = items[0]
    assert parent.metadata["sources"] == ["HTN paper"]
    assert parent.metadata["artifacts"] == ["Briefing (report)", "Deep dive (audio overview)"]
    assert parent.metadata["has_metadata"] is True
    report = next(i for i in items if i.metadata.get("type") == "report")
    assert report.title == "Planning — Briefing"
    assert report.metadata["_parent_source_id"] == NB


def test_artifact_only_part_yields_its_reports() -> None:
    items, _ = parse_zip(_zip(ARTIFACTS))
    kinds = {i.metadata.get("type") for i in items}
    assert kinds == {"notebook", "report"}
    parent = next(i for i in items if i.metadata["type"] == "notebook")
    assert parent.metadata["has_metadata"] is False


# --- multi-part NotebookLM ---------------------------------------------------------------


def _import_parts(test_db, *parts: dict[str, str]):
    with get_connection(test_db) as conn:
        for part in parts:
            items, _ = parse_zip(_zip(part))
            _store(conn, items)
        return _row(conn, NB), conn.execute(
            "SELECT COUNT(*) FROM items WHERE source_type = 'notebooklm' AND parent_id IS NULL"
        ).fetchone()[0]


def test_later_parts_add_to_the_notebook_without_overwriting_it(test_db) -> None:
    (title, content, md, _, created), top_level = _import_parts(test_db, META, SOURCES, ARTIFACTS)
    assert top_level == 1  # reports and notes are children
    assert title == "Planning"
    assert md["emoji"] == "🗺"
    assert md["sources"] == ["HTN paper"]
    assert md["artifacts"] == ["Briefing (report)", "Deep dive (audio overview)"]
    assert md["note_count"] == 1
    assert "**Sources (1):** HTN paper" in content and "**Notes:** 1" in content
    assert created.startswith("2026-03-01")


def test_metadata_part_arriving_last_sets_title_and_date(test_db) -> None:
    (title, content, md, _, created), _ = _import_parts(test_db, SOURCES, ARTIFACTS, META)
    assert title == "Planning" and md["emoji"] == "🗺" and md["has_metadata"] is True
    assert md["sources"] == ["HTN paper"]
    assert created.startswith("2026-03-01")


def test_reimporting_the_same_parts_is_idempotent(test_db) -> None:
    once, _ = _import_parts(test_db, META, SOURCES, ARTIFACTS)
    again, top_level = _import_parts(test_db, META, SOURCES, ARTIFACTS)
    assert again[:3] == once[:3]
    assert top_level == 1


# --- YouTube history ---------------------------------------------------------------------


def _history_zip(*entries: dict) -> bytes:
    return _zip({f"{YT}/watch-history.json": json.dumps(list(entries))})


def test_reimport_merges_watch_times(test_db) -> None:
    vid = "AAAAAAAAAAA"
    with get_connection(test_db) as conn:
        first, _ = parse_zip(
            _history_zip(_watch(vid, "2024-01-01T10:00:00Z"), _watch(vid, "2025-06-01T10:00:00Z"))
        )
        _store(conn, first)
        # A later export after auto-delete dropped the 2024 watch, plus a new watch
        later, _ = parse_zip(
            _history_zip(_watch(vid, "2025-06-01T10:00:00Z"), _watch(vid, "2026-02-01T10:00:00Z"))
        )
        _store(conn, later)
        _, content, md, _, _ = _row(conn, f"history:{vid}")
    assert md["watched_count"] == 3
    assert md["first_at"].startswith("2024-01-01")
    assert md["last_at"].startswith("2026-02-01")
    assert "Watched 3 times" in content


def test_reimport_merges_search_times(test_db) -> None:
    def search(when: str) -> dict:
        return {
            "title": "Searched for markov blanket",
            "titleUrl": "https://www.youtube.com/results?search_query=markov+blanket",
            "time": when,
        }

    with get_connection(test_db) as conn:
        for when in ("2024-01-01T10:00:00Z", "2026-01-01T10:00:00Z"):
            items, _ = parse_zip(_zip({f"{YT}/search-history.json": json.dumps([search(when)])}))
            _store(conn, items)
        _, content, md, _, _ = _row(conn, "search:markov blanket")
    assert md["searched_count"] == 2
    assert "2 times" in content


def test_removed_video_keeps_the_title_we_had(test_db) -> None:
    vid = "AAAAAAAAAAA"
    with get_connection(test_db) as conn:
        _store(conn, parse_zip(_history_zip(_watch(vid, "2025-01-01T10:00:00Z")))[0])
        gone = _watch(
            vid, "2026-01-01T10:00:00Z", title=f"Watched https://www.youtube.com/watch?v={vid}"
        )
        _store(conn, parse_zip(_history_zip(gone))[0])
        title, _, md, _, _ = _row(conn, f"history:{vid}")
    assert title == "Markov blankets explained"
    assert "unavailable" not in md


def _api_history_item(vid: str) -> ItemCreate:
    """The shape the YouTube API sync writes for history:{id} (video_to_item)."""
    return ItemCreate(
        source_type="youtube",
        source_id=f"history:{vid}",
        url=f"https://www.youtube.com/watch?v={vid}",
        title="Markov blankets explained",
        content="Full description from the API",
        author="Chan",
        metadata={"video_id": vid, "channel_title": "Chan", "duration_seconds": 600},
    )


def test_takeout_over_api_item_keeps_the_api_fields(test_db) -> None:
    vid = "AAAAAAAAAAA"
    with get_connection(test_db) as conn:
        _store(conn, [_api_history_item(vid)])
        _store(conn, parse_zip(_history_zip(_watch(vid, "2025-01-01T10:00:00Z")))[0])
        _, content, md, _, _ = _row(conn, f"history:{vid}")
    assert md["duration_seconds"] == 600
    assert md["description"] == "Full description from the API"
    assert md["watched_count"] == 1
    assert "Full description from the API" in content and "Watched once" in content


def test_api_over_takeout_item_keeps_the_watch_times(test_db) -> None:
    vid = "AAAAAAAAAAA"
    with get_connection(test_db) as conn:
        _store(conn, parse_zip(_history_zip(_watch(vid, "2025-01-01T10:00:00Z")))[0])
        _store(conn, [_api_history_item(vid)])
        _, content, md, _, _ = _row(conn, f"history:{vid}")
    assert md["watched_count"] == 1 and md["source"] == "takeout"
    assert md["duration_seconds"] == 600
    assert "Full description from the API" in content and "Watched once" in content


def test_sources_sharing_a_title_survive_a_reimport(test_db) -> None:
    twin = {
        **META,
        f"{NB}/Sources/a metadata.json": json.dumps({"title": "Texte"}),
        f"{NB}/Sources/b metadata.json": json.dumps({"title": "Texte"}),
    }
    (_, _, once, _, _), _ = _import_parts(test_db, twin)
    (_, _, again, _, _), _ = _import_parts(test_db, twin)
    assert once["sources"] == again["sources"] == ["Texte", "Texte"]
    assert again["source_count"] == 2


def test_reimporting_a_removed_video_is_idempotent(test_db) -> None:
    vid = "AAAAAAAAAAA"
    gone = _watch(
        vid, "2026-01-01T10:00:00Z", title=f"Watched https://www.youtube.com/watch?v={vid}"
    )
    with get_connection(test_db) as conn:
        for _ in range(2):
            _store(conn, parse_zip(_history_zip(gone))[0])
        title, _, md, _, _ = _row(conn, f"history:{vid}")
        history = conn.execute("SELECT COUNT(*) FROM item_history").fetchone()[0]
    assert title == "(video no longer available)" and md["unavailable"] is True
    assert history == 0
