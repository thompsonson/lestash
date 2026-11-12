"""The LinkedIn export import: posts, comments and articles, without doubling what the
sync holds."""

import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from lestash.core.config import Config, GeneralConfig
from lestash.core.database import get_connection, init_database, upsert_item
from lestash.models.item import ItemCreate
from lestash_linkedin.importer import import_export, parse_article, post_urn_of

ME = "urn:li:person:me"
# Made at 2026-01-19T07:09:09.522Z
HELD_SHARE = "urn:li:share:7418912385886941184"
NEW_SHARE = "urn:li:share:7000000000000000000"
OTHER_POST = "urn:li:activity:7100000000000000000"
FEED = "https://www.linkedin.com/feed/update/"

SHARES = f'''Date,ShareLink,ShareCommentary,SharedUrl,MediaUrl,Visibility
2026-01-19 07:09:09,"{FEED}urn%3Ali%3Ashare%3A7418912385886941184","Held already",,,MEMBER_NETWORK
2024-03-30 17:02:00,"{FEED}urn%3Ali%3Ashare%3A7000000000000000000","An old post
on two lines",https://example.com,,MEMBER_NETWORK
2024-03-31 09:00:00,"{FEED}urn%3Ali%3Ashare%3A7000000000000000001",,,,MEMBER_NETWORK
'''

COMMENTS = f"""Date,Link,Message
2024-03-30 20:37:00,{FEED}urn:li:activity:7100000000000000000,"Says \\"hello\\", twice"
2024-03-30 20:37:00,{FEED}urn:li:activity:7100000000000000000,"Another in the same second"
2026-01-19 10:45:54,{FEED}urn:li:activity:7418912385886941184,"Held, with other spacing"
2026-01-19 11:00:00,{FEED}urn:li:activity:7418912385886941184,"Text the sync never gave"
"""

ARTICLE = """<html><head><title>T</title><style>p {}</style></head><body>
<h1><a href="https://www.linkedin.com/pulse/an-article-me-abc">An article</a></h1>
<p class="created">Created on 2024-02-07 20:53</p>
<p class="published">Published on 2024-02-07 21:03</p>
<div><p>First paragraph.</p><h2>Part</h2><p>Second, with a <a href="https://x.org">link</a>.</p></div>
</body></html>"""

DRAFT = """<html><body><h1>A draft</h1>
<p class="created">Created on 2021-10-21 03:23</p><p class="published">Published on ---</p>
<div><p>An outline</p></div></body></html>"""


@pytest.fixture
def config() -> Iterator[Config]:
    with tempfile.TemporaryDirectory() as tmpdir:
        config = Config(general=GeneralConfig(database_path=str(Path(tmpdir) / "t.db")))
        init_database(config)
        yield config


@pytest.fixture
def export(tmp_path: Path) -> Path:
    path = tmp_path / "Basic_LinkedInDataExport.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Shares.csv", SHARES)
        zf.writestr("Comments.csv", COMMENTS)
        zf.writestr("Reactions.csv", "Date,Type,Link\n")
        zf.writestr("Articles/Articles/an-article-me-abc.html", ARTICLE)
        zf.writestr("Articles/Articles/2021-10-21 03:23:26.0-A draft.html", DRAFT)
    return path


def _held(conn) -> None:
    """What the sync holds: one post, one comment on it, one comment with no text."""
    upsert_item(
        conn,
        ItemCreate(
            source_type="linkedin",
            source_id=f"changelog-ugcPosts-{HELD_SHARE}",
            content="Held already",
            author=ME,
            is_own_content=True,
            metadata={"source": "changelog", "resource_name": "ugcPosts", "post_id": HELD_SHARE},
        ),
    )
    for n, (text, method, captured) in enumerate(
        [
            ("Held,  with other spacing!", "CREATE", 1768819554400),
            ("CREATE socialActions/comments", "CREATE", 1768820400700),
        ]
    ):
        upsert_item(
            conn,
            ItemCreate(
                source_type="linkedin",
                source_id=f"changelog-socialActions/comments-{n}",
                content=text,
                author=ME,
                is_own_content=True,
                metadata={
                    "source": "changelog",
                    "resource_name": "socialActions/comments",
                    "method": method,
                    "commented_on": "urn:li:activity:7418912385886941184",
                    # processedAt is often 30 s late; capturedAt is close
                    "raw": {"capturedAt": captured, "processedAt": captured + 30000},
                },
            ),
        )


