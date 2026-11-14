"""Collection API: reading order (#317) and the Kobo switch (#315)."""


def _new(client, name="0 Introducing ontology") -> int:
    r = client.post("/api/collections", json={"name": name})
    assert r.status_code == 201
    return r.json()["id"]


def _item_ids(client) -> list[int]:
    return [i["id"] for i in client.get("/api/items", params={"limit": 3}).json()["items"]]


def _order(client, cid: int) -> list[int]:
    return [i["id"] for i in client.get(f"/api/collections/{cid}").json()["items"]]


def test_items_list_in_the_order_added_and_can_be_moved(client) -> None:
    cid = _new(client)
    a, b, c = _item_ids(client)
    for i in (a, b, c):
        assert client.post(f"/api/collections/{cid}/items", json={"item_id": i}).status_code == 201
    assert _order(client, cid) == [a, b, c]
    r = client.put(f"/api/collections/{cid}/items/{c}", json={"position": 1})
    assert r.status_code == 200
    assert _order(client, cid) == [c, a, b]
    assert client.put(f"/api/collections/{cid}/items/{c}", json={"position": 0}).status_code == 422
    missing = client.put(f"/api/collections/{cid}/items/999999", json={"position": 1})
    assert missing.status_code == 404


def test_kobo_switch_and_status(client) -> None:
    cid = _new(client)
    (a, *_) = _item_ids(client)
    client.post(f"/api/collections/{cid}/items", json={"item_id": a})
    assert client.get(f"/api/collections/{cid}").json()["kobo"] is None
    r = client.put(f"/api/collections/{cid}/kobo", json={"shown": True})
    assert r.status_code == 200
    kobo = r.json()["kobo"]
    assert kobo["shown"] and kobo["missing"] == 1  # not a Kobo book
    assert kobo["status"] == "0 on the Kobo · 1 not on the Kobo"
    listed = {c["id"]: c for c in client.get("/api/collections").json()}
    assert listed[cid]["kobo"]["shown"]
    off = client.put(f"/api/collections/{cid}/kobo", json={"shown": False}).json()["kobo"]
    assert not off["shown"]
    assert client.put("/api/collections/999999/kobo", json={"shown": True}).status_code == 404
