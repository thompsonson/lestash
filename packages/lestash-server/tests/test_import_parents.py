"""POST /api/import links children to parents already in the DB, and keeps those links."""

import io
import json
import zipfile

from lestash.core.database import get_connection, upsert_item
from lestash.models.item import ItemCreate

VID = "AAAAAAAAAAA"


def _history_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "Takeout/YouTube and YouTube Music/history/watch-history.json",
            json.dumps(
                [
                    {
                        "header": "YouTube",
                        "title": "Watched Markov blankets explained",
                        "titleUrl": f"https://www.youtube.com/watch?v={VID}",
                        "time": "2026-01-01T10:00:00Z",
                    }
                ]
            ),
        )
    return buf.getvalue()


def _post(client) -> None:
    resp = client.post(
        "/api/import",
        files={"file": ("takeout.zip", io.BytesIO(_history_zip()), "application/zip")},
    )
    assert resp.status_code == 200, resp.text


def _parent(test_config) -> int | None:
    with get_connection(test_config) as conn:
        return conn.execute(
            "SELECT parent_id FROM items WHERE source_id = ?", (f"history:{VID}",)
        ).fetchone()[0]


def test_watched_video_nests_under_its_liked_video(client, test_config) -> None:
    with get_connection(test_config) as conn:
        liked_id = upsert_item(
            conn, ItemCreate(source_type="youtube", source_id=f"liked:{VID}", content="liked")
        )
    _post(client)
    assert _parent(test_config) == liked_id
    _post(client)  # re-import keeps the link
    assert _parent(test_config) == liked_id


def test_unresolved_parent_marker_is_kept_for_a_later_sync(client, test_config) -> None:
    _post(client)
    assert _parent(test_config) is None
    with get_connection(test_config) as conn:
        md = json.loads(
            conn.execute(
                "SELECT metadata FROM items WHERE source_id = ?", (f"history:{VID}",)
            ).fetchone()[0]
        )
    assert md["_parent_source_id"] == f"liked:{VID}"