def _items(conn, where: str) -> list[dict]:
    rows = conn.execute(f"SELECT * FROM items WHERE {where} ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def test_post_urn_of_reads_encoded_links() -> None:
    assert post_urn_of(f"{FEED}urn%3Ali%3Ashare%3A12/") == "urn:li:share:12"
    assert post_urn_of(f"{FEED}urn:li:groupPost:123-456") == "urn:li:groupPost:123-456"
    assert post_urn_of("https://example.com") is None


def test_import_adds_what_the_sync_lacks(config: Config, export: Path) -> None:
    with get_connection(config) as conn:
        _held(conn)
        report = import_export(conn, export)
        assert (report.posts_added, report.posts_held) == (1, 1)
        assert (report.comments_added, report.comments_held) == (2, 2)
        assert (report.articles_added, report.filled, report.skipped) == (2, 1, 1)

        (post,) = _items(
            conn,
            "json_extract(metadata, '$.source') = 'export' "
            "AND json_extract(metadata, '$.resource_name') = 'ugcPosts'",
        )
        assert post["content"] == "An old post\non two lines"
        assert post["author"] == ME and post["is_own_content"] == 1
        assert post["created_at"].startswith("2024-03-30 17:02:00+00:00")

        comments = _items(
            conn, "json_extract(metadata, '$.source') = 'export' AND kind = 'comment'"
        )
        assert [c["content"] for c in comments] == [
            'Says "hello", twice',
            "Another in the same second",
        ]
        assert comments[0]["source_id"] != comments[1]["source_id"]
        # Their post isn't held: each replies to the post's stub
        (stub,) = _items(conn, "kind = 'stub'")
        links = conn.execute(
            "SELECT from_id FROM item_links WHERE to_id = ? AND kind = 'in_reply_to'",
            (stub["id"],),
        ).fetchall()
        assert {r[0] for r in links} == {c["id"] for c in comments}

        # The comment the sync held without text now has the export's
        assert _items(conn, "source_id = 'changelog-socialActions/comments-1'")[0]["content"] == (
            "Text the sync never gave"
        )


def test_import_twice_adds_nothing(config: Config, export: Path) -> None:
    with get_connection(config) as conn:
        _held(conn)
        import_export(conn, export)
        count = conn.execute("SELECT count(*) FROM items").fetchone()[0]
        again = import_export(conn, export)
        assert (again.posts_added, again.comments_added, again.articles_added) == (0, 0, 0)
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == count


def test_an_article_held_by_hand_gets_the_full_text(config: Config, export: Path) -> None:
    with get_connection(config) as conn:
        upsert_item(
            conn,
            ItemCreate(
                source_type="linkedin",
                source_id="https://www.linkedin.com/pulse/an-article-me-abc/",
                title="Added by hand",
                content="An excerpt",
                metadata={"resource_name": "article", "captured_from": "notes"},
            ),
        )
        report = import_export(conn, export)
        assert (report.articles_added, report.articles_updated) == (1, 1)
        (article,) = _items(conn, "source_id LIKE 'https://www.linkedin.com/pulse/%'")
        assert article["title"] == "An article"
        assert "Second, with a [link](https://x.org)." in article["content"]
        assert '"captured_from": "notes"' in article["metadata"]


def test_parse_article_reads_times_and_drafts() -> None:
    article = parse_article(ARTICLE, "an-article-me-abc.html")
    assert article is not None
    assert article.url == "https://www.linkedin.com/pulse/an-article-me-abc"
    assert article.published is not None and article.published.isoformat().startswith(
        "2024-02-07T21:03"
    )
    assert article.text.startswith("First paragraph.")
    assert "## Part" in article.text and "Created on" not in article.text

    draft = parse_article(DRAFT, "2021-10-21 03:23:26.0-A draft.html")
    assert draft is not None and draft.published is None and draft.url is None
    assert draft.title == "A draft"


def test_not_an_export(config: Config, tmp_path: Path) -> None:
    path = tmp_path / "other.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("readme.txt", "hi")
    with get_connection(config) as conn, pytest.raises(ValueError, match="Not a LinkedIn"):
        import_export(conn, path)
