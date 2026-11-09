"""GET /api/items/{id}/activities: an item's watches, searches and prompts, newest first."""

import io
import json
import zipfile
from datetime import datetime

from lestash.core.activities import record_activities
from lestash.core.database import get_connection
from lestash.models.item import ActivityCreate


def _store(test_config, source_id: str, metadata: dict, times: list[str] = ()) -> int:
    verb = "searched" if source_id.startswith("search:") else "watched"
    details = {"query": metadata["query"]} if "query" in metadata else None
    with get_connection(test_config) as conn:
        item_id = conn.execute(
            "INSERT INTO items (source_type, source_id, content, metadata) "
            "VALUES ('youtube', ?, 'x', ?) RETURNING id",
            (source_id, json.dumps(metadata)),
        ).fetchone()[0]
        activities = [
            ActivityCreate(
                verb=verb,
                occurred_at=datetime.fromisoformat(t),
                source="takeout:youtube",
                details=details,
            )
            for t in times
        ]
        record_activities(conn, item_id, activities)
        conn.commit()
    return item_id


def test_activities_are_listed_newest_first_with_a_total(client, test_config) -> None:
    times = ["2025-06-01T10:00:00+00:00", "2026-01-22T14:36:35.783+00:00", "2025-09-01T08:00:00Z"]
    item_id = _store(test_config, "history:A", {}, times)

    data = client.get(f"/api/items/{item_id}/activities?limit=2").json()

    assert data["total"] == 3
    assert [(a["verb"], a["occurred_at"], a["source"]) for a in data["activities"]] == [
        ("watched", "2026-01-22T14:36:35.783000Z", "takeout:youtube"),
        ("watched", "2025-09-01T08:00:00Z", "takeout:youtube"),
    ]


def test_a_search_activity_carries_its_query(client, test_config) -> None:
    item_id = _store(
        test_config,
        "search:markov blanket",
        {"query": "markov blanket"},
        ["2026-01-17T23:20:58.043+00:00"],
    )
    [activity] = client.get(f"/api/items/{item_id}/activities").json()["activities"]
    assert activity["details"] == {"query": "markov blanket"}


def test_an_item_without_activities_has_none_and_a_missing_item_is_404(client, test_config) -> None:
    item_id = _store(test_config, "history:B", {"channel": "x"})
    assert client.get(f"/api/items/{item_id}/activities").json() == {"activities": [], "total": 0}
    assert client.get("/api/items/999999/activities").status_code == 404


def test_an_uploaded_takeout_records_its_watches_as_activities(client, test_config) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "Takeout/YouTube and YouTube Music/history/watch-history.json",
            json.dumps(
                [
                    {
                        "header": "YouTube",
                        "title": "Watched Markov blankets explained",
                        "titleUrl": "https://www.youtube.com/watch?v=AAAAAAAAAAA",
                        "time": "2026-01-01T10:00:00Z",
                    }
                ]
            ),
        )
    for _ in range(2):  # the same export again adds nothing
        upload = {"file": ("takeout.zip", io.BytesIO(buf.getvalue()), "application/zip")}
        assert client.post("/api/import", files=upload).status_code == 200
    with get_connection(test_config) as conn:
        item_id, metadata = conn.execute(
            "SELECT id, metadata FROM items WHERE source_id = 'history:AAAAAAAAAAA'"
        ).fetchone()
    assert "watched_times" not in json.loads(metadata)
    data = client.get(f"/api/items/{item_id}/activities").json()
    assert [(a["verb"], a["occurred_at"]) for a in data["activities"]] == [
        ("watched", "2026-01-01T10:00:00Z")
    ]
