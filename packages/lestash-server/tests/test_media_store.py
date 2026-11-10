"""Uploaded media go into the file store and are served from it (#217)."""

import io

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


def test_a_stored_app_page_is_a_download_and_audio_plays_inline(client, test_config) -> None:
    from lestash.core.blobs import BlobStore
    from lestash.core.database import add_item_media

    store = BlobStore(store_root(test_config))
    with get_connection(test_config) as conn:
        item = conn.execute(
            "INSERT INTO items (source_type, content) VALUES ('notebooklm', 'x') RETURNING id"
        ).fetchone()[0]
        ids = {}
        for name, media_type, mime, data in (
            ("Flashcards", "file", "text/html", b"<script>alert(1)</script>"),
            ("Talk.wav", "audio", "audio/x-wav", b"RIFF"),
        ):
            blob = store.put_stream(io.BytesIO(data))
            ids[name] = add_item_media(
                conn,
                item,
                media_type=media_type,
                mime_type=mime,
                alt_text=name,
                sha256=blob.sha256,
                size=blob.size,
            )
    media = {m["alt_text"]: m for m in client.get(f"/api/items/{item}").json()["media"]}
    assert media["Flashcards"]["available"] and media["Talk.wav"]["available"]
    page_resp = client.get(f"/api/media/{ids['Flashcards']}")
    assert page_resp.headers["content-disposition"].startswith("attachment")
    audio = client.get(f"/api/media/{ids['Talk.wav']}")
    assert "attachment" not in audio.headers.get("content-disposition", "")
    assert audio.content == b"RIFF"


def test_older_versions_are_listed_and_served(client, test_config) -> None:
    from lestash.core.blobs import BlobStore
    from lestash.core.database import add_item_media

    store = BlobStore(store_root(test_config))
    with get_connection(test_config) as conn:
        item = conn.execute(
            "INSERT INTO items (source_type, content) VALUES ('kobo', 'x') RETURNING id"
        ).fetchone()[0]
        old, new = (store.put_stream(io.BytesIO(b)) for b in (b"%PDF annotated", b"%PDF clean"))
        for seen, blob, superseded in (
            ("2026-11-07 09:56:00", old, "2026-11-07 09:57:00"),
            ("2026-11-07 09:57:00", new, None),
        ):
            add_item_media(
                conn,
                item,
                media_type="pdf",
                mime_type="application/pdf",
                alt_text="Judge.pdf",
                sha256=blob.sha256,
                size=blob.size,
                seen_at=seen,
                superseded_at=superseded,
            )
    media = client.get(f"/api/items/{item}").json()["media"]
    assert [(m["version"], m["superseded_at"]) for m in media] == [
        (1, "2026-11-07 09:57:00"),
        (2, None),
    ]
    assert client.get(media[0]["serve_url"]).content == b"%PDF annotated"


def test_a_pruned_download_keeps_the_name_it_was_saved_under(client, test_config) -> None:
    from lestash.core.blobs import prune_media

    with get_connection(test_config) as conn:
        item = conn.execute(
            "INSERT INTO items (source_type, content) VALUES ('t', 'x') RETURNING id"
        ).fetchone()[0]
        conn.commit()
    resp = client.post(
        f"/api/items/{item}/media", files={"file": ("notes.txt", b"plain", "text/plain")}
    )
    media_id = resp.json()["id"]
    with get_connection(test_config) as conn:
        assert prune_media(conn, test_config).removed == 1
        (local_path,) = conn.execute(
            "SELECT local_path FROM item_media WHERE id = ?", (media_id,)
        ).fetchone()
    served = client.get(f"/api/media/{media_id}")
    assert served.status_code == 200 and served.content == b"plain"
    disposition = served.headers["content-disposition"]
    assert disposition.startswith("attachment") and local_path.rsplit("/", 1)[1] in disposition
