"""GET /api/items/{id}/agents: who made, voiced, published or received an item."""

import json

from lestash.core.agents import link_agents
from lestash.core.database import get_connection


def test_an_items_agents_are_listed_with_their_accounts(client, test_config) -> None:
    with get_connection(test_config) as conn:
        video = conn.execute(
            "INSERT INTO items (source_type, source_id, content, author, metadata) "
            "VALUES ('youtube', 'history:a', 'x', 'BBC', ?) RETURNING id",
            (json.dumps({"channel_id": "UC1", "channel_title": "BBC News"}),),
        ).fetchone()[0]
        link_agents(conn)
        conn.commit()
    [agent] = client.get(f"/api/items/{video}/agents").json()["agents"]
    assert (agent["role"], agent["person"]["name"], agent["person"]["kind"]) == (
        "creator",
        "BBC News",
        "organization",
    )
    assert agent["accounts"] == [
        {"platform": "youtube", "identifier": "UC1", "url": "https://www.youtube.com/channel/UC1"}
    ]
    assert client.get("/api/items/999999/agents").status_code == 404
