"""Import reminders through the API, and the import they log (#403)."""

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta

from lestash.core.database import get_connection


def _takeout_zip() -> bytes:
    """A Takeout holding an empty YouTube watch history: read, but nothing new."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Takeout/YouTube and YouTube Music/history/watch-history.json", "[]")
    return buf.getvalue()


def _imports(client) -> dict[str, dict]:
    resp = client.get("/api/sources/imports")
    assert resp.status_code == 200, resp.text
    return {i["key"]: i for i in resp.json()["imports"]}


def test_every_manual_import_is_listed(client) -> None:
    imports = _imports(client)
    assert {"google-takeout", "mistral", "linkedin-export", "claude"} <= set(imports)
    claude = imports["claude"]
    assert claude["how"] is None  # LeStash can't read it yet
    assert claude["aim_days"] == claude["default_aim_days"] == 30


def test_an_uploaded_takeout_is_an_import_even_adding_nothing(client, test_config) -> None:
    resp = client.post(
        "/api/import",
        files={"file": ("takeout-1.zip", io.BytesIO(_takeout_zip()), "application/zip")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["items_added"] == 0
    takeout = _imports(client)["google-takeout"]
    assert (takeout["days"], takeout["due"]) == (0, False)
    with get_connection(test_config) as conn:
        details = json.loads(
            conn.execute("SELECT details FROM activities WHERE verb = 'imported'").fetchone()[0]
        )
    assert details == {
        "source": "google-takeout",
        "label": "Google Takeout",
        "items_added": 0,
        "name": "takeout-1.zip",
    }


def test_a_json_upload_is_no_manual_import(client, test_config) -> None:
    data = json.dumps([{"source_type": "note", "content": "bulk"}]).encode()
    client.post("/api/import", files={"file": ("b.json", io.BytesIO(data), "application/json")})
    with get_connection(test_config) as conn:
        assert (
            conn.execute("SELECT count(*) FROM activities WHERE verb = 'imported'").fetchone()[0]
            == 0
        )


def test_aim_and_snooze(client) -> None:
    resp = client.put("/api/sources/imports/mistral", json={"aim_days": 0})
    assert resp.status_code == 200
    mistral = {i["key"]: i for i in resp.json()["imports"]}["mistral"]
    assert (mistral["aim_days"], mistral["due"]) == (0, False)

    client.put("/api/sources/imports/mistral", json={"aim_days": None})
    resp = client.post("/api/sources/imports/mistral/snooze", json={"days": 7})
    mistral = {i["key"]: i for i in resp.json()["imports"]}["mistral"]
    until = datetime.fromisoformat(mistral["snoozed_until"].replace("Z", "+00:00"))
    assert timedelta(days=6) < until - datetime.now(UTC) <= timedelta(days=7)
    assert not mistral["due"]

    resp = client.post("/api/sources/imports/mistral/snooze")  # no body: a week
    assert resp.status_code == 200


def test_bad_requests(client) -> None:
    assert client.post("/api/sources/imports/nope/snooze").status_code == 404
    assert client.put("/api/sources/imports/nope", json={"aim_days": 3}).status_code == 404
    assert client.put("/api/sources/imports/mistral", json={"aim_days": -1}).status_code == 422
    assert client.post("/api/sources/imports/mistral/snooze", json={"days": 91}).status_code == 422


def test_a_failing_sync_is_listed(client, test_config) -> None:
    with get_connection(test_config) as conn:
        conn.execute("INSERT OR REPLACE INTO sources (source_type, enabled) VALUES ('voice', 1)")
        for _ in range(3):
            conn.execute(
                "INSERT INTO sync_log (source_type, started_at, status, error_message)"
                " VALUES ('voice', '2026-06-20 10:00:00', 'failed', 'Reveal CLI not found')"
            )
        conn.commit()
    failing = client.get("/api/sources/imports").json()["failing"]
    assert failing == [
        {"source": "voice", "failed_runs": 3, "last_error": "Reveal CLI not found", "last_ok": None}
    ]


def test_an_import_shows_on_the_timeline(client) -> None:
    client.post(
        "/api/import",
        files={"file": ("takeout-1.zip", io.BytesIO(_takeout_zip()), "application/zip")},
    )
    now = datetime.now(UTC)
    resp = client.get(
        "/api/timeline",
        params={
            "start": (now - timedelta(hours=1)).isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
        },
    )
    assert resp.status_code == 200, resp.text
    imported = [e for e in resp.json()["entries"] if e["verb"] == "imported"]
    assert len(imported) == 1
    assert imported[0]["item"] is None
    assert imported[0]["details"]["source"] == "google-takeout"
