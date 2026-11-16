"""Versions of a document, linked by revision_of (#338)."""

from lestash.core.database import get_connection


def _ids(test_config) -> list[int]:
    with get_connection(test_config) as conn:
        return [r[0] for r in conn.execute("SELECT id FROM items ORDER BY id LIMIT 3")]


def test_link_and_list_versions(client, test_config) -> None:
    v1, v2, v3 = _ids(test_config)
    resp = client.post(f"/api/items/{v2}/versions", json={"older_id": v1})
    assert resp.status_code == 200
    assert [v["id"] for v in resp.json()["versions"]] == [v2, v1]
    client.post(f"/api/items/{v3}/versions", json={"older_id": v2})
    resp = client.get(f"/api/items/{v1}/versions")
    assert resp.status_code == 200
    versions = resp.json()["versions"]
    assert [v["id"] for v in versions] == [v3, v2, v1]
    assert versions[0]["source_type"] and "title" in versions[0]
    links = client.get(f"/api/items/{v2}/links").json()["links"]
    assert {(link["kind"], link["direction"], link["item"]["id"]) for link in links} >= {
        ("revision_of", "outgoing", v1),
        ("revision_of", "incoming", v3),
    }


def test_a_bad_version_link_is_a_400(client, test_config) -> None:
    v1, v2, _ = _ids(test_config)
    client.post(f"/api/items/{v2}/versions", json={"older_id": v1})
    resp = client.post(f"/api/items/{v1}/versions", json={"older_id": v2})
    assert resp.status_code == 400
    assert "later version" in resp.json()["detail"]
    assert client.post(f"/api/items/{v1}/versions", json={"older_id": 99999}).status_code == 400


def test_versions_of_a_missing_item_is_a_404(client) -> None:
    assert client.get("/api/items/99999/versions").status_code == 404


def test_an_item_with_no_versions_lists_itself(client, test_config) -> None:
    v1, _, _ = _ids(test_config)
    assert [v["id"] for v in client.get(f"/api/items/{v1}/versions").json()["versions"]] == [v1]
