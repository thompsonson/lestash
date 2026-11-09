"""Uploaded media go into the file store and are served from it (#217)."""

from lestash.core.blobs import store_root
from lestash.core.database import get_connection, get_media_dir


def test_an_upload_is_stored_and_served_from_the_store(client, test_config) -> None:
    with get_connection(test_config) as conn:
        item = conn.execute(
            "INSERT INTO items (source_type, content) VALUES ('t', 'x') RETURNING id"
        ).fetchone()[0]
        conn.commit()
    resp = client.post(
        f"/api/items/{item}/media", files={"file": ("a.png", b"\x89PNG data", "image/png")}
    )
    assert resp.status_code == 201
    media_id = resp.json()["id"]
    with get_connection(test_config) as conn:
        local_path, sha256 = conn.execute(
            "SELECT local_path, sha256 FROM item_media WHERE id = ?", (media_id,)
        ).fetchone()
    assert sha256 and (store_root(test_config) / "blobs" / sha256[:2] / sha256).is_file()
    # Served from the store once the media folder's copy is gone
    (get_media_dir(test_config) / local_path).unlink()
    served = client.get(f"/api/media/{media_id}")
    assert served.status_code == 200 and served.content == b"\x89PNG data"
    assert served.headers["content-type"] == "image/png"
