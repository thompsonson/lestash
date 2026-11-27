"""POST /api/import reads a Claude data export (#88), and a second upload adds no copies."""

import io
import json
import zipfile

from lestash.core.database import get_connection

CONV, PROJECT = "conv-1", "proj-1"


def _export() -> bytes:
    message = {
        "uuid": "msg-1",
        "text": "What is active inference?",
        "content": [],
        "sender": "human",
        "created_at": "2026-03-01T10:00:00Z",
        "attachments": [],
        "files": [],
    }
    files = {
        "users.json": [{"uuid": "u1"}],
        "conversations.json": [
            {
                "uuid": CONV,
                "name": "Active inference",
                "summary": "",
                "created_at": "2026-03-01T09:59:00Z",
                "chat_messages": [message, {**message, "uuid": "msg-2", "sender": "assistant"}],
            }
        ],
        "projects.json": [
            {
                "uuid": PROJECT,
                "name": "Book",
                "description": "Chapters.",
                "prompt_template": "",
                "is_private": True,
                "created_at": "2025-04-06T13:44:44Z",
                "docs": [{"uuid": "doc-1", "filename": "ch1.md", "content": "# One"}],
            }
        ],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, json.dumps(data))
    return buf.getvalue()


def _post(client) -> dict:
    resp = client.post(
        "/api/import",
        files={"file": ("data-batch-0000.zip", io.BytesIO(_export()), "application/zip")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_an_export_uploaded_twice(client, test_config) -> None:
    first = _post(client)
    assert (first["source_type"], first["items_added"], first["errors"]) == ("claude", 5, [])
    _post(client)
    with get_connection(test_config) as conn:
        rows = conn.execute(
            "SELECT i.source_type, i.source_id, p.source_id FROM items i"
            " LEFT JOIN items p ON p.id = i.parent_id"
            " WHERE i.source_type IN ('claude', 'claude-project')"
            " ORDER BY i.source_type, i.source_id"
        ).fetchall()
        imports = conn.execute(
            "SELECT json_extract(details, '$.source') FROM activities WHERE verb = 'imported'"
        ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("claude", CONV, None),
        ("claude", "msg-1", CONV),
        ("claude", "msg-2", CONV),
        ("claude-project", "doc-1", PROJECT),
        ("claude-project", PROJECT, None),
    ]
    # Each upload counts for the Claude export's reminder (#403)
    assert [r[0] for r in imports] == ["claude", "claude"]
