"""GET /api/items/{id}/activities: an item's watches, searches and prompts, newest first."""

import json

from lestash.core.database import get_connection


def _store(test_config, source_id: str, metadata: dict) -> int:
    with get_connection(test_config) as conn:
        item_id = conn.execute(
            "INSERT INTO items (source_type, source_id, content, metadata) "
            "VALUES ('youtube', ?, 'x', ?) RETURNING id",
            (source_id, json.dumps(metadata)),
        ).fetchone()[0]
        conn.commit()
    return item_id


def test_activities_are_listed_newest_first_with_a_total(client, test_config) -> None:
    times = ["2025-06-01T10:00:00+00:00", "2026-01-22T14:36:35.783+00:00", "2025-09-01T08:00:00Z"]
    item_id = _store(test_config, "history:A", {"watched_times": times})

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
        {"query": "markov blanket", "searched_times": ["2026-01-17T23:20:58.043+00:00"]},
    )
    [activity] = client.get(f"/api/items/{item_id}/activities").json()["activities"]
    assert activity["details"] == {"query": "markov blanket"}


def test_an_item_without_activities_has_none_and_a_missing_item_is_404(client, test_config) -> None:
    item_id = _store(test_config, "history:B", {"channel": "x"})
    assert client.get(f"/api/items/{item_id}/activities").json() == {"activities": [], "total": 0}
    assert client.get("/api/items/999999/activities").status_code == 404
